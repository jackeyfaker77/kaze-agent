import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from agent.provider import LLMProvider
from core.execution.scope import ExecutionScope, current_execution
from core.memory.task_context import memory_intent, checkpoint_async
from memory2.durable_tasks import MemoryTasks
from memory2.memorizer import Memorizer
from memory2.post_response_worker import PostResponseMemoryWorker
from memory2.store import MemoryStore2
from session.manager import SessionManager


class Embedder:
    @checkpoint_async
    async def embed(self, text):
        return [1., 0.]


class Retriever:
    def __init__(self, store, old):
        self.store, self.old = store, old

    @checkpoint_async
    async def retrieve(self, *args, **kwargs):
        return [{**i, 'score': .99} for i in self.store.get_items_by_ids([self.old])]


async def setup(workspace, *, wire_hook=None):
    sessions = SessionManager(workspace)
    store = MemoryStore2(workspace / 'memory.db', vec_dim=2)
    old = store._db.execute("SELECT id FROM memory_items WHERE summary='old rule'").fetchone()
    old = old[0] if old else store.upsert_item('procedure', 'old rule', [1., 0.]).split(':')[1]
    provider = LLMProvider(api_key='test', provider_name='deepseek', max_retries=0)
    provider.execution_journal = sessions.execution_journal
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if wire_hook:
            wire_hook()
        prompt = payload['messages'][-1]['content']
        content = '["old rule"]' if prompt.startswith('判断用户消息') else json.dumps([old]) if prompt.startswith('用户明确表示') else '{"summary":"new preference"}'
        return httpx.Response(200, json={'id':'test','object':'chat.completion','created':0,'model':'test',
            'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':content}}]})

    await provider._client._client.aclose()
    provider._client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    memorizer = Memorizer(store, Embedder())
    async def implicit(**kwargs):
        response = await provider.chat(messages=[{'role':'user','content':'extract preference'}], tools=[], model='test', max_tokens=600)
        summary = json.loads(response.content)['summary']
        await memorizer.save_item_with_supersede(summary, 'preference', {}, kwargs['source_ref'])
    worker = PostResponseMemoryWorker(memorizer, Retriever(store, old), provider, 'test', implicit_memory_handler=implicit)
    jobs = MemoryTasks(store, sessions.execution_journal, worker)
    return SimpleNamespace(sessions=sessions, journal=sessions.execution_journal, store=store, provider=provider, calls=calls, jobs=jobs, old=old)


async def admit(f, *, key='desktop:memory', skip=False):
    row, _ = f.journal.admit(key, 'desktop', 'input', {'content':'old rule is wrong'})
    f.journal.start(row['turn_id'])
    scope = ExecutionScope(f.journal, row['turn_id'])
    token = current_execution.set(scope)
    intent = memory_intent.set(None if skip else {'session_key':key,'channel':'desktop','chat_id':'memory',
        'user_msg':'old rule is wrong','agent_response':'okay','tool_chain':[]})
    try:
        session = f.sessions.get_or_create(key)
        session.add_message('user', 'old rule is wrong')
        session.add_message('assistant', 'okay')
        await f.sessions.save_async(session)
        f.journal.finish(row['turn_id'], 'completed')
    finally:
        memory_intent.reset(intent)
        scope.active = False
        current_execution.reset(token)
    return row['turn_id']


async def close(f):
    await f.jobs.aclose()
    await f.provider._client.close()
    f.store.close()
    f.sessions._store.close()


