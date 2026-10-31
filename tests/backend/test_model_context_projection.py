"""Acceptance of the production request boundary and its authoritative sources."""
from contextlib import contextmanager
from copy import deepcopy
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from agent.provider import LLMProvider
from agent.tool_runtime import append_assistant_tool_calls, append_tool_result
from core.execution.contracts import ExecutionNeedsReview, JournalWriteError
from core.execution.journal import ExecutionJournal
from core.execution.model_context import ModelContextError, project_model_context
from core.execution.model_recording import history_chat
from core.execution.scope import ExecutionScope, current_execution
from session.manager import SessionManager


@contextmanager
def running(journal, key='desktop:context', source='desktop'):
    row, _ = journal.admit(key, source, 'request', {'content': 'original input'})
    journal.start(row['turn_id'])
    scope = ExecutionScope(journal, row['turn_id'])
    token = current_execution.set(scope)
    try:
        yield scope
    finally:
        scope.active = False
        current_execution.reset(token)


async def provider_with_wire(responses, *, journal=None, provider_name='openai'):
    provider = LLMProvider(api_key='never-archive-this-secret', provider_name=provider_name, max_retries=0)
    if journal is not None:
        provider.execution_journal = journal
    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        response = dict(responses[min(len(requests) - 1, len(responses) - 1)])
        finish = response.pop('finish_reason', 'tool_calls' if response.get('tool_calls') else 'stop')
        return httpx.Response(200, json={'id': 'test', 'object': 'chat.completion', 'created': 0,
            'model': 'test', 'choices': [{'index': 0, 'finish_reason': finish,
                                         'message': {'role': 'assistant', **response}}]})
    await provider._client._client.aclose()
    provider._client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    return provider, requests


async def test_hard_standard_cache_deleted_live_inputs_changed_same_prefix_same_wire(tmp_path):
    journal = ExecutionJournal(tmp_path)
    prompt, skill, config = (tmp_path / name for name in ('prompt.txt', 'skill.txt', 'test-config.json'))
    prompt.write_text('original system', encoding='utf-8')
    skill.write_text('original skill', encoding='utf-8')
    config.write_text('{"model":"old-model","max_tokens":71}', encoding='utf-8')
    provider, wire = await provider_with_wire([{'content': 'answer'}], journal=journal)
    async def send():
        settings = json.loads(config.read_text())
        return await history_chat(provider, messages=[{'role': 'system', 'content': prompt.read_text() + skill.read_text()},
             {'role': 'user', 'content': 'original input'}], tools=[], **settings)
    try:
        with running(journal) as scope:
            await send()
            first = journal.model_context(scope.turn_id)['calls'][0]
            original = journal.model_context(scope.turn_id, call_id=first['call_id'])
            assert original['request'] == wire[0]  # SDK consumed the projection.
            prompt.write_text('NEW SYSTEM', encoding='utf-8')
            skill.write_text('NEW SKILL', encoding='utf-8')
            config.write_text('{"model":"new-model","max_tokens":19}', encoding='utf-8')
            await send()
            assert wire[1] != wire[0]
            assert journal.model_context(scope.turn_id, through_seq=original['through_seq'])['calls'] == [first]
            journal.finish(scope.turn_id, 'completed')
        with journal._conn:
            journal._conn.execute('DROP TABLE model_context_cache')
        turn = scope.turn_id
        journal.close()
        journal = ExecutionJournal(tmp_path)
        assert journal._conn.execute('SELECT COUNT(*) FROM model_context_cache').fetchone()[0] == 0
        for _ in range(3):
            journal.rebuild_projection()
            rebuilt = journal.model_context(turn, call_id=first['call_id'], through_seq=original['through_seq'],
                                            expected_digest=original['prefix_digest'])
            assert rebuilt == original
            assert rebuilt['request'] == wire[0]
        assert len(wire) == 2
        assert 'never-archive-this-secret' not in json.dumps(journal.events(turn, limit=500))
    finally:
        await provider._client.close()
        journal.close()


