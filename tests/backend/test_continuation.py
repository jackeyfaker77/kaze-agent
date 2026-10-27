"""Safe boundaries are facts, never operator assertions or a second execution."""
import asyncio
import json
from contextlib import nullcontext
from pathlib import Path

import pytest
import pytest_asyncio

from agent.config_models import Config
from agent.tools.base import Tool
from bootstrap.tools import build_core_runtime
from core.execution.continuation import record_boundary, read_events, resolve_boundary
from core.execution.model_recording import history_chat, invoke_model_tool, record_tool_context
from core.execution.scope import ExecutionScope, current_execution
from core.net.http import SharedHttpResources
from desktop_bridge.session_service import DesktopBridgeService
from tests.backend.test_model_context_projection import provider_with_wire


class Write(Tool):
    name = 'continuation_write'
    description = 'write a test marker'
    parameters = {'type': 'object', 'properties': {'value': {'type': 'string'}}}

    def __init__(self):
        self.calls = []

    async def execute(self, value, **kwargs):
        self.calls.append(value)
        return 'saved:' + value


@pytest_asyncio.fixture
async def host(tmp_path):
    http = SharedHttpResources()
    runtime = build_core_runtime(Config(provider='openai', model='fake', api_key='fake'), tmp_path, http)
    service = DesktopBridgeService(runtime)
    tool = Write()
    runtime.tools.register(tool)
    yield service, tool
    await service.aclose()
    await runtime.stop()
    await runtime.memory_runtime.aclose()
    runtime.session_manager._store.close()
    await http.aclose()


async def seed(service, tool, *, mode='complete', metadata=None):
    journal = service.sessions.execution_journal
    key = 'desktop:continuation'
    row, _ = journal.admit(key, 'desktop', 'original', {'content': 'save once then explain', 'channel': 'desktop',
        'chat_id': key, 'sender': 'user', 'media': [], 'metadata': metadata or {}})
    journal.start(row['turn_id'])
    scope = ExecutionScope(journal, row['turn_id'])
    token = current_execution.set(scope)
    provider, wire = await provider_with_wire([{'content': '', 'tool_calls': [{'id': 'write-1', 'type': 'function',
        'function': {'name': tool.name, 'arguments': '{"value":"one"}'}}]}], journal=journal)
    try:
        session = service.sessions.get_or_create(key)
        session.add_message('user', row['input']['content'])
        await service.sessions.append_messages(session, [session.messages[-1]])
        messages = [{'role': 'system', 'content': 'OLD SYSTEM'}, {'role': 'user', 'content': row['input']['content']}]
        record_boundary(scope, messages)
        if mode != 'user':
            await history_chat(provider, messages=messages, tools=[], model='fake', max_tokens=50)
            if mode != 'dangling':
                result = await invoke_model_tool('write-1', service.runtime.tools.execute, tool.name, {'value': 'one'})
                if mode != 'physical_only':
                    record_tool_context([{'role': 'tool', 'tool_call_id': 'write-1', 'content': result}], tool_call_id='write-1')
        journal.finish(scope.turn_id, 'interrupted')
        return scope.turn_id, wire
    finally:
        scope.active = False
        current_execution.reset(token)
        await provider._client.close()


async def call(service, method, **payload):
    response = await service.handle({'method': method, 'payload': payload})
    assert response.error is None, response.error
    return response.payload


async def continuation_provider(service, responses=None):
    provider, wire = await provider_with_wire(responses or [{'content': 'finished'}], journal=service.sessions.execution_journal)
    service.runtime.loop._reasoner._llm.provider = provider
    return provider, wire


