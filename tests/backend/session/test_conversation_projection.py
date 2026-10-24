from __future__ import annotations

import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from session.manager import SessionManager
from session.store import SessionStore
from session.store.conversation_facts import project_conversation_facts
from core.execution.journal import ExecutionJournal


def snapshot(store):
    return {r['key']: (store.get_session_meta(r['key']), store.fetch_session_messages(r['key']), store.next_seq(r['key']))
            for r in store.list_sessions()}


def source_events(store):
    return store._projection.source.execute('SELECT seq,kind,payload FROM events ORDER BY seq').fetchall()


def add(store, key='desktop:test', seq=0, content='message'):
    return store.insert_message(key, role='assistant', content=content, ts='2026-10-25T00:00:00+08:00', seq=seq,
                                media=['kept.png'], tool_chain=[{'name': 'tool', 'result': 'saved'}],
                                extra={'thinking': 'retained', 'desktop_write_id': f'write-{seq}'})


def legacy_database(path):
    with closing(sqlite3.connect(path / 'sessions.db')) as db:
        db.executescript("""
            CREATE TABLE sessions(key TEXT PRIMARY KEY,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,last_consolidated INTEGER NOT NULL DEFAULT 0,metadata TEXT);
            CREATE TABLE messages(id TEXT PRIMARY KEY,session_key TEXT NOT NULL,seq INTEGER NOT NULL,role TEXT NOT NULL,content TEXT,tool_chain TEXT,extra TEXT,ts TEXT NOT NULL,UNIQUE(session_key,seq));
            INSERT INTO sessions VALUES ('telegram:old','2020-01-01','2020-01-02',3,'{"title":"legacy"}');
            INSERT INTO messages VALUES ('old-original-id','telegram:old',7,'user','old input',NULL,'{"custom":true}','2020-01-01');
            INSERT INTO messages VALUES ('old-reply-id','telegram:old',9,'assistant','old reply','[{"name":"old-tool"}]','{}','2020-01-02');
        """)
        db.commit()


def test_legacy_baseline_rebuild_keeps_ids_order_metadata_and_does_not_invent_runs(tmp_path):
    legacy_database(tmp_path)
    with closing(SessionStore(tmp_path / 'sessions.db')) as store:
        expected = snapshot(store)
        events = source_events(store)
        assert len(events) == 1 and events[0]['kind'] == 'conversation.baseline'
        body = json.loads(events[0]['payload'])['data']
        assert body['origin'] == 'legacy_snapshot'
        assert [m['id'] for m in expected['telegram:old'][1]] == ['old-original-id', 'old-reply-id']
        for _ in range(3):
            store.rebuild_projection()
            assert snapshot(store) == expected
            assert len(source_events(store)) == 1
    with closing(SessionStore(tmp_path / 'sessions.db')) as store:
        assert snapshot(store) == expected
    journal = ExecutionJournal(tmp_path)
    assert journal.list_executions('telegram:old')['executions'] == []
    journal.close()


def test_all_conversation_mutations_rebuild_and_deleted_messages_do_not_return(tmp_path):
    with closing(SessionStore(tmp_path / 'sessions.db')) as store:
        store.create_session(key='desktop:test', metadata={'title': 'first'})
        add(store, seq=2, content='retained search text')
        add(store, seq=5)
        add(store, seq=8)
        store.update_message('desktop:test:2', content='updated search text', delivery_status='sent', external_message_id='tg-42')
        store.replace_message_media(session_key='desktop:test', message_id='desktop:test:2', media_index=0, expected_path='kept.png', new_path='new.png')
        store.update_session('desktop:test', metadata={'title': 'renamed'})
        store.update_presence('desktop:test', last_user_at='2026-10-25', last_proactive_at='2026-10-24')
        store.delete_messages_batch(['desktop:test:5'])
        store.delete_session_messages_and_update_cursor('desktop:test', ids=['desktop:test:8'], last_consolidated=9)
        store.create_session(key='desktop:deleted')
        add(store, key='desktop:deleted')
        store.delete_session('desktop:deleted', cascade=True)
        expected = snapshot(store)
        count = len(source_events(store))
        for _ in range(3):
            store.rebuild_projection()
            assert snapshot(store) == expected
            assert store.next_seq('desktop:test') == 9
            assert store.get_message('desktop:test:5') is None
            assert not store.session_exists('desktop:deleted')
            assert store.search_message_previews('updated')[1] == 1
            assert len(source_events(store)) == count
            assert store._conn.execute('SELECT count(*) FROM conversation_message_sources').fetchone()[0] == 1


def test_lost_materialized_database_rebuilds_from_same_shared_event_log(tmp_path):
    with closing(SessionStore(tmp_path / 'sessions.db')) as store:
        store.create_session(key='desktop:test')
        add(store)
        expected = snapshot(store)
    (tmp_path / 'sessions.db').unlink()
    with closing(SessionStore(tmp_path / 'sessions.db')) as store:
        assert snapshot(store) == expected
        assert len([r for r in source_events(store) if r['kind'] == 'conversation.baseline']) == 1