async def test_ui_and_model_history_share_semantic_facts_not_mutable_cache(tmp_path):
    sessions = SessionManager(tmp_path)
    journal = sessions.execution_journal
    call = {'id': 'same-id', 'type': 'function', 'function': {'name': 'read', 'arguments': '{}'}}
    provider, wire = await provider_with_wire([
        {'content': 'checking', 'reasoning_content': 'native reasoning', 'tool_calls': [call]},
        {'content': 'final answer', 'reasoning_content': 'native final reasoning'},
    ], provider_name='deepseek')
    try:
        with running(journal) as scope:
            messages = [{'role': 'user', 'content': 'original input'}]
            response = await history_chat(provider, messages=messages, tools=[], model='deepseek-chat', max_tokens=50)
            append_assistant_tool_calls(messages, content=response.content, tool_calls=response.tool_calls,
                                        provider_fields=response.provider_fields)
            async def read():
                return 'tool result'
            result = await scope.effect('tool', {'name': 'read', 'retry_policy': 'read_only'}, read)
            append_tool_result(messages, tool_call_id='same-id', content=result)
            await history_chat(provider, messages=messages, tools=[], model='deepseek-chat', max_tokens=50)
            session = sessions.get_or_create('desktop:context')
            session.add_message('user', 'original input')
            # Intentionally no UI tool_chain: canonical model steps own history.
            session.add_message('assistant', 'final answer')
            sessions.save(session)
            journal.finish(scope.turn_id, 'completed')
        facts = [json.loads(r[0]) for r in journal._conn.execute("SELECT payload FROM events WHERE kind='conversation.changed'")]
        assistant = next(f for body in facts for f in body['data']['facts']
                         if f['kind'] == 'message.recorded' and f['data']['role'] == 'assistant')
        assert 'content' not in assistant['data']
        assert assistant['content_source']['kind'] == 'model.response.recorded'
        session.messages[-1]['content'] = 'POISONED MEMORY CACHE'
        with sqlite3.connect(sessions.db_path) as view:
            view.execute("UPDATE messages SET content='POISONED SQL CACHE' WHERE role='assistant'")
        history = session.read_committed_history()
        assert [m['role'] for m in history] == ['user', 'assistant', 'tool', 'assistant']
        assert history[1]['reasoning_content'] == 'native reasoning'
        assert history[2]['content'] == 'tool result'
        assert history[-1]['content'] == 'final answer'
        sessions._store.rebuild_projection()
        assert sessions._store.fetch_session_messages(session.key)[-1]['content'] == 'final answer'
        message_id = sessions._store.fetch_session_messages(session.key)[-1]['id']
        with running(journal, key='desktop:other') as other:
            await history_chat(provider, messages=[{'role': 'user', 'content': 'different turn'}], tools=[], model='deepseek-chat', max_tokens=50)
            sessions._store.update_message(message_id, delivery_status='sent')
            journal.finish(other.turn_id, 'completed')
        assert len(session.read_committed_history()) == 4  # original tool step survives
        sessions._store.update_message(message_id, content='explicitly edited answer')
        assert session.read_committed_history()[-1]['content'] == 'explicitly edited answer'
        # The edit changes new history, never the previous model request prefix.
        first_call = journal.model_context(scope.turn_id)['calls'][0]
        assert journal.model_context(scope.turn_id, call_id=first_call['call_id'])['request']['messages'][0]['content'] == 'original input'
        assert len(wire) == 3
    finally:
        await provider._client.close()
        sessions._store.close()


async def test_request_write_failure_fences_network_and_response_failure_fences_tools(tmp_path, monkeypatch):
    for boundary in ('request', 'response'):
        journal = ExecutionJournal(tmp_path / boundary)
        provider, wire = await provider_with_wire([{'content': 'answer', 'tool_calls': [
            {'id': 'write', 'type': 'function', 'function': {'name': 'write', 'arguments': '{}'}}]}])
        effects = []
        try:
            with running(journal) as scope:
                original = journal._event
                def fail(row, kind, payload):
                    if kind == ('model.request.prepared' if boundary == 'request' else 'model.response.recorded'):
                        raise OSError('disk full')
                    return original(row, kind, payload)
                monkeypatch.setattr(journal, '_event', fail)
                with pytest.raises(JournalWriteError):
                    await history_chat(provider, messages=[{'role': 'user', 'content': 'input'}], tools=[], model='m', max_tokens=5)
                    effects.append('tool')
                assert scope.broken
                assert len(wire) == (boundary == 'response')
                assert effects == []
                assert journal.get(scope.turn_id)['status'] == 'running'
        finally:
            await provider._client.close()
            journal.close()


