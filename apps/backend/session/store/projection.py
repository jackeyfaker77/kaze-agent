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
            changes = project_conversation_facts(body['facts'])
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
            projected = project_conversation_facts(changes)
            payload = {'facts': changes, 'execution_turn_id': scope.turn_id if scope else None,
                       'origin': 'accepted_conversation_change'}
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
