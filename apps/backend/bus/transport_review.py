"""Operator decisions are durable evidence, never fabricated transport receipts."""
from __future__ import annotations

import json
import sqlite3


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def inbound_account(row):
    channel = row['payload'].get('channel', '')
    namespace = row['id'].rsplit(':', 2)[0]
    prefix = 'in:' + channel + ':'
    return namespace[len(prefix):] if channel and namespace.startswith(prefix) else None


class TransportReview:
    db: sqlite3.Connection
    active: set[str]

    def get(self, key: str) -> dict:
        raise NotImplementedError

    # Kept with the queue transaction: no cross-database atomicity is assumed.
    def init_review(self):
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}
        for name, declaration in [('revision', 'INTEGER NOT NULL DEFAULT 0'),
                                  ('review_closed', 'INTEGER NOT NULL DEFAULT 0'),
                                  ('attempt_limit', 'INTEGER NOT NULL DEFAULT 5')]:
            if name not in columns:
                self.db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {declaration}')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS transport_reviews (
                seq INTEGER PRIMARY KEY, action_id TEXT NOT NULL UNIQUE,
                job TEXT NOT NULL, request TEXT NOT NULL, evidence TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
            CREATE INDEX IF NOT EXISTS transport_reviews_job ON transport_reviews(job,seq);
            CREATE TABLE IF NOT EXISTS transport_execution_links (
                job TEXT NOT NULL, turn_id TEXT NOT NULL, PRIMARY KEY(job,turn_id));
            CREATE TRIGGER IF NOT EXISTS transport_job_revision AFTER UPDATE OF
                state,attempts,due,result,error,review_closed,attempt_limit ON jobs
                BEGIN UPDATE jobs SET revision=revision+1 WHERE id=NEW.id; END;
            CREATE TRIGGER IF NOT EXISTS transport_part_insert AFTER INSERT ON parts
                BEGIN UPDATE jobs SET revision=revision+1 WHERE id=NEW.job; END;
            CREATE TRIGGER IF NOT EXISTS transport_part_update AFTER UPDATE ON parts
                BEGIN UPDATE jobs SET revision=revision+1 WHERE id=NEW.job; END;
            CREATE TRIGGER IF NOT EXISTS transport_review_immutable_update
                BEFORE UPDATE ON transport_reviews BEGIN SELECT RAISE(ABORT,'Immutable review'); END;
            CREATE TRIGGER IF NOT EXISTS transport_review_immutable_delete
                BEFORE DELETE ON transport_reviews BEGIN SELECT RAISE(ABORT,'Immutable review'); END;
        ''')

    def link_execution(self, key, turn_id):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO transport_execution_links VALUES (?,?)', (key, turn_id))

    def related_keys(self, row):
        key, kind, payload = row['id'], row['kind'], row['payload']
        candidates = {key}
        if kind.startswith('telegram-update:'):
            message = payload.get('message') or payload.get('edited_message') or {}
            chat = message.get('chat') or {}
            if chat.get('id') is not None and message.get('message_id') is not None:
                candidates.add(f"in:{kind.split(':', 1)[1]}:{chat['id']}:{message['message_id']}")
        if key.startswith(('reply:', 'command:')):
            candidates.add(key.split(':', 1)[1])
        for candidate in tuple(candidates):
            if candidate.startswith('in:'):
                candidates.update({'reply:' + candidate, 'command:' + candidate})
                identity = candidate.rsplit(':', 2)
                if len(identity) == 3:
                    namespace, chat, message = identity
                    for raw in self.db.execute('''SELECT id FROM jobs WHERE kind=? AND
                        (CAST(json_extract(payload,'$.message.chat.id') AS TEXT)=? AND
                         CAST(json_extract(payload,'$.message.message_id') AS TEXT)=?
                         OR CAST(json_extract(payload,'$.edited_message.chat.id') AS TEXT)=? AND
                         CAST(json_extract(payload,'$.edited_message.message_id') AS TEXT)=?)''',
                         ('telegram-update:' + namespace[3:], chat, message, chat, message)):
                        candidates.add(raw[0])
        return sorted(candidate for candidate in candidates
                      if self.db.execute('SELECT 1 FROM jobs WHERE id=?', (candidate,)).fetchone())

    def execution_records(self, row, journal):
        if journal is None:
            return []
        records = {}
        for key in self.related_keys(row):
            related = self.get(key)
            payload = related['payload']
            metadata = payload.get('metadata') or {}
            request = metadata.get('execution_request_id')
            channel = payload.get('channel')
            session = metadata.get('session_key_override') or payload.get('session_key') or (
                f"{channel}:{payload.get('chat_id')}" if channel else None)
            if request and channel and session:
                record = journal.find(session, channel, request)
                if record:
                    records[record['turn_id']] = record
            for link in self.db.execute('SELECT turn_id FROM transport_execution_links WHERE job=?', (key,)):
                record = journal.get(link[0])  # Missing/corrupt evidence must fail closed.
                records[record['turn_id']] = record
        return list(records.values())

    def require_execution_retry_clearance(self, turn_id, journal):
        # A queue can outlive its caller. Never grant a whole-turn retry while a
        # linked send can still run, or after a confirmed part was delivered.
        before = 0
        while True:
            page = self.list_jobs(journal=journal, turn_id=turn_id, status='', before=before, limit=100)
            for row in page['jobs']:
                if row['kind'] == 'inbound' or row['kind'].startswith('telegram-update:'):
                    continue
                if not row['review_closed'] or self.db.execute(
                        "SELECT 1 FROM parts WHERE job=? AND state='done'", (row['id'],)).fetchone():
                    raise ValueError('关联发件队列仍可发送或已有成功回执；请核对队列，不能整轮重试')
            if len(page['jobs']) < 100:
                return
            before = page['next_cursor']

    def detail(self, key, *, journal=None, accounts=None, handlers=None):
        row = self.get(key)
        parts = [{**dict(p), 'payload': json.loads(p['payload']),
                  'result': json.loads(p['result']) if p['result'] else None}
                 for p in self.db.execute('SELECT * FROM parts WHERE job=? ORDER BY part', (key,))]
        records = self.execution_records(row, journal)
        reviews = [{**dict(r), 'request': json.loads(r['request']), 'evidence': json.loads(r['evidence'])}
                   for r in self.db.execute('SELECT * FROM transport_reviews WHERE job=? ORDER BY seq', (key,))]
        reason = ''
        unknown = [p['part'] for p in parts if p['state'] == 'sending']
        active = key in self.active or row['state'] == 'running' or any(r['status'] in {'accepted', 'running'} for r in records)
        if active:
            reason = '任务仍在处理中，请等待停止后刷新'
        elif row['review_closed']:
            reason = '已结束处置，不再重试此队列任务'
        elif row['state'] not in {'failed', 'needs_review'}:
            reason = '仅失败或待核对的任务可重试'
        elif row['kind'].startswith('telegram-update:'):
            reason = '原始更新可能包含命令；本阶段仅支持查看和结束处置'
        elif row['kind'] == 'inbound':
            if journal is None:
                reason = '执行记录不可用，无法确认能否重新处理'
            elif inbound_account(row) is not None and inbound_account(row) != (accounts or {}).get(row['payload'].get('channel')):
                reason = '收件账户与当前渠道不一致，请恢复原账户后核对'
            elif not (row['payload'].get('metadata') or {}).get('execution_request_id'):
                reason = '缺少稳定执行请求标识，不能安全重新处理'
            elif any(r['status'] != 'completed' for r in records):
                reason = '原输入已接纳且未完成，请查看执行记录；队列不能重跑模型'
        elif handlers is None or row['kind'] not in handlers:
            reason = '发送渠道未就绪，请恢复原渠道后刷新'
        elif row['payload'].get('_transport_account') != (accounts or {}).get(row['payload'].get('channel')):
            reason = '渠道账户与原任务不一致，不能改发到其他账户'
        if not reason and unknown:
            reason = '需逐项核实所有结果未知的分段均未送达，填写依据后重试'
        from bus.durable import proactive_payload
        if proactive_payload(row['payload']) and (unknown or any(r['request'].get('absent_parts') for r in reviews)):
            reason = '主动提醒的未知发送结果不能通过重新排队解除；请核对并结束处置'
        return {**row, 'parts': parts, 'reviews': reviews, 'related_jobs': self.related_keys(row),
                'executions': [{'turn_id': r['turn_id'], 'session_key': r['session_key'], 'status': r['status']}
                               for r in records],
                'retry': {'allowed': not reason or bool(unknown and reason.startswith('需逐项')),
                          'unknown_parts': unknown, 'reason': reason or '仅重新排队；成功分段不会再次发送'},
                'can_close': not active and not row['review_closed'] and row['state'] in {'failed', 'needs_review', 'pending'}}

    def list_jobs(self, *, journal=None, session_key='', turn_id='', status='attention', before=0, limit=25):
        if status not in {'', 'attention', 'pending', 'running', 'done', 'failed', 'needs_review', 'closed'}:
            raise ValueError('未知队列状态')
        limit = max(1, min(int(limit), 100))
        before = int(before)
        # A stable insertion cursor; revisions never reorder pages.
        rows = self.db.execute('''SELECT rowid AS cursor,id FROM jobs
            WHERE (?=0 OR rowid<?) AND
            (?='' OR (?='attention' AND state IN ('failed','needs_review') AND review_closed=0)
             OR (?='closed' AND review_closed=1) OR (state=? AND review_closed=0))
            ORDER BY rowid DESC''', (before, before, status, status, status, status))
        found = []
        for entry in rows:
            row = self.get(entry['id'])
            records = self.execution_records(row, journal)
            payload = row['payload']
            metadata = payload.get('metadata') or {}
            session = metadata.get('session_key_override') or payload.get('session_key') or f"{payload.get('channel')}:{payload.get('chat_id')}"
            if session_key and session != session_key and not any(r['session_key'] == session_key for r in records):
                continue
            if turn_id and not any(r['turn_id'] == turn_id for r in records):
                continue
            found.append({**row, 'cursor': entry['cursor']})
            if len(found) >= limit:
                break
        return {'jobs': found, 'next_cursor': found[-1]['cursor'] if found else 0}

    def review_job(self, key, *, expected_revision, action_id, decision, note,
                   absent_parts=None, journal=None, accounts=None, handlers=None):
        if not isinstance(action_id, str) or not action_id.strip() or len(action_id) > 128:
            raise ValueError('处置标识无效')
        if not isinstance(note, str) or not note.strip() or len(note) > 4000:
            raise ValueError('请填写核对依据，最多 4000 字')
        if decision not in {'close', 'retry'}:
            raise ValueError('未知处置方式')
        absent_parts = absent_parts or []
        if not isinstance(absent_parts, list) or any(not isinstance(p, str) for p in absent_parts):
            raise ValueError('分段核对结果无效')
        request = encoded(dict(job=key, expected_revision=expected_revision, action_id=action_id,
                               decision=decision, note=note, absent_parts=sorted(absent_parts)))
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.db.execute('SELECT request FROM transport_reviews WHERE action_id=?', (action_id,)).fetchone()
            if old:
                if old[0] != request:
                    raise ValueError('处置标识已用于其他内容')
            else:
                detail = self.detail(key, journal=journal, accounts=accounts, handlers=handlers)
                if type(expected_revision) is not int or detail['revision'] != expected_revision:
                    raise ValueError('队列记录已变化，请刷新后重新核对')
                if decision == 'close':
                    if not detail['can_close']:
                        raise ValueError('当前任务不能结束处置，请刷新记录')
                else:
                    if not detail['retry']['allowed']:
                        raise ValueError(detail['retry']['reason'])
                    if sorted(absent_parts) != sorted(detail['retry']['unknown_parts']):
                        raise ValueError('请逐项核实所有结果未知的分段均未送达；不要勾选成功分段')
                evidence = {k: detail[k] for k in ('state', 'attempts', 'revision', 'result', 'error', 'parts', 'executions')}
                self.db.execute('INSERT INTO transport_reviews(action_id,job,request,evidence) VALUES (?,?,?,?)',
                                (action_id, key, request, encoded(evidence)))
                if decision == 'close':
                    self.db.execute('UPDATE jobs SET review_closed=1 WHERE id=?', (key,))
                else:
                    # Keep prior unknown physical state in immutable audit evidence.
                    for part in absent_parts:
                        self.db.execute("UPDATE parts SET state='pending' WHERE job=? AND part=?", (key, part))
                    self.db.execute("UPDATE jobs SET state='pending',due=0,attempt_limit=attempts+5 WHERE id=?", (key,))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return self.detail(key, journal=journal, accounts=accounts, handlers=handlers)