def test_prefix_corruption_missing_fact_version_and_future_prefix_rejected(tmp_path):
    journal = ExecutionJournal(tmp_path)
    try:
        with running(journal) as scope:
            original = journal.prepare_model_request(scope.turn_id, 'call', {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]}, 'chat.completions.v1')
            events = journal.events(scope.turn_id, limit=500)['events']
            for corruption in ('missing', 'changed', 'version'):
                damaged = deepcopy(events)
                if corruption == 'missing':
                    del damaged[1]
                elif corruption == 'changed':
                    damaged[-2]['payload']['components'][0]['value'] = 'tampered'
                else:
                    damaged[-1]['payload']['version'] = 999
                with pytest.raises(ModelContextError):
                    project_model_context(damaged, root_id=journal.root_id, turn_id=scope.turn_id,
                                          call_id='call', through_seq=original['through_seq'])
            with pytest.raises(ModelContextError):
                journal.model_context(scope.turn_id, call_id='call', through_seq=original['through_seq'] + 1)
            with pytest.raises(ModelContextError):
                journal.model_context(scope.turn_id, call_id='call', expected_digest='wrong')
    finally:
        journal.close()


def test_legacy_history_and_explicit_edits_remain_authoritative(tmp_path):
    sessions = SessionManager(tmp_path)
    try:
        session = sessions.get_or_create('legacy')
        session.add_message('user', 'old question')
        session.add_message('assistant', 'old answer')
        sessions.save(session)
        session.messages.clear()
        assert [m['content'] for m in session.read_committed_history()] == ['old question', 'old answer']
        row, _ = sessions.execution_journal.admit('legacy', 'desktop', 'old', {})
        assert sessions.execution_journal.model_context(row['turn_id'])['availability'] == 'not_recorded'
        with pytest.raises(ValueError, match='legacy'):
            sessions.execution_journal.model_context(row['turn_id'], call_id='invented')
    finally:
        sessions._store.close()


async def test_unknown_tool_result_remains_unknown_and_cannot_call_model_again(tmp_path):
    sessions = SessionManager(tmp_path)
    journal = sessions.execution_journal
    provider, wire = await provider_with_wire([{'content': None, 'tool_calls': [
        {'id': 'unknown', 'type': 'function', 'function': {'name': 'send', 'arguments': '{}'}}]}])
    try:
        with running(journal) as scope:
            await history_chat(provider, messages=[{'role': 'user', 'content': 'send'}], tools=[], model='m', max_tokens=10)
            async def uncertain():
                raise TimeoutError('receipt missing')
            with pytest.raises(ExecutionNeedsReview):
                await scope.effect('delivery', {}, uncertain)
            with pytest.raises(ExecutionNeedsReview):
                await history_chat(provider, messages=[{'role': 'user', 'content': 'retry'}], tools=[], model='m', max_tokens=10)
            session = sessions.get_or_create('desktop:context')
            session.add_message('user', 'original input')
            session.add_message('assistant', 'partial presentation', tool_chain=[{'calls': [
                {'call_id': 'unknown', 'name': 'send', 'arguments': {}, 'result': 'UNTRUSTED UI RESULT'}]}])
            sessions.save(session)
            journal.finish(scope.turn_id, 'failed')
        for _ in range(3):
            journal.rebuild_projection()
            projection = journal.interaction_history(scope.turn_id)
            assert not projection['messages']
            assert projection['diagnostics'][0]['reason'] == 'unresolved_tool_step_excluded'
            assert journal.get(scope.turn_id)['status'] == 'needs_review'
            history = session.read_committed_history()
            assert not any(m['role'] == 'tool' for m in history)
            assert 'UNTRUSTED UI RESULT' not in json.dumps(history)
        assert len(wire) == 1
    finally:
        await provider._client.close()
        sessions._store.close()


async def test_background_call_has_own_durable_scope(tmp_path):
    journal = ExecutionJournal(tmp_path)
    provider, wire = await provider_with_wire([{'content': 'memory summary'}], journal=journal)
    try:
        await provider.chat(messages=[{'role': 'user', 'content': 'summarize'}], tools=[], model='m', max_tokens=10)
        execution = journal.list_executions()['executions'][0]
        assert execution['source'] == 'model' and execution['status'] == 'completed'
        call_id = next(iter(execution['model_calls']))
        assert journal.model_context(execution['turn_id'], call_id=call_id)['request'] == wire[0]
        assert journal.interaction_history(execution['turn_id'])['messages'] == []
    finally:
        await provider._client.close()
        journal.close()