@pytest.mark.parametrize('mode', ['baseline', 'fact', 'view', 'committed'])
def test_real_process_exit_replays_committed_facts_once(tmp_path, mode):
    if mode == 'baseline':
        legacy_database(tmp_path)
    root = Path(__file__).resolve().parents[3]
    env = {**os.environ, 'PYTHONPATH': str(root / 'apps/backend')}
    result = subprocess.run([sys.executable, str(root / 'tests/e2e/conversation_projection_crash.py'), str(tmp_path), mode],
                            env=env, capture_output=True, timeout=30)
    assert result.returncode == 73, result.stderr.decode(errors='replace')
    for _ in range(3):
        with closing(SessionStore(tmp_path / 'sessions.db')) as store:
            key = 'telegram:old' if mode == 'baseline' else 'desktop:crash'
            messages = store.fetch_session_messages(key)
            assert len(messages) == 2
            assert [m['content'] for m in messages] == (['old input', 'old reply'] if mode == 'baseline' else ['original input', 'saved partial reply'])
            expected = snapshot(store)
            count = len(source_events(store))
            store.rebuild_projection()
            assert snapshot(store) == expected and len(source_events(store)) == count


def test_fact_write_failure_rolls_back_view_and_does_not_claim_success(tmp_path):
    with closing(SessionStore(tmp_path / 'sessions.db')) as store:
        store.create_session(key='desktop:test')
        source = store._projection.source
        source.execute("CREATE TRIGGER reject_conversation BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'injected disk failure'); END")
        with pytest.raises(sqlite3.IntegrityError):
            add(store)
        assert store.count_messages('desktop:test') == 0
        source.execute('DROP TRIGGER reject_conversation')
        add(store)
        store.rebuild_projection()
        assert store.count_messages('desktop:test') == 1


@pytest.mark.parametrize('content', ['input', [{'type': 'text', 'text': 'input'}]])
def test_same_process_retry_after_fact_commit_reconciles_write_identity(tmp_path, monkeypatch, content):
    manager = SessionManager(tmp_path)
    try:
        session = manager.get_or_create('desktop:test')
        messages = [{'role': 'user', 'content': content}, {'role': 'assistant', 'content': 'reply'}]
        session.messages.extend(messages)
        projection = manager._store._projection
        def fail_once():
            # Fail only after accepting the first message, not metadata writes.
            if manager._store._conn.execute('SELECT count(*) FROM messages').fetchone()[0]:
                monkeypatch.setattr(projection, '_after_fact_commit', lambda: None)
                raise OSError('view unavailable after acceptance')
        monkeypatch.setattr(projection, '_after_fact_commit', fail_once)
        with pytest.raises(OSError):
            asyncio.run(manager.append_messages(session, messages))
        asyncio.run(manager.append_messages(session, messages))
        manager._store.rebuild_projection()
        saved = manager._store.fetch_session_messages(session.key)
        assert [m['content'] for m in saved] == [content if isinstance(content, str) else json.dumps(content), 'reply']
        assert len({m['session_write_id'] for m in saved}) == 2
    finally:
        manager._store.close()


def test_two_open_stores_share_one_fact_source_and_cursor(tmp_path):
    with closing(SessionStore(tmp_path / 'sessions.db')) as a, closing(SessionStore(tmp_path / 'sessions.db')) as b:
        a.create_session(key='desktop:test')
        add(a, seq=0)
        add(b, seq=1)
        b.update_message('desktop:test:0', content='changed from b')
        a.rebuild_projection()
        assert a.fetch_session_messages('desktop:test') == b.fetch_session_messages('desktop:test')
        assert a.count_messages('desktop:test') == 2


def test_bad_projection_version_or_fact_digest_never_serves_empty_success(tmp_path):
    with closing(SessionStore(tmp_path / 'sessions.db')) as store:
        store.create_session(key='desktop:test')
        add(store)
        store._conn.execute('UPDATE conversation_projection SET version=999')
        store._conn.commit()
        with pytest.raises(RuntimeError, match='version mismatch'):
            store.fetch_session_messages('desktop:test')
        store._conn.execute('UPDATE conversation_projection SET version=1')
        store._conn.commit()
        source = store._projection.source
        source.execute('DROP TRIGGER events_no_update')
        source.execute("UPDATE events SET payload=json_set(payload,'$.digest','bad') WHERE seq=(SELECT max(seq) FROM events)")
        source.commit()
        with pytest.raises(RuntimeError, match='digest mismatch'):
            store.rebuild_projection()
        assert store._conn.execute('SELECT count(*) FROM messages').fetchone()[0] == 1


def test_pure_projector_rejects_unknown_facts_instead_of_silently_losing_rows():
    with pytest.raises(ValueError, match='Unsupported'):
        project_conversation_facts([{'kind': 'future.fact', 'id': 'x'}])