async def test_fact_discovery_and_repeated_drain_never_repeat_models_or_memory(tmp_path):
    f = await setup(tmp_path)
    try:
        turn = await admit(f)
        await asyncio.gather(f.jobs.drain(), f.jobs.drain())
        assert len(f.calls) == 3
        tasks = f.jobs.list_tasks()['tasks']
        assert len(tasks) == 1 and tasks[0]['state'] == 'done' and tasks[0]['source_turn_id'] == turn
        assert f.store.get_items_by_ids([f.old])[0]['status'] == 'superseded'
        new = f.store._db.execute("SELECT reinforcement FROM memory_items WHERE summary='new preference'").fetchone()
        assert new == (1,)
        before = list(f.store._db.iterdump())
        await f.jobs.drain()
        assert list(f.store._db.iterdump()) == before and len(f.calls) == 3
        # A rebuilt discovery cursor must not interpret the original facts as
        # new work, even if model projection caches were also discarded.
        with f.store._db:
            f.store._db.execute('DELETE FROM memory_task_cursor')
        with f.journal._conn:
            f.journal._conn.execute('DELETE FROM model_context_cache')
        await f.jobs.drain()
        assert len(f.calls) == 3 and len(f.jobs.list_tasks()['tasks']) == 1
    finally:
        await close(f)
    f = await setup(tmp_path)
    try:
        await f.jobs.drain()
        assert not f.calls and f.jobs.list_tasks()['tasks'][0]['state'] == 'done'
    finally:
        await close(f)


async def test_old_history_and_skip_do_not_create_background_work(tmp_path):
    f = await setup(tmp_path)
    try:
        await admit(f, skip=True)
        await f.jobs.drain()
        assert not f.calls and not f.jobs.list_tasks()['tasks']
    finally:
        await close(f)


async def test_two_host_epochs_do_not_steal_live_task(tmp_path):
    first = await setup(tmp_path)
    second = await setup(tmp_path)
    gate, entered = asyncio.Event(), asyncio.Event()
    run = first.jobs.worker.run
    async def paused(**kwargs):
        entered.set()
        await gate.wait()
        await run(**kwargs)
    first.jobs.worker.run = paused
    try:
        await admit(first)
        running = asyncio.create_task(first.jobs.drain())
        await asyncio.wait_for(entered.wait(), 5)
        await second.jobs.drain()
        assert not second.calls
        assert second.jobs.list_tasks()['tasks'][0]['state'] == 'running'
        gate.set()
        await running
        await second.jobs.drain()
        assert len(first.calls) == 3 and not second.calls
        assert second.jobs.list_tasks()['tasks'][0]['state'] == 'done'
    finally:
        gate.set()
        await close(first)
        await close(second)


async def test_unknown_embedding_is_retained_and_never_resent(tmp_path):
    from core.memory.task_context import checkpoint_external
    from memory2.durable_tasks import Task, _frame
    f = await setup(tmp_path)
    calls = []
    class RemoteEmbedding:
        @checkpoint_external
        async def embed(self):
            calls.append(1)
            raise asyncio.CancelledError()
    from core.memory.task_context import memory_task, MemoryTaskBlocked
    await admit(f)
    f.jobs.discover()
    task = Task(f.jobs, {'id':f.jobs.list_tasks()['tasks'][0]['id']})
    token = memory_task.set(task)
    try:
        for expected in (asyncio.CancelledError, MemoryTaskBlocked):
            frame = _frame.set(('', [0]))
            try:
                with pytest.raises(expected):
                    await RemoteEmbedding().embed()
            finally:
                _frame.reset(frame)
        assert calls == [1]
    finally:
        memory_task.reset(token)
        await close(f)


async def test_checkpoint_identity_rejects_changed_arguments(tmp_path):
    from core.memory.task_context import memory_task, MemoryTaskBlocked
    from memory2.durable_tasks import Task, _frame
    f = await setup(tmp_path)
    await admit(f)
    f.jobs.discover()
    task = Task(f.jobs, {'id': f.jobs.list_tasks()['tasks'][0]['id']})
    token = memory_task.set(task)
    try:
        for summary in ('first', 'different'):
            frame = _frame.set(('', [0]))
            try:
                if summary == 'first':
                    f.store.upsert_item('preference', summary, [1., 0.])
                else:
                    with pytest.raises(MemoryTaskBlocked, match='checkpoint_input_changed'):
                        f.store.upsert_item('preference', summary, [1., 0.])
            finally:
                _frame.reset(frame)
        assert f.store._db.execute("SELECT count(*) FROM memory_items WHERE summary='different'").fetchone()[0] == 0
    finally:
        memory_task.reset(token)
        await close(f)
