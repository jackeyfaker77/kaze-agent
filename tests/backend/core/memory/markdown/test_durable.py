import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from agent.provider import LLMProvider
from bus.event_bus import EventBus
from core.execution.scope import ExecutionScope, current_execution
from core.memory.task_context import memory_intent
from core.memory.markdown.runtime import build_markdown_memory_runtime
from core.memory.markdown.durable import MarkdownTasks
from core.memory.markdown.contracts import ConsolidateRequest
from session.manager import SessionManager


async def setup(workspace, wire_hook=None, callback=False):
    sessions = SessionManager(workspace)
    provider = LLMProvider(api_key='test', provider_name='deepseek', max_retries=0)
    provider.execution_journal = sessions.execution_journal
    calls = []
    def respond(request):
        data = json.loads(request.content)
        calls.append(data)
        if wire_hook:
            wire_hook()
        if '长期记忆提取器' in data['messages'][0]['content']:
            content = '{"preference":[{"summary":"callback preference"}]}'
        elif '近期语境压缩代理' in data['messages'][0]['content']:
            content = '{"active_topics":["durable topic"]}'
        else:
            content = '{"history_entries":[{"summary":"[2026-10-28 12:00] durable history"}],"pending_items":[{"tag":"preference","content":"durable preference"}]}'
        return httpx.Response(200, json={'id':'test','object':'chat.completion','created':0,'model':'test',
            'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':content}}]})
    await provider._client._client.aclose()
    provider._client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    bus = EventBus()
    runtime = build_markdown_memory_runtime(workspace=workspace, provider=provider, model='test', keep_count=2, event_bus=bus)
    jobs = MarkdownTasks(runtime.maintenance, sessions)
    runtime.maintenance.durable = jobs
    f = SimpleNamespace(sessions=sessions, provider=provider, journal=sessions.execution_journal,
        jobs=jobs, runtime=runtime, calls=calls, bus=bus, callback_jobs=None)
    if callback:
        from memory2.store import MemoryStore2
        from memory2.memorizer import Memorizer
        from memory2.durable_tasks import MemoryTasks
        from core.memory.events import ConsolidationCommitted
        from plugins.default_memory.engine.lifecycle import DefaultMemoryEngine
        from tests.backend.memory2.test_durable_tasks import Embedder
        engine = DefaultMemoryEngine.__new__(DefaultMemoryEngine)
        engine._v2_store = MemoryStore2(workspace/'memory2.db', vec_dim=2)
        engine._memorizer = Memorizer(engine._v2_store, Embedder())
        engine._provider = provider
        engine._config = SimpleNamespace(model='test')
        engine.memory_tasks = MemoryTasks(engine._v2_store, f.journal, None)
        engine.memory_tasks.consolidation_handler = engine._apply_consolidation
        engine.memory_tasks.start = lambda: None  # Explicit drain controls crash boundaries.
        f.journal.memory_tasks_enabled = False
        f.callback_jobs, f.engine = engine.memory_tasks, engine
        bus.on(ConsolidationCommitted, engine._on_consolidation_committed)
    return f


async def admit(f, count=8, skip=False):
    key = 'desktop:markdown'
    session = f.sessions.get_or_create(key)
    for i in range(max(0, count - 2)):
        session.add_message('assistant' if i % 2 else 'user', f'message {i}')
    await f.sessions.save_async(session)
    row, _ = f.journal.admit(key, 'desktop', 'input', {'content':'archive input'})
    f.journal.start(row['turn_id'])
    scope = ExecutionScope(f.journal, row['turn_id'])
    token = current_execution.set(scope)
    intent = memory_intent.set(None if skip else {'session_key':key})
    try:
        for i in range(max(0, count - 2), count):
            session.add_message('assistant' if i % 2 else 'user', f'message {i}')
        await f.sessions.append_messages(session, list(session.messages[-2:]))
        f.journal.finish(row['turn_id'], 'completed')
    finally:
        memory_intent.reset(intent)
        current_execution.reset(token)
    return key


async def close(f):
    await f.jobs.aclose()
    if f.callback_jobs:
        await f.callback_jobs.aclose()
        f.engine._v2_store.close()
    await f.provider._client.close()
    f.sessions._store.close()


