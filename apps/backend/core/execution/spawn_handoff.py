"""Durable child result handoff, sharing the execution journal transaction.

These coordination tables survive projection rebuilds. Execution facts remain the
authority for work/results; a claim is permission to deliver, never to rerun work.
"""
from __future__ import annotations

import json
from uuid import uuid4

from .contracts import ExecutionConflict, ExecutionNeedsReview
from .lease import EpochLease


def ensure_spawn_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS spawn_tasks (
            job_id TEXT PRIMARY KEY, parent_task_id TEXT NOT NULL,
            parent_turn_id TEXT NOT NULL, profile TEXT NOT NULL,
            retry_count INTEGER NOT NULL, route TEXT NOT NULL,
            cancelled INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS spawn_parent ON spawn_tasks(parent_task_id);
        CREATE TABLE IF NOT EXISTS spawn_completions (
            job_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
            owner_epoch TEXT, claim_token TEXT, consumer_turn_id TEXT,
            acked INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS spawn_completion_pending ON spawn_completions(acked);
        CREATE UNIQUE INDEX IF NOT EXISTS spawn_consumer_turn ON spawn_completions(consumer_turn_id)
            WHERE consumer_turn_id IS NOT NULL;
    """)


class SpawnHandoff:
    # One initial dispatch plus one supplementary dispatch per parent task.
    SPAWN_ATTEMPT_LIMIT = 2

    def spawn_parent_context(self, turn_id):
        with self._transaction():
            parent_task_id, followup = self._spawn_parent(turn_id)
            return {'parent_task_id': parent_task_id, 'followup': followup}

    def _spawn_parent(self, turn_id):
        """Resolve host-owned lineage, including explicit controlled continuation."""
        seen = set()
        while turn_id and turn_id not in seen:
            seen.add(turn_id)
            linked = self._conn.execute(
                'SELECT t.parent_task_id FROM spawn_completions c JOIN spawn_tasks t USING(job_id) '
                'WHERE c.consumer_turn_id=?', (turn_id,)).fetchone()
            if linked:
                return linked[0], True
            state = self._state(turn_id)
            ancestor = state.get('retry_of') or (state.get('continuation') or {}).get('source_turn_id')
            if not ancestor:
                return turn_id, False
            turn_id = ancestor
        raise ExecutionConflict('Invalid spawn parent lineage')

    def reserve_spawn(self, job_id: str, parent_turn_id: str, profile: str, route: dict) -> dict:
        """Reserve before dispatch. Model task/label/retry_count never select a budget."""
        from .journal import encode
        if not job_id or not parent_turn_id or profile not in {'research', 'scripting', 'general'}:
            raise ValueError('Stable child/parent identity and a valid tool profile are required')
        with self._transaction():
            parent_task_id, followup = self._spawn_parent(parent_turn_id)
            state = self._state(parent_turn_id)
            if state['status'] != 'running' or state['epoch'] != self.epoch:
                raise ExecutionConflict('Parent execution is not running')
            prior = self._conn.execute('SELECT * FROM spawn_tasks WHERE parent_task_id=? ORDER BY rowid',
                                       (parent_task_id,)).fetchall()
            if any(r['cancelled'] for r in prior):
                raise ExecutionConflict('父任务已取消，不能继续委派')
            if len(prior) >= self.SPAWN_ATTEMPT_LIMIT:
                raise ExecutionConflict('父任务委派额度已耗尽：最多初次执行和一次补做')
            if prior:
                # A changed tool profile must not widen an existing authorization.
                allowed = {'general': {'general', 'research', 'scripting'},
                           'research': {'research'}, 'scripting': {'scripting'}}
                if profile not in allowed.get(prior[0]['profile'], set()):
                    raise ExecutionConflict('补做不能扩大子任务工具权限')
            if followup:
                source = self._conn.execute(
                    'SELECT payload FROM spawn_completions WHERE consumer_turn_id=?', (parent_turn_id,)).fetchone()
                # Continuation/retry lineage may point at a consumer ancestor.
                if source is None:
                    source = self._conn.execute(
                        'SELECT c.payload FROM spawn_completions c JOIN spawn_tasks t USING(job_id) '
                        'WHERE t.parent_task_id=? ORDER BY c.rowid DESC LIMIT 1', (parent_task_id,)).fetchone()
                event = json.loads(source[0])['event'] if source else {}
                if event.get('exit_reason') not in {'max_iterations', 'tool_loop'}:
                    raise ExecutionNeedsReview('此子任务结果不允许自动补做，请核对后另开请求')
            count = len(prior)
            self._conn.execute('INSERT INTO spawn_tasks(job_id,parent_task_id,parent_turn_id,profile,retry_count,route) '
                               'VALUES (?,?,?,?,?,?)', (job_id, parent_task_id, parent_turn_id, profile, count, encode(route)))
            return {'job_id': job_id, 'parent_task_id': parent_task_id, 'parent_turn_id': parent_turn_id,
                    'retry_count': count}

    def cancel_spawn(self, job_id):
        with self._transaction():
            changed = self._conn.execute('UPDATE spawn_tasks SET cancelled=1 WHERE parent_task_id IN '
                                        '(SELECT parent_task_id FROM spawn_tasks WHERE job_id=?)', (job_id,))
            return changed.rowcount > 0

    def _derive_spawn_completion(self, row):
        """Called inside terminal commit (and startup reconciliation for old prefixes)."""
        from .journal import encode
        if row['source'] != 'subagent' or row['status'] in {'accepted', 'running'}:
            return
        task = self._conn.execute('SELECT * FROM spawn_tasks WHERE job_id=?', (row['request_key'],)).fetchone()
        if task is None:
            return  # Old releases did not persist a route; never invent a recipient.
        route = json.loads(task['route'])
        result = json.loads(row['result']) if row['result'] else {}
        result = result or {}
        reason = result.get('exit_reason', row['status'])
        summary = result.get('result_summary', '')
        state = self._state(row['turn_id'])
        if row['status'] == 'needs_review' or (row['status'] != 'completed' and
                any(not e['retry_safe'] for e in state['effects'].values())):
            reason = 'needs_review'
            summary = f"子任务存在需核对的外部操作，请查看执行 {row['turn_id']}，核对前不要重试。"
        elif not result:
            reason = 'cancelled' if task['cancelled'] else 'needs_review'
            summary = '后台任务已取消。' if task['cancelled'] else f"子任务执行中断，请核对执行 {row['turn_id']}，不会自动重跑。"
        if len(summary) > 12000:
            summary = summary[:12000] + f'\n...[结果已截断，原始长度 {len(summary)}]'
        label, task_text = route.pop('label'), route.pop('task')
        artifact_refs = route.pop('artifact_refs', [])
        payload = {**route, 'event': {
            'job_id': task['job_id'], 'label': label, 'task': task_text,
            'status': result.get('status', 'cancelled' if reason == 'cancelled' else 'error'),
            'exit_reason': reason, 'result': summary, 'profile': task['profile'],
            'retry_count': task['retry_count'], 'parent_task_id': task['parent_task_id'],
            'parent_turn_id': task['parent_turn_id'], 'child_turn_id': row['turn_id'],
            'artifact_refs': artifact_refs}}
        self._conn.execute('INSERT OR IGNORE INTO spawn_completions(job_id,payload) VALUES (?,?)',
                           (task['job_id'], encode(payload)))

    def reconcile_spawn_completions(self):
        with self._transaction():
            for row in self._conn.execute("SELECT * FROM executions WHERE source='subagent' "
                                          "AND status NOT IN ('accepted','running')").fetchall():
                self._derive_spawn_completion(row)

    def claim_spawn_completion(self, *, accounts: dict[str, str] | None = None):
        """Exclusive, epoch-fenced claim; a live slow consumer is never timed out."""
        with self._transaction():
            for row in self._conn.execute('SELECT * FROM spawn_completions WHERE acked=0 ORDER BY rowid').fetchall():
                payload = json.loads(row['payload'])
                if accounts is not None and payload['channel'] not in {'desktop', 'cli'}:
                    expected = payload.get('metadata', {}).get('_spawn_account')
                    if not expected or expected != accounts.get(payload['channel']):
                        continue  # Keep pending; never bind old work to a new account.
                if row['owner_epoch']:
                    if row['owner_epoch'] == self.epoch:
                        continue
                    try:
                        lease = EpochLease(self.directory / f"{row['owner_epoch']}.lock")
                    except (BlockingIOError, PermissionError):
                        continue
                    try:
                        self._recover_epoch(row['owner_epoch'])
                    finally:
                        lease.close()
                token = uuid4().hex
                self._conn.execute('UPDATE spawn_completions SET owner_epoch=?,claim_token=? WHERE job_id=?',
                                   (self.epoch, token, row['job_id']))
                return {**json.loads(row['payload']), 'claim_token': token}
        return None

    def _check_spawn_claim(self, job_id, token, *, allow_acked=False):
        row = self._conn.execute('SELECT * FROM spawn_completions WHERE job_id=?', (job_id,)).fetchone()
        if row is None or (row['acked'] and not allow_acked) or row['owner_epoch'] != self.epoch or row['claim_token'] != token:
            raise ExecutionConflict('Spawn completion claim is stale')
        return row

    def admit_spawn_consumer(self, job_id, token, session_key):
        """Admission and receipt link commit together, under the same stable identity."""
        with self._transaction():
            completion = self._check_spawn_claim(job_id, token)
            payload = json.loads(completion['payload'])
            expected = payload.get('metadata', {}).get('session_key_override') or f"{payload['channel']}:{payload['chat_id']}"
            if expected != session_key:
                raise ExecutionConflict('Completion recipient changed')
            record, created = self._admit(session_key, 'spawn_completion', job_id, payload)
            self._conn.execute('UPDATE spawn_completions SET consumer_turn_id=? WHERE job_id=?',
                               (record['turn_id'], job_id))
            if not created and record['status'] == 'interrupted':
                state = self._state(record['turn_id'])
                if not state.get('started') and not (state.get('review') or {}).get('decision') == 'close':
                    # Only admission occurred: no model, tool, plugin or delivery ran.
                    self._event(self._row(record['turn_id']), 'spawn.admission.reclaimed', {'epoch': self.epoch})
                    record = self._decode(self._row(record['turn_id']))
                    created = True
            task = self._conn.execute('SELECT cancelled FROM spawn_tasks WHERE job_id=?', (job_id,)).fetchone()
            return {**record, 'spawn_cancelled': bool(task['cancelled'])}, created

    def settle_spawn_completion(self, job_id, token, *, handled=True):
        """ACK only a terminal consumer. Conflict before admission releases for polling."""
        with self._transaction():
            row = self._check_spawn_claim(job_id, token, allow_acked=True)
            if row['acked']:
                return True
            if row['consumer_turn_id']:
                state = self._state(row['consumer_turn_id'])
                if state['status'] in {'accepted', 'running'}:
                    return False
                if handled:
                    self._conn.execute('UPDATE spawn_completions SET acked=1 WHERE job_id=?', (job_id,))
                    return True
            if not row['consumer_turn_id'] or not handled:
                self._conn.execute('UPDATE spawn_completions SET owner_epoch=NULL,claim_token=NULL WHERE job_id=?', (job_id,))
            return True

    def spawn_completion(self, job_id):
        with self._transaction():
            row = self._conn.execute('SELECT * FROM spawn_completions WHERE job_id=?', (job_id,)).fetchone()
            return {**dict(row), 'payload': json.loads(row['payload'])} if row else None

    def spawn_task(self, job_id):
        """Stable downstream join: child job -> parent task/turn, profile and route."""
        with self._transaction():
            row = self._conn.execute('SELECT * FROM spawn_tasks WHERE job_id=?', (job_id,)).fetchone()
            return {**dict(row), 'route': json.loads(row['route'])} if row else None
