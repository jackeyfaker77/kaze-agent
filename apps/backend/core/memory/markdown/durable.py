"""Durable Markdown work. Conversation facts admit work; this DB owns receipts.

Model responses remain in the execution log. Files are committed using atomic
replacement; a saved draft is replayed without re-running its models. Unknown
model outcomes are blocked, never treated as permission to retry.
"""
from __future__ import annotations

import asyncio
from contextvars import Context
from dataclasses import asdict
import hashlib
import json
import logging
import sqlite3
import re
from types import SimpleNamespace
from typing import Any, cast

from core.execution.lease import EpochLease
from core.execution.model_context import digest
from core.memory.task_context import MemoryTaskBlocked, memory_task
from memory2.durable_tasks import Task
from .consolidation import _MarkdownConsolidationWorker
from .contracts import ConsolidateResult, _ConsolidationDraft, _ConsolidationFailure, _ConsolidationWindow

logger = logging.getLogger(__name__)


def fault(point):
    """Process-test boundary; no effect in production."""


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _same_source(current, snapshot):
    # Delivery receipts are transport facts, updated after the conversation is
    # committed. They are not Markdown source content. Keep every other field
    # (including identity, order, tools and user edits) in the comparison.
    def semantic(message):
        return {k: v for k, v in message.items()
                if k not in ('delivery_status', 'external_message_id')}
    return ([semantic(m) for m in current[:len(snapshot)]] ==
            [semantic(m) for m in snapshot])


class _Models(Task):
    def __init__(self, owner, row):
        self.owner, self.row = owner, row
        self.counter = 0

    def path(self, kind):
        self.counter += 1
        return f'/markdown-v1/{self.counter}:{kind}'