async def test_committed_fact_discovers_work_without_wakeup_and_replays_once(tmp_path):
    f = await setup(tmp_path)
    try:
        key = await admit(f)
        await asyncio.gather(f.jobs.drain(), f.jobs.drain())
        assert len(f.calls) == 2
        assert f.jobs.list_tasks()[0]['state'] == 'done'
        assert f.sessions._load(key).last_consolidated == 6
        before = {p.name:p.read_bytes() for p in f.runtime.store.memory_dir.rglob('*.md')}
        with f.jobs.db:
            f.jobs.db.execute('DELETE FROM cursor')
        with f.journal._conn:
            f.journal._conn.execute('DELETE FROM model_context_cache')
        await f.jobs.drain()
        assert len(f.calls) == 2 and len(f.jobs.list_tasks()) == 1
        assert before == {p.name:p.read_bytes() for p in f.runtime.store.memory_dir.rglob('*.md')}
    finally:
        await close(f)
    f = await setup(tmp_path)
    try:
        await f.jobs.drain()
        assert not f.calls
        assert f.runtime.store.read_history().count('durable history') == 1
        assert f.runtime.store.read_pending().count('durable preference') == 1
        assert f.runtime.store.read_recent_context().count('durable topic') == 1
    finally:
        await close(f)


async def test_recent_only_and_skipped_history_do_not_call_models(tmp_path):
    f = await setup(tmp_path)
    try:
        await admit(f, count=2)
        await f.jobs.drain()
        assert not f.calls and f.jobs.list_tasks()[0]['state'] == 'done'
        assert 'message 0' not in f.runtime.store.read_recent_context()  # keep_count=2 => latest one
        assert 'message 1' in f.runtime.store.read_recent_context()
    finally:
        await close(f)


async def test_legacy_history_manual_repeated_clicks_share_task(tmp_path):
    f = await setup(tmp_path)
    try:
        key = await admit(f, skip=True)
        await f.jobs.drain()
        assert not f.jobs.list_tasks()
        session = f.sessions.get_or_create(key)
        requests = [f.runtime.maintenance.consolidate(ConsolidateRequest(session, archive_all=True)) for _ in range(2)]
        results = await asyncio.gather(*requests)
        assert all(r.trace['mode'] == 'markdown' for r in results)
        await f.runtime.maintenance.consolidate(ConsolidateRequest(session, archive_all=True))
        assert len(f.jobs.list_tasks()) == 1 and len(f.calls) == 2
        assert f.runtime.store.read_history().count('durable history') == 1
    finally:
        await close(f)


async def test_two_owners_cannot_take_live_job(tmp_path):
    first = await setup(tmp_path)
    second = await setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original = first.jobs._execute
    async def pause(row):
        entered.set()
        await release.wait()
        await original(row)
    first.jobs._execute = pause
    try:
        await admit(first)
        running = asyncio.create_task(first.jobs.drain())
        await asyncio.wait_for(entered.wait(), 5)
        await second.jobs.drain()
        assert not second.calls and second.jobs.list_tasks()[0]['state'] == 'running'
        release.set()
        await running
        await second.jobs.drain()
        assert not second.calls and len(first.calls) == 2
    finally:
        release.set()
        await close(first)
        await close(second)


async def test_deleted_source_is_blocked_not_recreated(tmp_path):
    f = await setup(tmp_path)
    try:
        key = await admit(f)
        f.jobs.discover()
        f.sessions._store.delete_session(key, cascade=True)
        await f.jobs.drain()
        assert f.jobs.list_tasks()[0]['error'] == 'source_session_deleted'
        assert not f.calls and not f.sessions._store.session_exists(key)
    finally:
        await close(f)


async def test_consolidation_callback_uses_persistent_memory_receipts(tmp_path):
    f = await setup(tmp_path, callback=True)
    try:
        await admit(f)
        await f.jobs.drain()
        await f.callback_jobs.drain()
        assert len(f.calls) == 3
        assert f.callback_jobs.list_tasks()['tasks'][0]['state'] == 'done'
        items = f.engine._v2_store.list_items_for_admin()[0]
        assert len(items) == 2 and all(i['reinforcement'] == 1 for i in items)
        await f.callback_jobs.drain()
        assert len(f.calls) == 3
    finally:
        await close(f)