@pytest.mark.parametrize('queue_kind', ['observers', 'markdown'])
@pytest.mark.parametrize('fail_model', [False, True])
async def test_queued_memory_model_does_not_own_replied_turn_or_next_producer(tmp_path, monkeypatch, queue_kind, fail_model):
    from bus.event_bus import EventBus
    from core.memory.markdown.maintenance import MarkdownMemoryMaintenance
    from core.execution.model_recording import history_lane
    from core.execution.scope import active_execution

    journal = ExecutionJournal(tmp_path)
    provider, wire = await provider_with_wire([{'content': 'summary'}], journal=journal)
    started, release = asyncio.Event(), asyncio.Event()
    original = provider._client._client._transport

    async def slow(request):
        started.set()
        await release.wait()
        if fail_model:
            wire.append(json.loads(request.content))
            return httpx.Response(503, json={'error': {'message': 'test unavailable'}})
        return await original.handle_async_request(request)

    provider._client._client._transport = httpx.MockTransport(slow)
    bus = EventBus()

    async def summarize(_):
        assert active_execution() is None and not history_lane.get()
        if fail_model:
            # A handled auxiliary failure still owns its own durable terminal fact.
            from openai import InternalServerError
            with pytest.raises(InternalServerError):
                await provider.chat(messages=[{'role': 'user', 'content': 'summary'}], tools=[], model='m', max_tokens=10)
        else:
            await provider.chat(messages=[{'role': 'user', 'content': 'summary'}], tools=[], model='m', max_tokens=10)

    if queue_kind == 'observers':
        bus.on(str, summarize)
        enqueue = lambda: bus.enqueue('post response')
        drain = bus.drain
    else:
        maintenance = MarkdownMemoryMaintenance(store=SimpleNamespace(), provider=provider, model='m', keep_count=20)
        maintenance._get_session = lambda _: SimpleNamespace()
        async def save(_): pass
        maintenance._save_session = save
        monkeypatch.setattr(maintenance, '_should_consolidate_session', lambda _: False)
        monkeypatch.setattr(maintenance, 'refresh_recent_turns', summarize)
        # Direct lifecycle enqueue keeps every turn even while maintenance is busy.
        enqueue = lambda: maintenance._enqueue_maintenance('same-session')
        async def drain():
            await asyncio.gather(*list(maintenance._maintenance_tasks.values()))

    parents = []
    try:
        with running(journal, key='first') as parent:
            parents.append(parent.turn_id)
            lane = history_lane.set(True)
            try:
                enqueue()
            finally:
                history_lane.reset(lane)
            await asyncio.wait_for(started.wait(), 5)
            # Regression: the slow auxiliary request previously blocked this terminal event.
            journal.finish(parent.turn_id, 'completed')
        with running(journal, key='second') as parent:
            parents.append(parent.turn_id)
            enqueue()
            journal.finish(parent.turn_id, 'completed')
        release.set()
        await drain()
        rows = journal.list_executions()['executions']
        children = [r for r in rows if r['source'] == 'model']
        assert len(children) == 2 and len(wire) == 2
        assert {r['input']['parent_execution']['turn_id'] for r in children} == set(parents)
        assert all(r['input']['parent_execution']['root_id'] == journal.root_id for r in children)
        for turn in parents:
            assert journal.get(turn)['status'] == 'completed'
            assert not journal.get(turn).get('model_calls')
        for child in children:
            assert child['status'] == ('failed' if fail_model else 'completed')
            assert journal.interaction_history(child['turn_id'])['messages'] == []
            call_id = next(iter(child['model_calls']))
            expected = journal.model_context(child['turn_id'], call_id=call_id)
            for _ in range(2):
                journal.rebuild_projection()
                assert journal.model_context(child['turn_id'], call_id=call_id) == expected
        assert len(wire) == 2
    finally:
        release.set()
        await drain()
        await bus.aclose()
        await provider._client.close()
        journal.close()