@pytest.mark.asyncio
async def test_plan_is_read_only_continue_reuses_completed_steps_and_clicks(host):
    service, tool = host
    turn, original_wire = await seed(service, tool)
    journal = service.sessions.execution_journal
    before = list(journal._conn.iterdump())
    plan = await call(service, 'execution.plan', turn_id=turn)
    assert plan['allowed'], plan
    assert plan == await call(service, 'execution.plan', turn_id=turn)
    assert before == list(journal._conn.iterdump())
    provider, wire = await continuation_provider(service)
    try:
        args = dict(turn_id=turn, plan_digest=plan['plan_digest'], action_id='confirm', confirmed=True)
        first = await call(service, 'execution.continue', **args)
        duplicate = await call(service, 'execution.continue', **{**args, 'action_id': 'other-client'})
        assert first['execution']['turn_id'] == duplicate['execution']['turn_id']
        assert first['created'] and not duplicate['created']
        assert first['execution']['status'] == 'completed'
        assert tool.calls == ['one'] and len(original_wire) == len(wire) == 1
        messages = wire[0]['messages']
        assert sum(m.get('content') == 'save once then explain' for m in messages) == 1
        assert any(m.get('role') == 'tool' and m.get('content') == 'saved:one' for m in messages)
        assert not any(m.get('content') == 'OLD SYSTEM' for m in messages)
        assert len([m for m in service.sessions.get_or_create('desktop:continuation').messages if m['role'] == 'user']) == 1
        assert read_events(journal, turn)[-1]['kind'] == 'run.ended'
        journal.rebuild_projection()
        assert journal.get(turn)['continuation_turn_id'] == first['execution']['turn_id']
        assert journal.find('desktop:continuation', 'desktop', 'original') == journal.get(turn)
        listed = next(r for r in journal.list_executions()['executions'] if r['turn_id'] == turn)
        assert listed == journal.get(turn)
        assert not (await call(service, 'execution.plan', turn_id=turn))['allowed']
    finally:
        await provider._client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['dangling', 'physical_only'])
async def test_incomplete_tool_boundary_cannot_be_confirmed(host, mode):
    service, tool = host
    turn, _ = await seed(service, tool, mode=mode)
    plan = await call(service, 'execution.plan', turn_id=turn)
    assert not plan['allowed'] and 'incomplete_tools' in plan['reasons']
    result = await service.handle({'method': 'execution.continue', 'payload': {
        'turn_id': turn, 'plan_digest': plan['plan_digest'], 'action_id': 'force', 'confirmed': True}})
    assert result.error
    assert service.sessions.execution_journal._conn.execute('SELECT COUNT(*) FROM continuation_claims').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_stale_plan_revalidates_session_catalog_and_explicit_confirmation(host):
    service, tool = host
    turn, _ = await seed(service, tool)
    plan = await call(service, 'execution.plan', turn_id=turn)
    payload = {'turn_id': turn, 'plan_digest': plan['plan_digest'], 'action_id': 'x'}
    assert (await service.handle({'method': 'execution.continue', 'payload': payload})).error
    service.runtime.tools.unregister(tool.name)
    assert (await service.handle({'method': 'execution.continue', 'payload': {**payload, 'confirmed': True}})).error
    assert 'tools_changed' in (await call(service, 'execution.plan', turn_id=turn))['reasons']


@pytest.mark.asyncio
async def test_new_model_cannot_redispatch_same_completed_tool(host):
    service, tool = host
    turn, _ = await seed(service, tool)
    plan = await call(service, 'execution.plan', turn_id=turn)
    provider, wire = await continuation_provider(service, [{'content': '', 'tool_calls': [{'id': 'new-id', 'type': 'function',
        'function': {'name': tool.name, 'arguments': '{"value":"one"}'}}]}])
    try:
        result = await service.handle({'method': 'execution.continue', 'payload': {
            'turn_id': turn, 'plan_digest': plan['plan_digest'], 'action_id': 'x', 'confirmed': True}})
        assert result.error and tool.calls == ['one'] and len(wire) == 1
    finally:
        await provider._client.close()


