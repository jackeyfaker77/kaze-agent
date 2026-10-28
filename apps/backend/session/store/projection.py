"""Durable conversation facts and a rebuildable SQLite materialized view.

Facts contain validated row values, never SQL to execute. The source is committed
before the view; rows and the view cursor commit together. Replaying has no model,
tool, channel or filesystem side effects.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from core.execution.contracts import CONTRACT_VERSION
from core.execution.schema import ensure_event_schema
from .conversation_facts import VERSION, TABLES, conversation_fact, project_conversation_facts


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ConversationProjection:
    def __init__(self, view: sqlite3.Connection, path: Path):
        self.view = view
        self.stream = 'conversation:' + path.name
        directory = path.parent / "private_runtime" / "execution"
        directory.mkdir(parents=True, exist_ok=True)
        self.source = sqlite3.connect(directory / "events.sqlite3", check_same_thread=False)
        self.source.row_factory = sqlite3.Row
        try:
            self.source.execute("PRAGMA journal_mode=WAL")
            self.source.execute("PRAGMA synchronous=FULL")
            ensure_event_schema(self.source)
            self.source.executescript("""
                CREATE TABLE IF NOT EXISTS conversation_streams (
                    name TEXT PRIMARY KEY, root TEXT NOT NULL, version INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS conversation_event_stream ON events(kind,turn_id,seq);
                CREATE UNIQUE INDEX IF NOT EXISTS conversation_fact_id ON events(json_extract(payload,'$.fact_id'))
                    WHERE kind IN ('conversation.baseline', 'conversation.changed');
            """)
            view.execute("CREATE TABLE IF NOT EXISTS conversation_projection (id INTEGER PRIMARY KEY CHECK(id=1), root TEXT NOT NULL, version INTEGER NOT NULL, cursor INTEGER NOT NULL, digest TEXT NOT NULL)")
            view.execute('CREATE TABLE IF NOT EXISTS conversation_message_sources (message_id TEXT PRIMARY KEY, event_seq INTEGER NOT NULL)')
            view.execute("CREATE TEMP TABLE conversation_dirty (entity TEXT NOT NULL, identity TEXT NOT NULL, PRIMARY KEY(entity,identity))")
            self.columns = {}
            for table, key in TABLES.items():
                self.columns[table] = [r['name'] for r in view.execute(f'PRAGMA table_info({table})')]
                for action, ref in [('INSERT', 'new'), ('UPDATE', 'new'), ('DELETE', 'old')]:
                    view.execute(f"CREATE TEMP TRIGGER conversation_{table}_{action} AFTER {action} ON main.{table} BEGIN INSERT OR IGNORE INTO conversation_dirty VALUES ('{table}', {ref}.{key}); END")
            view.commit()
            view.execute('BEGIN IMMEDIATE')
            self.source.execute('BEGIN IMMEDIATE')
            try:
                self.source.execute('INSERT OR IGNORE INTO identity VALUES (1,?,?)', (uuid4().hex, CONTRACT_VERSION))
                identity = self.source.execute('SELECT * FROM identity WHERE id=1').fetchone()
                if identity['version'] != CONTRACT_VERSION:
                    raise RuntimeError('Unsupported Runtime Event Log version')
                self.root = identity['root_id']
                stream = self.source.execute('SELECT * FROM conversation_streams WHERE name=?', (self.stream,)).fetchone()
                if stream is None:
                    first = self.source.execute('SELECT * FROM events WHERE turn_id=? ORDER BY seq LIMIT 1', (self.stream,)).fetchone()
                    if first is not None:
                        if first['kind'] != 'conversation.baseline':
                            raise RuntimeError('Conversation baseline is missing')
                        self._decode(first)
                        self.source.execute('INSERT INTO conversation_streams VALUES (?,?,?)', (self.stream, self.root, VERSION))
                        stream = self.source.execute('SELECT * FROM conversation_streams WHERE name=?', (self.stream,)).fetchone()
                if stream is None:
                    if view.execute('SELECT 1 FROM conversation_projection').fetchone():
                        raise RuntimeError('Conversation source is missing; refusing to replace it with a view')
                    self.source.execute('INSERT INTO conversation_streams VALUES (?,?,?)', (self.stream, self.root, VERSION))
                    # All pages commit together; large histories need not become
                    # one unbounded Python object. No legacy execution is invented.
                    page: list[dict] = []
                    size = 0
                    wrote_page = False
                    for table, key in TABLES.items():
                        for record in view.execute(f'SELECT * FROM {table} ORDER BY {key}'):
                            fact = conversation_fact(table, record[key], dict(record))
                            page.append(fact)
                            size += len(encode(fact).encode('utf-8'))
                            if len(page) >= 256 or size >= 4 * 1024 * 1024:
                                self._insert('conversation.baseline', {'origin': 'legacy_snapshot', 'facts': page})
                                page, size, wrote_page = [], 0, True
                    if page or not wrote_page:
                        self._insert('conversation.baseline', {'origin': 'legacy_snapshot', 'facts': page})
                else:
                    if stream['version'] != VERSION or stream['root'] != self.root:
                        raise RuntimeError('Unsupported conversation source version')
                    self.root = stream['root']
                self.source.commit()
                self._after_baseline_commit()
                self.replay()
                view.commit()
            except BaseException:
                self.source.rollback()
                view.rollback()
                raise
        except BaseException:
            self.source.close()
            raise

    def _insert(self, kind: str, payload: dict) -> int:
        previous = self.source.execute('SELECT * FROM events WHERE turn_id=? ORDER BY seq DESC LIMIT 1', (self.stream,)).fetchone()
        data = {'stream': self.stream, 'root_id': self.root, 'kind': kind, 'projection_version': VERSION,
                'previous_cursor': previous['seq'] if previous else 0,
                'previous_digest': self._decode(previous)['digest'] if previous else '', **payload}
        checksum = hashlib.sha256(encode(data).encode('utf-8')).hexdigest()
        body = encode({'fact_id': uuid4().hex, 'digest': checksum, 'data': data})
        cursor = self.source.execute('INSERT INTO events(turn_id,run_id,epoch,version,kind,payload,created_at) VALUES (?,?,?,?,?,?,?)',
                                    (self.stream, self.stream, 'conversation', CONTRACT_VERSION, kind, body, datetime.now(timezone.utc).isoformat()))
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def _checkpoint(self, row):
        self.view.execute('INSERT OR REPLACE INTO conversation_projection VALUES (1,?,?,?,?)', (self.root, VERSION, row['seq'], self._decode(row)['digest']))

    def _decode(self, row):
        body = json.loads(row['payload'])
        data = body['data']
        if (row['version'] != CONTRACT_VERSION or data['projection_version'] != VERSION
                or data['stream'] != self.stream or data['root_id'] != self.root or data['kind'] != row['kind']
                or hashlib.sha256(encode(data).encode('utf-8')).hexdigest() != body['digest']):
            raise RuntimeError('Conversation fact version/source/digest mismatch')
        return body

    def _provenance(self, seq: int, changes):
        for table, identity, record in changes:
            if table == 'messages':
                self.view.execute('DELETE FROM conversation_message_sources WHERE message_id=?', (identity,))
                if record is not None:
                    self.view.execute('INSERT INTO conversation_message_sources VALUES (?,?)', (identity, seq))

    def _upsert(self, table, row):
        if set(row) != set(self.columns[table]):
            raise RuntimeError('Unsupported conversation row schema')
        columns = self.columns[table]
        key = TABLES[table]
        updates = ','.join(f'{c}=excluded.{c}' for c in columns if c != key)
        self.view.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) ON CONFLICT({key}) DO UPDATE SET {updates}", tuple(row[c] for c in columns))

    def _project_changes(self, facts, *, through_seq=None):
        sources = {}
        for fact in facts:
            ref = fact.get('content_source')
            if ref:
                if through_seq is not None and ref['seq'] >= through_seq:
                    raise ValueError('Conversation source is outside its committed prefix')
                row = self.source.execute('SELECT * FROM events WHERE seq=?', (ref['seq'],)).fetchone()
                if row is not None:
                    if row['version'] != CONTRACT_VERSION:
                        raise ValueError('Unsupported semantic source version')
                    sources[row['seq']] = {**dict(row), 'payload': json.loads(row['payload'])}
        return project_conversation_facts(facts, sources)

    def _bind_semantic_sources(self, changes, scope):
        for change in changes:
            if change['kind'] != 'message.recorded':
                continue
            data = change['data']
            previous = self.view.execute('SELECT event_seq FROM conversation_message_sources WHERE message_id=?', (change['id'],)).fetchone()
            if previous is not None:
                old_event = self.source.execute('SELECT * FROM events WHERE seq=?', (previous[0],)).fetchone()
                if old_event is None:
                    raise ValueError('Existing conversation source is missing')
                old_fact = next(f for f in self._decode(old_event)['data']['facts']
                                if f['kind'] == 'message.recorded' and f['id'] == change['id'])
                old_row = self._project_changes([old_fact])[0][2]
                if old_row['content'] != data['content']:
                    change['origin'] = 'conversation_edit'
                else:
                    # Metadata updates and full-view reconciliation cannot claim
                    # an old message for whatever execution happens to be active.
                    for key in ('interaction_turn_id', 'content_source', 'origin'):
                        if key in old_fact:
                            change[key] = old_fact[key]
                    if 'content_source' in change:
                        del data['content']
                continue
            turn_id = scope.turn_id if scope is not None else None
            if turn_id is None:
                # Cancellation/pending-write recovery happens after the execution
                # scope ended, possibly in a new process. Resolve its stable
                # admission key, never guess ownership by matching reply text.
                attributes = data.get('attributes')
                metadata = attributes.get('metadata') if isinstance(attributes, dict) else None
                metadata = metadata if isinstance(metadata, dict) else {}
                request_key = metadata.get('execution_request_id')
                if isinstance(request_key, str) and request_key:
                    source = metadata.get('source') or 'desktop'
                    source = source if isinstance(source, str) else 'desktop'
                    admitted = self.source.execute("""SELECT turn_id FROM events WHERE kind='request.accepted'
                        AND json_extract(payload,'$.identity.session_key')=?
                        AND json_extract(payload,'$.identity.source')=?
                        AND json_extract(payload,'$.identity.request_key')=?""",
                        (data['session_key'], source, request_key)).fetchone()
                    turn_id = admitted['turn_id'] if admitted else None
            if turn_id is None:
                continue
            rows = self.source.execute("SELECT * FROM events WHERE turn_id=? AND kind IN ('request.accepted','model.response.recorded') ORDER BY seq DESC", (turn_id,)).fetchall()
            if data['role'] == 'assistant' and any(r['kind'] == 'model.response.recorded' for r in rows):
                change['interaction_turn_id'] = turn_id
            # Presentation transforms (e.g. parsed proactive text) remain explicit
            # accepted presentation facts, not a second verdict on a raw response.
            change['origin'] = 'interaction_presentation'
            for row in rows:
                payload = json.loads(row['payload'])
                expected = None
                if data['role'] == 'assistant' and row['kind'] == 'model.response.recorded':
                    expected = payload.get('response', {}).get('content')
                elif data['role'] == 'user' and row['kind'] == 'request.accepted':
                    expected = payload['input'].get('content')
                if isinstance(expected, str) and expected == data.get('content'):
                    change['content_source'] = {'seq': row['seq'], 'turn_id': turn_id, 'kind': row['kind']}
                    if row['kind'] == 'model.response.recorded':
                        change['content_source']['call_id'] = payload['call_id']
                    del data['content']
                    break
        return changes

    def session_snapshot(self, session_key: str) -> dict:
        """Project committed history directly; the Session/UI cache is not input."""
        messages = {}
        links = {}
        previous, checksum = 0, ''
        rows = self.source.execute('SELECT * FROM events WHERE turn_id=? ORDER BY seq', (self.stream,)).fetchall()
        for event in rows:
            decoded = self._decode(event)
            body = decoded['data']
            if body['previous_cursor'] != previous or body['previous_digest'] != checksum:
                raise RuntimeError('Conversation event prefix is incomplete')
            facts = {f['id']: f for f in body['facts'] if f['kind'].startswith('message.')}
            for table, identity, record in self._project_changes(body['facts'], through_seq=event['seq']):
                if table != 'messages':
                    continue
                if record is None:
                    messages.pop(identity, None)
                    links.pop(identity, None)
                elif record['session_key'] == session_key:
                    fact = facts[identity]
                    # Metadata-only delivery updates keep the original ownership.
                    prior = links.get(identity, {}) if messages.get(identity, {}).get('content') == record['content'] else {}
                    messages[identity] = record
                    links[identity] = {'seq': event['seq'], 'turn_id': fact.get('interaction_turn_id') or
                                       fact.get('content_source', {}).get('turn_id') or prior.get('turn_id')}
            previous, checksum = event['seq'], decoded['digest']
        return {'root_id': self.root, 'stream': self.stream, 'through_seq': previous,
                'prefix_digest': checksum, 'rows': sorted(messages.values(), key=lambda m: m['seq']), 'links': links}

    def replay(self, *, rebuild: bool = False):
        """Caller holds the view write transaction, including its cursor update."""
        checkpoint = self.view.execute('SELECT * FROM conversation_projection WHERE id=1').fetchone()
        if checkpoint and (checkpoint['root'] != self.root or checkpoint['version'] != VERSION):
            raise RuntimeError('Conversation projection source/version mismatch')
        after = 0 if rebuild or checkpoint is None else checkpoint['cursor']
        if after:
            boundary = self.source.execute('SELECT * FROM events WHERE turn_id=? AND seq=?', (self.stream, after)).fetchone()
            if boundary is None or self._decode(boundary)['digest'] != checkpoint['digest']:
                raise RuntimeError('Conversation projection cursor is not a committed source boundary')
        else:
            self.view.execute('DELETE FROM messages')
            self.view.execute('DELETE FROM sessions')
            self.view.execute('DELETE FROM conversation_message_sources')
        previous_seq = after
        previous_digest = checkpoint['digest'] if after else ''
        for row in self.source.execute('SELECT * FROM events WHERE turn_id=? AND seq>? ORDER BY seq', (self.stream, after)):
            body = self._decode(row)['data']
            if body['previous_cursor'] != previous_seq or body['previous_digest'] != previous_digest:
                raise RuntimeError('Conversation event prefix is incomplete')
            changes = self._project_changes(body['facts'], through_seq=row['seq'])
            if row['kind'] == 'conversation.baseline':
                if after:
                    raise RuntimeError('Unexpected conversation baseline')
            elif row['kind'] != 'conversation.changed':
                raise RuntimeError('Unsupported conversation fact')
            for table, identity, _ in changes:
                self.view.execute(f'DELETE FROM {table} WHERE {TABLES[table]}=?', (identity,))
            for table, _, record in changes:
                if record is not None:
                    self._upsert(table, record)
            self._provenance(row['seq'], changes)
            self._checkpoint(row)
            previous_seq, previous_digest = row['seq'], self._decode(row)['digest']
        self.view.execute('DELETE FROM conversation_dirty')

    def commit(self):
        changes = []
        for dirty in self.view.execute('SELECT * FROM conversation_dirty ORDER BY entity,identity'):
            table = dirty['entity']
            row = self.view.execute(f'SELECT * FROM {table} WHERE {TABLES[table]}=?', (dirty['identity'],)).fetchone()
            changes.append(conversation_fact(table, dirty['identity'], dict(row) if row else None))
        if changes:
            from core.execution.scope import active_execution
            scope = active_execution()
            changes = self._bind_semantic_sources(changes, scope)
            projected = self._project_changes(changes)
            payload = {'facts': changes, 'execution_turn_id': scope.turn_id if scope else None,
                       'origin': 'accepted_conversation_change'}
            from core.memory.task_context import memory_intent
            intent = memory_intent.get()
            if intent is not None and scope is not None and getattr(scope.journal, 'memory_tasks_enabled', False) and any(
                f['kind'] == 'message.recorded' and f['data']['role'] == 'assistant'
                for f in changes
            ):
                # Admission intent and the original reply fact commit together.
                payload['memory_post_response'] = intent
            else:
                # Pending-write recovery has no active execution context. The
                # stable semantic owner resolves a prepared recipe; old history
                # and partial/interrupted replies have no recipe to promote.
                owners = {f.get('interaction_turn_id') or f.get('content_source', {}).get('turn_id')
                          for f in changes if f['kind'] == 'message.recorded' and f['data']['role'] == 'assistant'}
                owners.discard(None)
                if len(owners) == 1:
                    turn = owners.pop()
                    prepared = self.source.execute("SELECT payload FROM events WHERE turn_id=? AND kind='memory.post_response.prepared' ORDER BY seq DESC LIMIT 1", (turn,)).fetchone()
                    if prepared is not None:
                        payload['execution_turn_id'] = turn
                        payload['memory_post_response'] = json.loads(prepared['payload'])['input']
            # The wakeup can be lost after this commit. A startup scan discovers
            # Markdown work from the same durable reply fact as the UI message.
            markdown_intent = payload.get('memory_post_response') or intent
            if markdown_intent is not None and any(f['kind'] == 'message.recorded' and f['data']['role'] == 'assistant' for f in changes):
                payload['markdown_maintenance'] = {k: markdown_intent.get(k, '') for k in ('session_key', 'channel', 'chat_id')}
            with self.source:
                seq = self._insert('conversation.changed', payload)
            # Fault boundaries are intentionally separate for process crash tests.
            self._after_fact_commit()
            row = self.source.execute('SELECT * FROM events WHERE seq=?', (seq,)).fetchone()
            self._provenance(seq, projected)
            self._checkpoint(row)
            self.view.execute('DELETE FROM conversation_dirty')
        self._before_view_commit()
        self.view.commit()

    def _after_fact_commit(self):
        pass

    def _after_baseline_commit(self):
        pass

    def _before_view_commit(self):
        pass

    def close(self):
        self.source.close()