@pytest.mark.parametrize('finish', ['length', 'content_filter'])
async def test_truncated_nonstream_response_cannot_dispatch_tools(tmp_path, finish):
    from agent.provider import ContextLengthError, ContentSafetyError
    journal = ExecutionJournal(tmp_path)
    provider, wire = await provider_with_wire([{'content': 'unfinished', 'finish_reason': finish,
        'tool_calls': [{'id': 'tc', 'type': 'function', 'function': {'name': 'send', 'arguments': '{}'}}]}])
    try:
        with running(journal) as scope:
            with pytest.raises((ContextLengthError, ContentSafetyError)):
                await history_chat(provider, messages=[{'role': 'user', 'content': 'input'}], tools=[], model='m', max_tokens=1)
            response = next(e['payload'] for e in journal.events(scope.turn_id, limit=500)['events']
                            if e['kind'] == 'model.response.recorded')
            assert 'error_type' in response and 'response' not in response
            if finish == 'length':
                assert response['diagnostics']['reason'] == 'output_truncated'
                partial = response['diagnostics']['partial_response']
                assert partial['choices'][0]['message']['content'] == 'unfinished'
                assert partial['choices'][0]['finish_reason'] == 'length'
            assert journal.interaction_history(scope.turn_id)['messages'] == []
            assert not journal.get(scope.turn_id)['effects'] and len(wire) == 1
    finally:
        await provider._client.close()
        journal.close()


async def test_actual_provider_consumes_projector_output_not_input_snapshot(tmp_path, monkeypatch):
    from core.execution import model_context
    journal = ExecutionJournal(tmp_path)
    provider, wire = await provider_with_wire([{'content': 'answer'}], journal=journal)
    original = model_context.project_model_context
    consumed = []
    def instrument(*args, **kwargs):
        projected = original(*args, **kwargs)
        # A distinctive test-only projection result proves the wire consumes it.
        projected['request']['max_tokens'] = 17
        consumed.append(projected['request'])
        return projected
    monkeypatch.setattr(model_context, 'project_model_context', instrument)
    try:
        await provider.chat(messages=[{'role': 'user', 'content': 'input'}], tools=[], model='m', max_tokens=99)
        assert len(consumed) == 1 and wire[0] == consumed[0]
        assert wire[0]['max_tokens'] == 17
    finally:
        await provider._client.close()
        journal.close()