@pytest.mark.asyncio
async def test_unknown_even_read_only_and_manual_review_never_grants_resume(host):
    service, tool = host
    turn, _ = await seed(service, tool, mode='user')
    journal = service.sessions.execution_journal
    # Separate real admission with an unresolved read-only dispatch. Read-only
    # retry policy is not evidence of a committed result for continuation.
    key = 'desktop:unknown'
    row, _ = journal.admit(key, 'desktop', 'unknown', {'channel': 'desktop', 'content': 'inspect'})
    service.sessions.get_or_create(key)
    journal.start(row['turn_id'])
    scope = ExecutionScope(journal, row['turn_id'])
    record_boundary(scope, [{'role': 'user', 'content': 'inspect'}])
    journal.append(scope.turn_id, 'tool.dispatched', {'effect_id': 'unknown', 'name': tool.name,
        'model_arguments': {}, 'retry_policy': 'read_only'})
    journal.finish(scope.turn_id, 'interrupted')
    plan = await call(service, 'execution.plan', turn_id=scope.turn_id)
    assert 'unknown_effect' in plan['reasons']
    old = journal.get(scope.turn_id)
    journal.review(scope.turn_id, expected_seq=old['last_seq'], action_id='review', decision='no_effect', note='operator checked')
    plan = await call(service, 'execution.plan', turn_id=scope.turn_id)
    assert not plan['allowed'] and 'unknown_effect' in plan['reasons']
    assert journal.get(scope.turn_id)['effects'][0]['certainty'] == 'unknown'


@pytest.mark.asyncio
async def test_claim_failure_rolls_back_before_any_model_and_source_is_immutable(host, monkeypatch):
    service, tool = host
    turn, _ = await seed(service, tool)
    journal = service.sessions.execution_journal
    plan = await call(service, 'execution.plan', turn_id=turn)
    original = journal._event
    def fail(row, kind, payload):
        original(row, kind, payload)
        if kind == 'continuation.started':
            raise OSError('commit failure')
    monkeypatch.setattr(journal, '_event', fail)
    before = list(journal._conn.iterdump())
    result = await service.handle({'method': 'execution.continue', 'payload': {
        'turn_id': turn, 'plan_digest': plan['plan_digest'], 'action_id': 'x', 'confirmed': True}})
    assert result.error
    assert before == list(journal._conn.iterdump())


@pytest.mark.asyncio
async def test_new_user_activity_invalidates_preview(host):
    service, tool = host
    turn, _ = await seed(service, tool)
    plan = await call(service, 'execution.plan', turn_id=turn)
    session = service.sessions.get_or_create('desktop:continuation')
    session.add_message('user', 'new instruction')
    await service.sessions.append_messages(session, [session.messages[-1]])
    result = await service.handle({'method': 'execution.continue', 'payload': {
        'turn_id': turn, 'plan_digest': plan['plan_digest'], 'action_id': 'x', 'confirmed': True}})
    assert result.error


@pytest.mark.asyncio
@pytest.mark.parametrize('entered_loop', [True, False])
async def test_multigeneration_lineage_keeps_original_input_and_tool_history_once(host, entered_loop):
    service, tool = host
    turn, _ = await seed(service, tool)
    journal = service.sessions.execution_journal
    from core.execution.continuation import claim_continuation
    from desktop_bridge.continuation_service import inspect_host
    plan = await call(service, 'execution.plan', turn_id=turn)
    child, _ = claim_continuation(journal, turn, plan['plan_digest'], 'first', lambda: inspect_host(service, turn))
    journal.start(child['turn_id'])
    if entered_loop:
        record_boundary(ExecutionScope(journal, child['turn_id']), [])
    journal.finish(child['turn_id'], 'interrupted')
    plan = await call(service, 'execution.plan', turn_id=child['turn_id'])
    assert plan['allowed'], plan
    provider, wire = await continuation_provider(service)
    try:
        result = await call(service, 'execution.continue', turn_id=child['turn_id'],
                            plan_digest=plan['plan_digest'], action_id='second', confirmed=True)
        history = service.sessions.get_or_create('desktop:continuation').read_committed_history()
        assert sum(m.get('role') == 'user' and m.get('content') == 'save once then explain' for m in history) == 1
        assert sum(m.get('role') == 'tool' for m in history) == 1
        assert tool.calls == ['one'] and len(wire) == 1
        assert result['execution']['continuation']['source_turn_id'] == child['turn_id']
        assert not result['execution']['retry']['allowed']
    finally:
        await provider._client.close()


