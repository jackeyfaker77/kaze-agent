"""SessionManager 缓存、锁与基础入口。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..store import SessionStore

from .models import Session

class _ManagerCoreMixin:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.session_dir = workspace / "sessions"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = workspace / "sessions.db"
        self._store = SessionStore(self.db_path)
        from core.execution.journal import ExecutionJournal
        self.execution_journal = ExecutionJournal(workspace)
        self._store.execution_journal = self.execution_journal
        self._cache: dict[str, Session] = {}
        self._write_locks: dict[str, asyncio.Lock] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        if key not in self._write_locks:
            self._write_locks[key] = asyncio.Lock()
        return self._write_locks[key]

    def get_or_create(self, key: str) -> Session:
        if key in self._cache:
            self._cache[key].read_committed_history = lambda **kwargs: self._committed_model_history(key, **kwargs)
            return self._cache[key]

        session = self._load(key)
        if session is None:
            session = Session(key)
            self._ensure_session_meta(session)
        session.read_committed_history = lambda **kwargs: self._committed_model_history(key, **kwargs)
        self._cache[key] = session
        return session

    def _committed_model_history(self, key: str, *, max_messages: int = 500, start_index: int | None = None) -> list[dict]:
        from core.execution.scope import active_execution
        from core.execution.model_context import digest
        snapshot = self._store.committed_history_snapshot(key)
        cached = self._cache.get(key)
        if cached is not None:
            # A stale UI cache must not later be written back as an implicit edit
            # during this turn. Pending, not-yet-accepted messages keep their IDs.
            pending = [m for m in cached.messages if not m.get('id')]
            cached.messages = list(snapshot['messages']) + pending
        rows = snapshot['messages']
        if max_messages <= 0:
            rows = []
        elif start_index is None:
            rows = rows[-max_messages:]
        else:
            from .helpers import _align_to_user_boundary
            start = min(len(rows), max(0, int(start_index)))
            while 0 < start < len(rows) and rows[start]['role'] != 'user' and not rows[start].get('proactive'):
                start -= 1
            rows = rows[start:]
            if rows and not (rows[0]['role'] == 'user' or rows[0].get('proactive')):
                rows = _align_to_user_boundary(rows)
        history, seen, diagnostics, turns = [], set(), [], []
        for message in rows:
            turn = snapshot['links'].get(message['id'], {}).get('turn_id')
            if message['role'] == 'assistant' and turn and not message.get('proactive'):
                projected = self.execution_journal.interaction_history(turn)
                if projected['messages']:
                    if turn not in seen:
                        from .helpers import _truncate_tool_result
                        history.extend({**m, 'content': _truncate_tool_result(m.get('content', ''))}
                                       if m.get('role') == 'tool' else m for m in projected['messages'])
                        seen.add(turn)
                        turns.append(projected['source'])
                    diagnostics.extend(projected['diagnostics'])
                    continue
                diagnostics.extend(projected['diagnostics'])
                if projected['diagnostics']:
                    # A legacy UI tool_chain cannot fill a missing canonical tool
                    # result. Keep only accepted presentation text for this tail.
                    history.extend(Session(key=key, messages=[{**message, 'tool_chain': []}]).get_history())
                    turns.append(projected['source'])
                    continue
            # Historical/imported messages remain readable without invented model
            # invocations. Their accepted conversation facts are the legacy source.
            history.extend(Session(key=key, messages=[message]).get_history())
        scope = active_execution()
        if scope is not None:
            scope.record('model.history.selected', {
                'version': 1, 'session_key': key,
                'conversation_prefix': {k: snapshot[k] for k in ('root_id', 'stream', 'through_seq', 'prefix_digest')},
                'interaction_prefixes': turns, 'history_digest': digest(history),
                'diagnostics': diagnostics, 'legacy_policy': 'accepted_conversation_facts',
                'selection': {'max_messages': max_messages, 'start_index': start_index,
                              'tool_result_policy': 'existing_history_truncation'},
            })
        return history

    def peek_next_message_id(self, session_key: str) -> str:
        next_seq = self._store.next_seq(session_key)
        return f"{session_key}:{next_seq}"

    def invalidate(self, key: str) -> None:
        self._cache.pop(key, None)

    def list_sessions(self) -> list[dict[str, Any]]:
        sessions = self._store.list_sessions()
        for item in sessions:
            item["path"] = str(self.db_path)
        return sessions

    def get_channel_metadata(self, channel: str) -> list[dict[str, Any]]:
        try:
            return self._store.get_channel_metadata(channel)
        except Exception as e:
            logging.warning("Failed to read channel metadata for %s: %s", channel, e)
            return []
