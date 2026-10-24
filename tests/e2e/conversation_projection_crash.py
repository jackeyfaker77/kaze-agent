"""Isolated hard-exit probes: no graceful flush or exception cleanup."""
import os
from pathlib import Path
import sys

from session.store import SessionStore
from session.store.projection import ConversationProjection

path, mode = Path(sys.argv[1]), sys.argv[2]
if mode == 'baseline':
    ConversationProjection._after_baseline_commit = lambda self: os._exit(73)
store = SessionStore(path / 'sessions.db')
if mode == 'baseline':
    raise AssertionError('Failpoint was not reached')
store.create_session(key='desktop:crash', metadata={'title': 'crash test'})
if mode == 'fact':
    store._projection._after_fact_commit = lambda: os._exit(73)
elif mode == 'view':
    store._projection._before_view_commit = lambda: os._exit(73)
rows = [dict(id=f'desktop:crash:{seq}', seq=seq, role=role, content=content,
             timestamp='2026-10-25T01:00:00+08:00', extra={'source': 'crash-test'})
        for seq, role, content in [(0, 'user', 'original input'), (1, 'assistant', 'saved partial reply')]]
store.replace_session_messages('desktop:crash', rows=rows,
                               updated_at='2026-10-25T01:00:00+08:00', last_consolidated=0, next_seq=2)
if mode == 'committed':
    os._exit(73)
raise AssertionError('Unknown crash boundary')