@pytest.mark.asyncio
async def test_pending_or_unknown_queue_blocks_and_done_receipt_is_not_resent(host):
    service, tool = host
    turn, _ = await seed(service, tool)
    queue = service.runtime.bus.transport
    queue.put('existing-send', 'bus:test', {'content': 'sent once'}, turn_id=turn)
    for state in ('pending', 'running', 'needs_review', 'failed'):
        queue.state('existing-send', state)
        assert 'queue_unsettled' in (await call(service, 'execution.plan', turn_id=turn))['reasons']
    queue.state('existing-send', 'done', result={'message_id': 'receipt'})
    plan = await call(service, 'execution.plan', turn_id=turn)
    assert plan['allowed'], plan
    before = list(queue.db.iterdump())
    provider, _ = await continuation_provider(service)
    try:
        await call(service, 'execution.continue', turn_id=turn, plan_digest=plan['plan_digest'], action_id='x', confirmed=True)
        assert list(queue.db.iterdump()) == before
    finally:
        await provider._client.close()


@pytest.mark.asyncio
async def test_real_chat_cancel_at_committed_tool_boundary_then_continue(host, monkeypatch):
    service, tool = host
    journal = service.sessions.execution_journal
    monkeypatch.setattr(service.runtime.loop._reasoner, '_stream_sink_factory', None)
    provider, requests = await continuation_provider(service, [{'content': '', 'tool_calls': [{'id': 'real-1', 'type': 'function',
        'function': {'name': tool.name, 'arguments': '{"value":"one"}'}}]}, {'content': 'finished'}])
    original = ExecutionScope.record
    def cancel_after_result(scope, kind, payload):
        original(scope, kind, payload)
        if kind == 'interaction.tool_result':
            raise asyncio.CancelledError()
    monkeypatch.setattr(ExecutionScope, 'record', cancel_after_result)
    try:
        result = await call(service, 'chat.send', session_key='desktop:real', content='save once then explain', execution_request_id='real-original')
        assert result['cancelled']
        source = journal.find('desktop:real', 'desktop', 'real-original')
        plan = await call(service, 'execution.plan', turn_id=source['turn_id'])
        assert plan['allowed'], plan
        monkeypatch.setattr(ExecutionScope, 'record', original)
        continued = await call(service, 'execution.continue', turn_id=source['turn_id'], plan_digest=plan['plan_digest'], action_id='real', confirmed=True)
        assert continued['execution']['status'] == 'completed'
        assert len(requests) == 2 and tool.calls == ['one']
        assert sum(m['role'] == 'user' for m in continued['session']['messages']) == 1
        assert sum(bool(m.get('tool_calls')) for m in service.sessions.get_or_create('desktop:real').read_committed_history()) == 1
    finally:
        await provider._client.close()


@pytest.mark.asyncio
async def test_changed_directory_and_legacy_boundary_park_without_mutation(host, monkeypatch):
    service, tool = host
    turn, _ = await seed(service, tool)
    from desktop_bridge import continuation_service
    monkeypatch.setattr(continuation_service, 'getcwd', lambda: str(service.workspace / 'different'))
    plan = await call(service, 'execution.plan', turn_id=turn)
    assert not plan['allowed'] and 'workspace_changed' in plan['reasons']
    journal = service.sessions.execution_journal
    source, _ = journal.admit('desktop:legacy', 'desktop', 'legacy', {'channel': 'desktop'})
    journal.start(source['turn_id'])
    journal.finish(source['turn_id'], 'interrupted')
    service.sessions.get_or_create('desktop:legacy')
    before = list(journal._conn.iterdump())
    plan = await call(service, 'execution.plan', turn_id=source['turn_id'])
    assert not plan['allowed'] and 'legacy_boundary' in plan['reasons']
    assert before == list(journal._conn.iterdump())