async def test_unknown_file_change_is_not_overwritten(tmp_path, monkeypatch):
    from core.memory.markdown import durable
    f = await setup(tmp_path)
    try:
        await admit(f)
        def edit(point):
            if point == 'file_intent':
                f.runtime.store.history_file.write_text('user edited', encoding='utf-8')
        monkeypatch.setattr(durable, 'fault', edit)
        await f.jobs.drain()
        # The concurrent change is detected before replacing the file.
        assert f.jobs.list_tasks()[0]['state'] == 'blocked'
        assert f.runtime.store.history_file.read_text(encoding='utf-8') == 'user edited'
    finally:
        await close(f)


async def test_completed_file_receipt_does_not_resurrect_consumed_pending(tmp_path, monkeypatch):
    from core.memory.markdown import durable
    class Crash(BaseException):
        pass
    def crash(point):
        if point == 'before_done':
            raise Crash()
    f = await setup(tmp_path)
    try:
        await admit(f)
        monkeypatch.setattr(durable, 'fault', crash)
        with pytest.raises(Crash):
            await f.jobs.drain()
        f.runtime.store.clear_pending()
    finally:
        await close(f)

    monkeypatch.setattr(durable, 'fault', lambda _: None)
    f = await setup(tmp_path)
    try:
        await f.jobs.drain()
        assert not f.calls and f.jobs.list_tasks()[0]['state'] == 'done'
        assert 'durable preference' not in f.runtime.store.read_pending()
    finally:
        await close(f)


async def test_summary_edited_during_model_call_is_preserved(tmp_path):
    def edit():
        (tmp_path/'memory/RECENT_CONTEXT.md').write_text('user new summary', encoding='utf-8')
    f = await setup(tmp_path, wire_hook=edit)
    try:
        await admit(f)
        await f.jobs.drain()
        assert f.jobs.list_tasks()[0]['error'] == 'markdown_file_conflict:RECENT_CONTEXT.md'
        assert f.runtime.store.read_recent_context() == 'user new summary'
        await f.jobs.drain()
        assert len(f.calls) == 2
    finally:
        await close(f)


@pytest.mark.parametrize('edit_content', [False, True])
async def test_delivery_receipt_during_model_does_not_change_source(tmp_path, edit_content):
    def deliver():
        message = f.sessions._load(key).messages[-1]
        f.sessions._store.update_message(message['id'], delivery_status='sent',
            external_message_id='telegram:123', **({'content': 'edited'} if edit_content else {}))
    f = await setup(tmp_path, wire_hook=deliver)
    try:
        key = await admit(f)
        await f.jobs.drain()
        task = f.jobs.list_tasks()[0]
        assert task['state'] == ('blocked' if edit_content else 'done')
        if edit_content:
            assert task['error'] == 'source_history_changed'
        await f.jobs.drain()
        assert len(f.calls) == 2
        assert f.runtime.store.read_history().count('durable history') == (0 if edit_content else 1)
    finally:
        await close(f)


@pytest.mark.parametrize('reason', ['source_history_changed', 'model_outcome_unknown'])
async def test_old_receipt_conflict_recovers_saved_draft_only(tmp_path, monkeypatch, reason):
    from core.memory.markdown import durable
    class Crash(BaseException):
        pass
    def crash(point):
        if point == 'draft':
            raise Crash()
    f = await setup(tmp_path)
    try:
        key = await admit(f)
        monkeypatch.setattr(durable, 'fault', crash)
        with pytest.raises(Crash):
            await f.jobs.drain()
        assert len(f.calls) == 2
        message = f.sessions._load(key).messages[-1]
        f.sessions._store.update_message(message['id'], delivery_status='sent', external_message_id='123')
        with f.jobs.db:
            f.jobs.db.execute("UPDATE tasks SET state='blocked',error=?", (reason,))
    finally:
        await close(f)
    monkeypatch.setattr(durable, 'fault', lambda _: None)
    f = await setup(tmp_path)
    try:
        for _ in range(2):
            await f.jobs.drain()
        assert not f.calls
        assert f.jobs.list_tasks()[0]['state'] == ('done' if reason == 'source_history_changed' else 'blocked')
        assert f.runtime.store.read_history().count('durable history') == (1 if reason == 'source_history_changed' else 0)
    finally:
        await close(f)