class MarkdownTasks:
    VERSION = 1

    def __init__(self, maintenance, manager):
        self.maintenance, self.manager = maintenance, manager
        self.journal = manager.execution_journal
        self.db = sqlite3.connect(maintenance._store.memory_dir / 'markdown_tasks.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS cursor(root TEXT PRIMARY KEY, seq INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks(
                id TEXT PRIMARY KEY, root TEXT NOT NULL, source_seq INTEGER NOT NULL,
                source_turn TEXT, session_key TEXT NOT NULL, version INTEGER NOT NULL,
                state TEXT NOT NULL, snapshot TEXT, draft TEXT, owner TEXT, error TEXT,
                archive_all INTEGER NOT NULL DEFAULT 0, force INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS file_writes(job TEXT, name TEXT, before_hash TEXT,
                content BLOB NOT NULL, done INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(job,name));
        ''')
        self.task = None
        self.closed = False
        self.journal.markdown_tasks_enabled = True

    def discover(self):
        row = self.db.execute('SELECT seq FROM cursor WHERE root=?', (self.journal.root_id,)).fetchone()
        after = row[0] if row else 0
        events = self.journal._conn.execute("SELECT seq,payload FROM events WHERE kind='conversation.changed' AND seq>? ORDER BY seq LIMIT 128", (after,)).fetchall()
        if not events:
            return False
        with self.db:
            for event in events:
                envelope = json.loads(event['payload'])
                data = envelope['data']
                intent = data.get('markdown_maintenance')
                if not intent:
                    continue
                from session.store.projection import encode as encode_fact
                if hashlib.sha256(encode_fact(data).encode()).hexdigest() != envelope['digest'] or data['root_id'] != self.journal.root_id:
                    raise MemoryTaskBlocked('markdown_source_fact_corrupt')
                self._insert(intent['session_key'], event['seq'], data.get('execution_turn_id'))
            self.db.execute('INSERT OR REPLACE INTO cursor VALUES (?,?)', (self.journal.root_id, events[-1]['seq']))
            fault('before_admission_commit')
        fault('admitted')
        return True

    def _insert(self, session_key, source_seq, source_turn, *, archive_all=False, force=False, identity=None):
        identity = identity or digest([self.journal.root_id, session_key, source_turn or source_seq, bool(archive_all), bool(force), self.VERSION])
        self.db.execute('''INSERT OR IGNORE INTO tasks
            (id,root,source_seq,source_turn,session_key,version,state,archive_all,force)
            VALUES (?,?,?,?,?,?,'pending',?,?)''',
            (identity, self.journal.root_id, source_seq, source_turn, session_key, self.VERSION, archive_all, force))
        return identity

    def request(self, session_key, *, archive_all=False, force=False):
        # Manual requests reference the latest committed prefix, never a mutable
        # UI cache. Admission commits before any background task is scheduled.
        seq = self.journal._conn.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        session = self.manager._load(session_key)
        identity = digest(['manual-markdown-v1', self.journal.root_id, session_key,
                           session.messages if session else [], bool(archive_all), bool(force)])
        with self.db:
            return self._insert(session_key, seq, None, archive_all=archive_all, force=force, identity=identity)

    def failure(self, key):
        row = self.db.execute("SELECT error FROM tasks WHERE root=? AND state='blocked' ORDER BY rowid LIMIT 1", (self.journal.root_id,)).fetchone()
        return row[0] if row else None

    def list_tasks(self, session_key='', limit=50):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('Invalid Markdown task limit')
        return [dict(row) for row in self.db.execute('''SELECT id,source_seq,source_turn,session_key,state,error
            FROM tasks WHERE root=? AND (?='' OR session_key=?) ORDER BY rowid DESC LIMIT ?''',
            (self.journal.root_id, session_key, session_key, limit))]

    def start(self):
        if not self.closed and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self.run(), name='markdown_tasks', context=Context())

    async def run(self):
        while not self.closed:
            try:
                await self.drain()
            except Exception:
                logger.exception('Markdown task recovery failed; persisted work retained')
            await asyncio.sleep(1)  # Local DB scan only, no proactive/model polling.

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.db.close()

    async def drain(self):
        async with self.maintenance._global_write_lock:
            while self.discover():
                pass
            # All sessions share Markdown files. An unresolved earlier write
            # blocks later work, so a stale summary cannot overwrite a newer one.
            rows = self.db.execute("SELECT * FROM tasks WHERE root=? AND state!='done' ORDER BY rowid", (self.journal.root_id,)).fetchall()
            for raw in rows:
                row = dict(raw)
                if row['state'] == 'blocked':
                    # Repair only the old false conflict caused by late delivery
                    # receipts. Unknown models, real edits and file conflicts
                    # remain blocked; saved model/draft/file receipts are reused.
                    snapshot = json.loads(row['snapshot']) if row['snapshot'] else None
                    current = self.manager._load(row['session_key'])
                    if (row['error'] != 'source_history_changed' or not snapshot or
                            current is None or
                            current.messages[:len(snapshot['messages'])] == snapshot['messages'] or
                            not _same_source(current.messages, snapshot['messages'])):
                        break
                if row['state'] == 'running':
                    if row['owner'] == self.journal.epoch:
                        break
                    try:
                        lease = EpochLease(self.journal.directory / (row['owner'] + '.lock'))
                    except OSError:
                        break
                    lease.close()
                with self.db:
                    claimed = self.db.execute("UPDATE tasks SET state='running',owner=? WHERE id=? AND state=? AND owner IS ?", (self.journal.epoch, row['id'], row['state'], row['owner'])).rowcount
                if not claimed:
                    break
                try:
                    await self._execute(row)
                    fault('before_done')
                    with self.db:
                        self.db.execute("UPDATE tasks SET state='done',error=NULL WHERE id=?", (row['id'],))
                    fault('done')
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, MemoryTaskBlocked) else type(exc).__name__
                    with self.db:
                        self.db.execute("UPDATE tasks SET state='blocked',error=? WHERE id=?", (reason, row['id']))
                    logger.warning('Markdown task blocked: id=%s reason=%s', row['id'], reason)
                    break

    async def _execute(self, row):
        if row['version'] != self.VERSION:
            raise MemoryTaskBlocked('unsupported_markdown_workflow')
        m = self.maintenance
        current = self.manager._load(row['session_key'])
        if current is None:
            raise MemoryTaskBlocked('source_session_deleted')
        if row['snapshot'] is None:
            fact = self.journal._conn.execute('SELECT payload FROM events WHERE seq=?', (row['source_seq'],)).fetchone()
            origin = json.loads(fact[0]).get('data', {}).get('markdown_maintenance', {}) if fact else {}
            if origin.get('session_key') != current.key:
                channel, _, chat_id = current.key.partition(':')
                origin = {'channel': channel, 'chat_id': chat_id}
            snapshot: dict[str, Any] = dict(key=current.key, messages=current.messages, metadata=current.metadata,
                last_consolidated=current.last_consolidated, keep_count=m._keep_count,
                _channel=origin.get('channel', ''), _chat_id=origin.get('chat_id', ''),
                long_term=m._store.read_long_term(), history=m._store.read_history(16000),
                recent=m._store.read_recent_context(), model=m._worker._model,
                recent_hash=hashlib.sha256(m._store.recent_context_file.read_bytes()).hexdigest(),
                recent_model=m._worker._recent_context_model)
            with self.db:
                self.db.execute('UPDATE tasks SET snapshot=? WHERE id=?', (encode(snapshot), row['id']))
            fault('snapshot')
        else:
            snapshot = json.loads(row['snapshot'])
        if not _same_source(current.messages, snapshot['messages']):
            raise MemoryTaskBlocked('source_history_changed')
        session = SimpleNamespace(**snapshot)
        frozen = SimpleNamespace(read_long_term=lambda: snapshot['long_term'], read_history=lambda *_: snapshot['history'], read_recent_context=lambda: snapshot['recent'])
        worker = _MarkdownConsolidationWorker(profile_maint=cast(Any, frozen), provider=m._worker._provider,
            model=snapshot['model'], keep_count=snapshot['keep_count'],
            recent_context_provider=m._worker._recent_context_provider, recent_context_model=snapshot['recent_model'])
        if row['draft'] is not None:
            saved: dict[str, Any] = json.loads(row['draft'])
        else:
            token = memory_task.set(_Models(self, row))
            try:
                draft = await worker.prepare_consolidation(session, archive_all=bool(row['archive_all']), force=bool(row['force']))
            finally:
                memory_task.reset(token)
            if isinstance(draft, _ConsolidationFailure):
                raise MemoryTaskBlocked(draft.step + ':' + draft.error)
            if draft is None:
                from .recent_context import _replace_recent_turns_block, _format_recent_context_messages, _recent_turn_count
                from .formatting import _is_nsfw_memory_enabled_session
                count = _recent_turn_count(snapshot['keep_count']) if snapshot['keep_count'] > 0 else 0
                rendered = _format_recent_context_messages(session.messages[-count:] if count else [], nsfw_memory_enabled=_is_nsfw_memory_enabled_session(session))
                saved = {'recent_only': _replace_recent_turns_block(snapshot['recent'], rendered)}
            else:
                saved = asdict(draft)
                # Session identity avoids collisions in legacy histories without IDs.
                saved['source_ref'] = 'markdown-task:' + row['id']
            with self.db:
                self.db.execute('UPDATE tasks SET draft=? WHERE id=?', (encode(saved), row['id']))
            fault('draft')
        # Recheck after model awaits; user edits/deletion must not be resurrected.
        current = self.manager._load(row['session_key'])
        if current is None or not _same_source(current.messages, snapshot['messages']):
            raise MemoryTaskBlocked('source_history_changed')
        if 'recent_only' in saved:
            self._write_file(row['id'], m._store.recent_context_file, saved['recent_only'], append=False, expected_before=snapshot['recent_hash'])
            fault('recent')
            return
        draft = _ConsolidationDraft(**{**saved, 'window': _ConsolidationWindow(**saved['window'])})
        self._commit_files(row['id'], draft, snapshot['recent_hash'])
        fault('files')
        # Update ONLY the cursor; never save a stale session snapshot over a
        # foreground turn. SessionStore commits this metadata as a conversation fact.
        target = max(current.last_consolidated, draft.window.consolidate_up_to)
        if current.last_consolidated != target:
            self.manager._store.update_last_consolidated(current.key, target)
        cached = self.manager._cache.get(current.key)
        if cached is not None:
            cached.last_consolidated = target
        fault('cursor')
        if m._event_bus is not None:
            from core.memory.events import ConsolidationCommitted
            await m._event_bus.emit(ConsolidationCommitted(
                history_entry_payloads=draft.history_entry_payloads, source_ref=draft.source_ref,
                scope_channel=draft.scope_channel, scope_chat_id=draft.scope_chat_id,
                conversation=draft.conversation, task_id=row['id'], session_key=row['session_key'],
                source_seq=row['source_seq'], source_turn=row['source_turn']))
        fault('callback')
        if m._after_consolidation is not None:
            raise MemoryTaskBlocked('unregistered_after_consolidation_hook')

    def _write_file(self, job, path, text, *, append, kind='', expected_before=None):
        store = self.maintenance._store
        name = path.relative_to(store.memory_dir).as_posix()
        row = self.db.execute('SELECT * FROM file_writes WHERE job=? AND name=?', (job, name)).fetchone()
        if row is not None and row['done']:
            # A completed write is not undone by a later optimizer/user edit.
            return
        current = path.read_bytes() if path.exists() else b''
        if row is None:
            if expected_before is not None and hashlib.sha256(current).hexdigest() != expected_before:
                raise MemoryTaskBlocked('markdown_file_conflict:' + name)
            if append:
                marker = store._consolidation_marker('markdown-task:' + job, kind)
                prefix = ('# ' + path.stem + '\n\n').encode('utf-8') if not current and path.parent == store.journal_dir else current
                content = prefix + (marker + '\n' + text.rstrip() + '\n\n').encode('utf-8')
            else:
                from core.memory.markdown_schema import normalize_memory_document
                content = normalize_memory_document(path.name, text).encode('utf-8')
            with self.db:
                self.db.execute('INSERT INTO file_writes VALUES (?,?,?,?,0)',
                    (job, name, hashlib.sha256(current).hexdigest(), content))
            row = self.db.execute('SELECT * FROM file_writes WHERE job=? AND name=?', (job, name)).fetchone()
            fault('file_intent')
        content = row['content']
        current = path.read_bytes() if path.exists() else b''
        if current != content:
            if hashlib.sha256(current).hexdigest() != row['before_hash']:
                raise MemoryTaskBlocked('markdown_file_conflict:' + name)
            store._atomic_write(path, content)
        fault('file_applied:' + name)
        with self.db:
            self.db.execute('UPDATE file_writes SET done=1 WHERE job=? AND name=?', (job, name))

    def _commit_files(self, job, draft, recent_hash):
        store = self.maintenance._store
        entries = [entry for entry, _ in draft.history_entry_payloads]
        if entries:
            self._write_file(job, store.history_file, '\n'.join(entries), append=True, kind='history_entry')
        if draft.pending_items:
            self._write_file(job, store.pending_file, draft.pending_items, append=True, kind='pending_items')
        self._write_file(job, store.recent_context_file, draft.recent_context_text, append=False, expected_before=recent_hash)
        dates = {}
        for entry in entries:
            match = re.match(r'^\[(\d{4}-\d{2}-\d{2})', entry)
            if match:
                dates.setdefault(match[1], []).append(entry)
        for date, rows in dates.items():
            path = store.journal_dir / (date + '.md')
            self._write_file(job, path, '\n'.join(rows), append=True, kind='journal:' + date)

    async def consolidate(self, request):
        identity = self.request(request.session.key, archive_all=request.archive_all, force=request.force)
        # Running the durable job must not inherit a foreground execution scope.
        await asyncio.create_task(self.drain(), context=Context())
        row = self.db.execute('SELECT state,error,draft FROM tasks WHERE id=?', (identity,)).fetchone()
        if row['state'] != 'done':
            return ConsolidateResult(trace={'mode': 'failed', 'error': row['error'] or 'markdown_queue_blocked'})
        draft = json.loads(row['draft'])
        request.session.last_consolidated = self.manager._load(request.session.key).last_consolidated
        return ConsolidateResult(consolidated_count=len(draft.get('window', {}).get('old_messages', [])),
            trace={'mode': 'skipped' if 'recent_only' in draft else 'markdown', 'task_id': identity})