@pytest.mark.asyncio
async def test_deleted_ancestor_input_cannot_be_restored_by_later_continuation(host):
    service, tool = host
    turn, _ = await seed(service, tool)
    journal = service.sessions.execution_journal
    from core.execution.continuation import claim_continuation
    from desktop_bridge.continuation_service import inspect_host
    plan = await call(service, 'execution.plan', turn_id=turn)
    child, _ = claim_continuation(journal, turn, plan['plan_digest'], 'claim', lambda: inspect_host(service, turn))
    journal.finish(child['turn_id'], 'interrupted')
    snapshot = service.sessions._store.committed_history_snapshot('desktop:continuation')
    assert service.sessions._store.delete_message(snapshot['messages'][0]['id'])
    plan = await call(service, 'execution.plan', turn_id=child['turn_id'])
    assert not plan['allowed'] and 'session_changed' in plan['reasons']


@pytest.mark.asyncio
async def test_continuation_keeps_disabled_tools_and_original_user_reference(host):
    service, tool = host
    turn, _ = await seed(service, tool, metadata={'disabled_tools': [tool.name]})
    class ContextProbe(Write):
        name = 'continuation_context_probe'

        async def execute(self, value, **kwargs):
            self.calls.append(kwargs)
            return 'observed'

    probe = ContextProbe()
    service.runtime.tools.register(probe)
    plan = await call(service, 'execution.plan', turn_id=turn)
    provider, wire = await continuation_provider(service, [{'content': '', 'tool_calls': [
        {'id': 'blocked', 'type': 'function', 'function': {'name': tool.name, 'arguments': '{"value":"two"}'}},
        {'id': 'probe', 'type': 'function', 'function': {'name': probe.name, 'arguments': '{"value":"inspect"}'}}]},
        {'content': 'finished'}])
    try:
        result = await call(service, 'execution.continue', turn_id=turn, plan_digest=plan['plan_digest'], action_id='x', confirmed=True)
        assert result['execution']['status'] == 'completed'
        assert tool.calls == ['one'] and len(probe.calls) == 1 and len(wire) == 2
        assert all(t['function']['name'] != tool.name for t in wire[0]['tools'])
        user, = [m for m in result['session']['messages'] if m['role'] == 'user']
        assert probe.calls[0]['current_user_source_ref'] == user['id']
        assert probe.calls[0]['current_user_message'] == 'save once then explain'
    finally:
        await provider._client.close()


@pytest.mark.asyncio
async def test_cancelled_continuation_returns_identity_without_reusing_claim(host, monkeypatch):
    service, tool = host
    turn, _ = await seed(service, tool)
    plan = await call(service, 'execution.plan', turn_id=turn)
    entered = asyncio.Event()
    async def hold_prompt(_):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(service.runtime.loop._reasoner, 'render_prompt', hold_prompt)
    args = dict(turn_id=turn, plan_digest=plan['plan_digest'], action_id='cancelled', confirmed=True)
    pending = asyncio.create_task(call(service, 'execution.continue', **args))
    await asyncio.wait_for(entered.wait(), 5)
    assert (await call(service, 'chat.cancel', session_key='desktop:continuation'))['cancelled']
    result = await asyncio.wait_for(pending, 5)
    assert result['cancelled'] and result['execution']['status'] == 'interrupted'
    duplicate = await call(service, 'execution.continue', **args)
    assert not duplicate['created'] and duplicate['execution']['turn_id'] == result['execution']['turn_id']
    assert not service._requests and not service._chat_tasks and tool.calls == ['one']