@pytest.mark.parametrize('complete', [True, False])
async def test_stream_partial_never_becomes_a_complete_semantic_response(tmp_path, complete):
    journal = ExecutionJournal(tmp_path)
    provider = LLMProvider(api_key='fixture', max_retries=0)
    requests, deltas = [], []
    chunks = [{'id': 'stream', 'object': 'chat.completion.chunk', 'created': 0, 'model': 'm',
               'choices': [{'index': 0, 'delta': {'content': 'partial'}, 'finish_reason': None}]}]
    if complete:
        chunks.append({'id': 'stream', 'object': 'chat.completion.chunk', 'created': 0, 'model': 'm',
                       'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})
    def wire(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
            content=''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n')
    await provider._client._client.aclose()
    provider._client._client = httpx.AsyncClient(transport=httpx.MockTransport(wire))
    try:
        with running(journal) as scope:
            async def on_delta(delta):
                deltas.append(delta)
                scope.record('reply.delta', {'content': delta.get('content_delta', '')})
            operation = history_chat(provider, messages=[{'role': 'user', 'content': 'hi'}], tools=[],
                                     model='m', max_tokens=10, on_content_delta=on_delta)
            if complete:
                assert (await operation).content == 'partial'
            else:
                with pytest.raises(RuntimeError, match='完整结束'):
                    await operation
            facts = journal.events(scope.turn_id, limit=500)['events']
            result = next(e['payload'] for e in facts if e['kind'] == 'model.response.recorded')
            assert ('response' in result) == complete
            assert len(deltas) == 1
            call_id = journal.model_context(scope.turn_id)['calls'][0]['call_id']
            assert journal.model_context(scope.turn_id, call_id=call_id)['request'] == requests[0]
    finally:
        await provider._client.close()
        journal.close()


async def test_codex_native_request_projection_and_auth_is_out_of_band(tmp_path, monkeypatch):
    from agent.config_models import ModelRegistration
    from agent.model_runtime.provider import CodexProvider
    from agent.model_runtime.transports.responses import CodexResponsesTransport
    from agent.model_runtime.types import ModelCapabilities
    journal = ExecutionJournal(tmp_path)
    sent = []
    async def stream():
        yield {'type': 'response.output_text.done', 'text': 'answer'}
        yield {'type': 'response.output_item.done', 'item': {'type': 'reasoning', 'id': 'r1', 'encrypted_content': 'opaque-native-state'}}
        yield {'type': 'response.completed', 'response': {}}
    class Client:
        def __init__(self, **kwargs):
            self.responses = self
        async def create(self, **kwargs):
            sent.append(kwargs)
            return stream()
        async def close(self):
            pass
    monkeypatch.setattr('agent.model_runtime.transports.responses.AsyncOpenAI', Client)
    auth = SimpleNamespace(headers=lambda **kwargs: {'Authorization': 'Bearer never-archive-oauth', 'ChatGPT-Account-ID': 'account'})
    provider = CodexProvider(workspace=tmp_path, registration=ModelRegistration('connection', 'codex', '', '', 'm'))
    provider._transports['m'] = CodexResponsesTransport(auth, runtime_id='connection')
    provider._capabilities['m'] = ModelCapabilities(100000, 1000)
    try:
        with running(journal) as scope:
            response = await history_chat(provider, messages=[{'role': 'system', 'content': 'system'},
                {'role': 'assistant', 'content': 'prior', 'model_state': {'schema_version': 1, 'runtime_id': 'connection',
                    'transport': 'responses', 'model': 'm', 'items': [{'type': 'reasoning', 'encrypted_content': 'prior-native'}]}},
                {'role': 'user', 'content': 'hi'}], tools=[], model='m', max_tokens=50)
            call_id = journal.model_context(scope.turn_id)['calls'][0]['call_id']
            projected = journal.model_context(scope.turn_id, call_id=call_id)
            assert projected['request'] == sent[0]
            assert projected['request']['input'][0]['encrypted_content'] == 'prior-native'
            assert response.provider_fields['model_state']['items'][0]['encrypted_content'] == 'opaque-native-state'
            assert 'never-archive-oauth' not in json.dumps(journal.events(scope.turn_id, limit=500))
    finally:
        journal.close()


@pytest.mark.parametrize('provider_name', ['openai', 'deepseek'])
async def test_chat_route_does_not_send_codex_native_history_state(tmp_path, provider_name):
    journal = ExecutionJournal(tmp_path)
    provider, wire = await provider_with_wire([{'content': 'ok'}], journal=journal, provider_name=provider_name)
    try:
        await history_chat(provider, messages=[{'role': 'assistant', 'content': 'previous answer',
            'model_state': {'schema_version': 1, 'runtime_id': 'codex-route', 'transport': 'responses',
                            'model': 'old', 'items': [{'type': 'reasoning', 'encrypted_content': 'opaque-codex'}]}},
            {'role': 'user', 'content': 'next'}], tools=[], model='deepseek-chat' if provider_name == 'deepseek' else 'm', max_tokens=10)
        assert 'model_state' not in json.dumps(wire[0])
        assert 'opaque-codex' not in json.dumps(wire[0])
        execution = journal.list_executions()['executions'][0]
        call_id = next(iter(execution['model_calls']))
        assert journal.model_context(execution['turn_id'], call_id=call_id)['request']['messages'] == wire[0]['messages']
    finally:
        await provider._client.close()
        journal.close()


@pytest.mark.parametrize('source', ['desktop', 'telegram', 'subagent'])
async def test_existing_execution_sources_share_model_projection(tmp_path, source):
    journal = ExecutionJournal(tmp_path)
    provider, wire = await provider_with_wire([{'content': 'ok'}])
    try:
        with running(journal, key=source + ':context', source=source) as scope:
            await history_chat(provider, messages=[{'role': 'user', 'content': 'input'}], tools=[], model='m', max_tokens=10)
            journal.finish(scope.turn_id, 'completed')
        before = journal.events(scope.turn_id, limit=500)
        call = journal.model_context(scope.turn_id)['calls'][0]
        for _ in range(3):
            assert journal.model_context(scope.turn_id, call_id=call['call_id'])['request'] == wire[0]
            journal.rebuild_projection()
        assert journal.events(scope.turn_id, limit=500) == before
        assert len(wire) == 1
    finally:
        await provider._client.close()
        journal.close()


async def test_deferred_conversation_commit_after_restart_keeps_original_semantic_owner(tmp_path):
    sessions = SessionManager(tmp_path)
    provider, wire = await provider_with_wire([{'content': 'answer', 'reasoning_content': 'native state'}], provider_name='deepseek')
    try:
        with running(sessions.execution_journal) as scope:
            await history_chat(provider, messages=[{'role': 'user', 'content': 'original input'}], tools=[], model='deepseek-chat', max_tokens=50)
        # No UI row committed yet. The persisted request key in the pending
        # presentation batch must resolve the old execution after restart.
        sessions._store.close()
        sessions = SessionManager(tmp_path)
        assert sessions.execution_journal.get(scope.turn_id)['status'] == 'interrupted'
        session = sessions.get_or_create('desktop:context')
        for role, content in [('user', 'original input'), ('assistant', 'answer')]:
            session.add_message(role, content, metadata={'execution_request_id': 'request', 'source': 'desktop'})
        sessions.save(session)
        sessions._store.rebuild_projection()
        history = session.read_committed_history()
        assert history[-1]['reasoning_content'] == 'native state'
        assert history[-1]['content'] == 'answer'
        sessions.save(session)
        assert sessions._store.count_messages(session.key) == 2 and len(wire) == 1
    finally:
        await provider._client.close()
        sessions._store.close()


async def test_proactive_audit_references_canonical_response_and_duplicate_admission_does_not_call(tmp_path):
    from core.execution.proactive import RecordedProactiveProvider, run_proactive_recorded
    journal = ExecutionJournal(tmp_path)
    provider, wire = await provider_with_wire([{'content': 'decision'}])
    wrapper = RecordedProactiveProvider(provider)
    async def operation():
        await wrapper.chat(messages=[{'role': 'user', 'content': 'judge'}], tools=[], model='m', max_tokens=10)
        return 0
    try:
        for _ in range(2):
            assert await run_proactive_recorded(journal, 'reminder', 'tick', {}, operation) == 0
        execution = journal.find('reminder', 'proactive', 'tick')
        events = journal.events(execution['turn_id'], limit=500)['events']
        returned = next(e['payload'] for e in events if e['kind'] == 'proactive.model.returned')
        assert 'model_call_id' in returned and 'response' not in returned
        assert journal.model_response(execution['turn_id'], returned['model_call_id'])['content'] == 'decision'
        assert len(wire) == 1
    finally:
        await provider._client.close()
        journal.close()


async def test_tool_effect_and_model_result_have_stable_owner_and_reused_ids_are_scoped(tmp_path):
    from agent.tools.base import Tool
    from agent.tools.registry import ToolRegistry
    from agent.tool_hooks import ToolExecutor, ToolExecutionRequest
    journal = ExecutionJournal(tmp_path)
    provider, wire = await provider_with_wire([{'content': 'read', 'tool_calls': [
        {'id': 'reused', 'type': 'function', 'function': {'name': 'read', 'arguments': '{}'}}]}])
    effects = []
    class Read(Tool):
        name = 'read'
        description = 'test'
        parameters = {'type': 'object', 'properties': {}}
        async def execute(self, **kwargs):
            effects.append('read')
            return 'observed result'
    registry = ToolRegistry()
    registry.register(Read())
    executor = ToolExecutor()
    try:
        with running(journal) as scope:
            messages = [{'role': 'user', 'content': 'read'}]
            for _ in range(2):
                response = await history_chat(provider, messages=messages, tools=registry.get_schemas(), model='m', max_tokens=50)
                append_assistant_tool_calls(messages, content=response.content, tool_calls=response.tool_calls)
                result = await executor.execute(ToolExecutionRequest(call_id='reused', tool_name='read', arguments={},
                    source='passive', session_key='desktop:context', channel='desktop', chat_id='context'), registry.execute)
                append_tool_result(messages, tool_call_id='reused', content=result.output)
            journal.finish(scope.turn_id, 'completed')
        state = journal.get(scope.turn_id)
        assert len(state['model_calls']) == 2
        assert len({e['model_call_id'] for e in state['effects']}) == 2
        results = [e['payload'] for e in journal.events(scope.turn_id, limit=500)['events'] if e['kind'] == 'interaction.tool_result']
        assert all(len(r['effect_ids']) == 1 for r in results)
        assert {r['effect_ids'][0] for r in results} == {e['effect_id'] for e in state['effects']}
        for call_id in state['model_calls']:
            journal.model_context(scope.turn_id, call_id=call_id)
        assert len(effects) == len(wire) == 2
    finally:
        await provider._client.close()
        journal.close()