def test_missing_source_is_not_replaced_with_stale_view(tmp_path):
    with closing(SessionStore(tmp_path / 'sessions.db')) as store:
        store.create_session(key='desktop:test')
        add(store)
    (tmp_path / 'private_runtime/execution/events.sqlite3').unlink()
    with pytest.raises(RuntimeError, match='source is missing'):
        SessionStore(tmp_path / 'sessions.db')


def test_large_legacy_migration_is_paged_and_rebuildable(tmp_path):
    legacy_database(tmp_path)
    with closing(sqlite3.connect(tmp_path / 'sessions.db')) as db:
        db.executemany("INSERT INTO messages VALUES (?, 'telegram:old', ?, 'assistant', ?, NULL, '{}', '2020-01-03')",
                       [(f'old:{seq}', seq, f'legacy {seq}') for seq in range(10, 610)])
        db.commit()
    with closing(SessionStore(tmp_path / 'sessions.db')) as store:
        events = source_events(store)
        assert len(events) == 3
        assert all(len(json.loads(e['payload'])['data']['facts']) <= 256 for e in events)
        expected = snapshot(store)
        store.rebuild_projection()
        assert snapshot(store) == expected
        assert store.count_messages('telegram:old') == 602


def test_deleted_source_prefix_is_detected_and_rebuild_is_atomic(tmp_path):
    with closing(SessionStore(tmp_path / 'sessions.db')) as store:
        store.create_session(key='desktop:test')
        add(store, seq=0)
        add(store, seq=1)
        source = store._projection.source
        source.execute('DROP TRIGGER events_no_delete')
        source.execute('DELETE FROM events WHERE seq=2')
        source.commit()
        with pytest.raises(RuntimeError, match='prefix is incomplete'):
            store.rebuild_projection()
        assert store._conn.execute('SELECT count(*) FROM messages').fetchone()[0] == 2


def test_cli_rebuild_requires_exclusive_host_ownership_and_no_config(tmp_path, capsys):
    from bootstrap.host_state import WorkspaceOwner
    from main import main
    with closing(SessionStore(tmp_path / 'sessions.db')) as store:
        store.create_session(key='desktop:test')
        add(store)
    owner = WorkspaceOwner(tmp_path, 'host')
    try:
        assert main(['conversation-rebuild', '--workspace', str(tmp_path)]) == 2
    finally:
        owner.close()
    assert main(['conversation-rebuild', '--workspace', str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)['rebuilt'] is True
    assert not (tmp_path / 'config.toml').exists()


def test_execution_and_conversation_facts_share_one_root_and_global_sequence(tmp_path):
    manager = SessionManager(tmp_path)
    try:
        manager.get_or_create('desktop:test')
        execution, _ = manager.execution_journal.admit('desktop:test', 'desktop', 'request', {'content': 'input'})
        manager.execution_journal.start(execution['turn_id'])
        from core.execution.scope import current_execution, ExecutionScope
        token = current_execution.set(ExecutionScope(manager.execution_journal, execution['turn_id']))
        try:
            add(manager._store)
        finally:
            current_execution.reset(token)
        events = source_events(manager._store)
        assert [e['seq'] for e in events] == list(range(1, len(events) + 1))
        last = json.loads(events[-1]['payload'])['data']
        assert last['execution_turn_id'] == execution['turn_id']
        assert manager._store._projection.root == manager.execution_journal.root_id
        manager._store.rebuild_projection()
        assert manager._store.count_messages('desktop:test') == 1
    finally:
        manager._store.close()


def test_stream_index_can_be_rederived_without_importing_history_twice(tmp_path):
    with closing(SessionStore(tmp_path / 'sessions.db')) as store:
        store.create_session(key='desktop:test')
        add(store)
        count = len(source_events(store))
        store._projection.source.execute('DELETE FROM conversation_streams')
        store._projection.source.commit()
    with closing(SessionStore(tmp_path / 'sessions.db')) as store:
        store.rebuild_projection()
        assert store.count_messages('desktop:test') == 1
        assert len(source_events(store)) == count


def test_save_retry_retains_accepted_message_id(tmp_path, monkeypatch):
    manager = SessionManager(tmp_path)
    try:
        session = manager.get_or_create('desktop:test')
        message = {'role': 'user', 'content': 'input'}
        session.messages.append(message)
        projection = manager._store._projection
        def fail_once():
            if manager._store._conn.execute('SELECT count(*) FROM messages').fetchone()[0]:
                monkeypatch.setattr(projection, '_after_fact_commit', lambda: None)
                raise OSError('view commit failed')
        monkeypatch.setattr(projection, '_after_fact_commit', fail_once)
        with pytest.raises(OSError):
            manager.save(session)
        manager.save(session)
        assert message['id'] == 'desktop:test:0'
        manager._store.rebuild_projection()
        assert [m['id'] for m in manager._store.fetch_session_messages(session.key)] == ['desktop:test:0']
    finally:
        manager._store.close()
