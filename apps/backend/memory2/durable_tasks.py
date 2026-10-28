"""Reply facts own admission; memory DB owns work receipts and mutations.

Model results stay in the execution log. A receipt is a reference to that fact,
not another model verdict. No response after a network attempt means blocked.
"""
from __future__ import annotations

import asyncio
from contextvars import Context, ContextVar
import json
import hashlib
import logging
from typing import Any

from core.memory.task_context import memory_task, MemoryTaskBlocked
from core.execution.model_context import digest
from core.execution.scope import ExecutionScope, current_execution

logger = logging.getLogger(__name__)
_frame: ContextVar[tuple[str, list[int]]] = ContextVar('memory_step_frame')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def fault(point):
    """Explicit process-test boundary; production is a no-op."""


class MemoryTasks:
    VERSION = 1

    def __init__(self, store, journal, worker):
        self.store, self.journal, self.worker = store, journal, worker
        self.db = store._db
        self.task = None
        self.closed = False
        self.consolidation_handler: Any = None
        self.lock = asyncio.Lock()
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS memory_task_cursor(root TEXT PRIMARY KEY, seq INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS memory_tasks(
                id TEXT PRIMARY KEY, root TEXT NOT NULL, source_seq INTEGER NOT NULL,
                source_turn TEXT, version INTEGER NOT NULL, input TEXT NOT NULL,
                state TEXT NOT NULL, owner TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS memory_task_steps(
                job TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL, result TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                PRIMARY KEY(job,path));
        ''')
        self.db.commit()
        journal.memory_tasks_enabled = True

    def discover(self):
        """Cursor and admission share a transaction. A lost wakeup loses no work."""
        root = self.journal.root_id
        row = self.db.execute('SELECT seq FROM memory_task_cursor WHERE root=?', (root,)).fetchone()
        after = row[0] if row else 0
        events = self.journal._conn.execute("SELECT seq,payload FROM events WHERE kind='conversation.changed' AND seq>? ORDER BY seq LIMIT 128", (after,)).fetchall()
        if not events:
            return
        with self.db:
            for event in events:
                envelope = json.loads(event['payload'])
                payload = envelope.get('data', {})
                intent = payload.get('memory_post_response')
                if not isinstance(intent, dict):
                    continue
                from session.store.projection import encode as encode_fact
                if hashlib.sha256(encode_fact(payload).encode()).hexdigest() != envelope.get('digest'):
                    raise MemoryTaskBlocked('source_fact_corrupt')
                if payload.get('root_id') != root or not payload.get('execution_turn_id'):
                    raise MemoryTaskBlocked('source_identity_missing')
                job = digest({'root': root, 'turn': payload['execution_turn_id'], 'kind': 'post_response.v1'})
                self.db.execute('INSERT OR IGNORE INTO memory_tasks VALUES (?,?,?,?,?,?,?,NULL,NULL)',
                    (job, root, event['seq'], payload.get('execution_turn_id'), self.VERSION, encode(intent), 'pending'))
            self.db.execute('INSERT OR REPLACE INTO memory_task_cursor VALUES (?,?)', (root, events[-1]['seq']))
            fault('before_admission_commit')
        fault('admitted')

    def start(self):
        if not self.closed and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self.run(), name='memory_tasks', context=Context())

    def enqueue_consolidation(self, event, existing_profile):
        from dataclasses import asdict
        job = 'markdown-callback:' + event.task_id
        payload = {'kind': 'markdown_consolidation', 'session_key': event.session_key,
                   'event': asdict(event), 'existing_profile': existing_profile}
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO memory_tasks VALUES (?,?,?,?,?,?,?,NULL,NULL)',
                (job, self.journal.root_id, event.source_seq, event.source_turn, self.VERSION, encode(payload), 'pending'))
        return job

    def list_tasks(self, session_key='', limit=50):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('Invalid memory task limit')
        rows = self.db.execute('''SELECT id,source_seq,source_turn,state,error,input FROM memory_tasks
            WHERE root=? AND (?='' OR json_extract(input,'$.session_key')=?)
            ORDER BY source_seq DESC LIMIT ?''', (self.journal.root_id, session_key, session_key, limit)).fetchall()
        return {'tasks': [dict(id=r[0], source_seq=r[1], source_turn_id=r[2], state=r[3], error=r[4],
                               session_key=json.loads(r[5])['session_key']) for r in rows]}

    async def run(self):
        while not self.closed:
            try:
                await self.drain()
            except Exception:
                logger.exception('memory task discovery failed; persisted work retained')
            await asyncio.sleep(1)  # Local SQLite only; not the proactive/model poll.

    async def drain(self):
        async with self.lock:
            self.discover()
            rows = self.db.execute("SELECT * FROM memory_tasks WHERE root=? AND state IN ('pending','running') ORDER BY source_seq", (self.journal.root_id,)).fetchall()
            columns = [c[1] for c in self.db.execute('PRAGMA table_info(memory_tasks)')]
            for raw in rows:
                row = dict(zip(columns, raw))
                if row['state'] == 'running' and row['owner'] != self.journal.epoch:
                    from core.execution.lease import EpochLease
                    try:
                        lease = EpochLease(self.journal.directory / (row['owner'] + '.lock'))
                    except OSError:
                        continue
                    lease.close()
                elif row['state'] == 'running':
                    continue
                with self.db:
                    updated = self.db.execute("UPDATE memory_tasks SET state='running',owner=? WHERE id=? AND state=? AND owner IS ?",
                        (self.journal.epoch, row['id'], row['state'], row['owner'])).rowcount
                if not updated:
                    continue
                context = Task(self, row)
                token = memory_task.set(context)
                frame = _frame.set(('', [0]))
                try:
                    if row['version'] != self.VERSION:
                        raise MemoryTaskBlocked('unsupported_workflow_version')
                    inputs = json.loads(row['input'])
                    if inputs.get('kind') == 'markdown_consolidation':
                        if self.consolidation_handler is None:
                            raise MemoryTaskBlocked('consolidation_handler_unavailable')
                        from core.memory.events import ConsolidationCommitted
                        await self.consolidation_handler(ConsolidationCommitted(**inputs['event']), existing_profile=inputs['existing_profile'])
                    else:
                        await self.worker.run(**inputs, source_ref='memory-task:' + row['id'])
                    fault('before_done')
                    with self.db:
                        self.db.execute("UPDATE memory_tasks SET state='done',error=NULL WHERE id=?", (row['id'],))
                    fault('done')
                except asyncio.CancelledError:
                    # Keep running/owner so startup verifies the old epoch died.
                    raise
                except Exception as exc:
                    with self.db:
                        reason = str(exc) if isinstance(exc, MemoryTaskBlocked) else type(exc).__name__
                        self.db.execute("UPDATE memory_tasks SET state='blocked',error=? WHERE id=?", (reason, row['id']))
                    logger.warning('memory task blocked: id=%s error=%s', row['id'], type(exc).__name__)
                finally:
                    _frame.reset(frame)
                    memory_task.reset(token)

    async def aclose(self):
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass


class Task:
    def __init__(self, owner, row):
        self.owner, self.row, self.store = owner, row, owner.store

    def path(self, kind):
        parent, counter = _frame.get()
        counter[0] += 1
        return f'{parent}/{counter[0]}:{kind}'

    def read(self, path, inputs):
        row = self.owner.db.execute('SELECT result,input_digest FROM memory_task_steps WHERE job=? AND path=?', (self.row['id'], path)).fetchone()
        if row and row[1] != digest(inputs):
            raise MemoryTaskBlocked('checkpoint_input_changed')
        return (True, json.loads(row[0])) if row else (False, None)

    def save(self, path, kind, inputs, value):
        self.owner.db.execute('INSERT INTO memory_task_steps VALUES (?,?,?,?,?)', (self.row['id'], path, kind, encode(value), digest(inputs)))

    async def async_step(self, kind, inputs, operation):
        path = self.path(kind)
        found, value = self.read(path, inputs)
        if found:
            return value
        frame = _frame.set((path, [0]))
        try:
            value = await operation()
            with self.owner.db:
                self.save(path, kind, inputs, value)
            return value
        finally:
            _frame.reset(frame)

    async def external_step(self, kind, inputs, operation):
        path = self.path(kind)
        found, value = self.read(path, inputs)
        if found:
            if value['state'] != 'done':
                raise MemoryTaskBlocked('embedding_outcome_unknown')
            return value['result']
        with self.owner.db:
            self.save(path, kind, inputs, {'state': 'started'})
        value = await operation()
        with self.owner.db:
            self.owner.db.execute('UPDATE memory_task_steps SET result=? WHERE job=? AND path=?',
                (encode({'state':'done','result':value}), self.row['id'], path))
        return value

    def store_step(self, kind, inputs, operation):
        path = self.path(kind)
        found, value = self.read(path, inputs)
        if found:
            return value
        # Existing mutators call commit internally. Defer those commits until
        # the result receipt is in the SAME memory transaction.
        db = self.owner.db
        token = memory_task.set(None)
        with self.store._lock:
            db.execute('BEGIN IMMEDIATE')
            db.defer_commit = True
            try:
                value = operation()
                self.save(path, kind, inputs, value)
                fault('before_memory_commit')
                fault('before_memory_commit:' + kind)
                db.defer_commit = False
                db.commit()
                fault('after_memory_commit')
                fault('after_memory_commit:' + kind)
                return value
            except BaseException:
                db.rollback()
                raise
            finally:
                db.defer_commit = False
                memory_task.reset(token)

    async def model(self, provider, operation, args, kwargs):
        from agent.provider import LLMResponse, ToolCall
        journal = self.owner.journal
        path = self.path('model')
        # Resolve the immutable result even if the process died before a local
        # checkpoint or run terminal. Never re-send a dispatched unknown call.
        key = digest({'job': self.row['id'], 'path': path})
        for generation in range(100):
            request_key = key + ':' + str(generation)
            row, created = journal.admit('memory-model:' + key, 'model', request_key,
                {'origin': 'memory_task', 'memory_task_id': self.row['id'], 'step': path,
                 'parent_execution': {'root_id': journal.root_id, 'turn_id': self.row['source_turn']}})
            facts = [dict(e, payload=json.loads(e['payload'])) for e in journal._conn.execute(
                'SELECT * FROM events WHERE turn_id=? ORDER BY seq', (row['turn_id'],))]
            responses = [e for e in facts if e['kind'] == 'model.response.recorded' and 'response' in e['payload']]
            if responses:
                saved = journal.model_response(row['turn_id'], responses[-1]['payload']['call_id'])
                saved['tool_calls'] = [ToolCall(**c) for c in saved.get('tool_calls', [])]
                return LLMResponse(**saved)
            if any(e['kind'] == 'model.attempt.started' for e in facts):
                raise MemoryTaskBlocked('model_outcome_unknown')
            if created:
                break
            if row['status'] in {'accepted', 'running'}:
                raise MemoryTaskBlocked('model_owner_active')
        else:
            raise MemoryTaskBlocked('model_admission_exhausted')
        fault('before_model')
        journal.start(row['turn_id'])
        scope = ExecutionScope(journal, row['turn_id'])
        token = current_execution.set(scope)
        task_token = memory_task.set(None)
        try:
            response = await operation(provider, *args, **kwargs)
            fault('model_response')
            journal.finish(scope.turn_id, 'completed')
            return response
        except BaseException:
            if not scope.broken:
                journal.finish(scope.turn_id, 'failed')
            raise
        finally:
            scope.active = False
            memory_task.reset(task_token)
            current_execution.reset(token)
