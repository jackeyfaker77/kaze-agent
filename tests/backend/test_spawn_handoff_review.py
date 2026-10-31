"""Regression coverage for P0a's independent review findings."""
import asyncio
from typing import Any, cast

import pytest

from agent.background.subagent_manager import SubagentManager
from agent.looping.core import AgentLoop
from agent.looping.ports import AgentLoopConfig, AgentLoopDeps, LLMConfig, MemoryServices
from agent.provider import LLMResponse
from agent.tools.registry import ToolRegistry
from bus.events import OutboundMessage
from core.channels.hub import ChannelHub
from core.execution.contracts import ExecutionUnavailable
from core.execution.journal import ExecutionJournal
from core.execution.projection import ExecutionProjectionError
from core.execution.runner import run_spawn_recorded
from session.manager import SessionManager
from tests.backend.memory_fakes import FakeMemoryEngine
from tests.backend.test_spawn_handoff import bus_for, close_bus, completed_child, parent, route


class Provider:
    def __init__(self):
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        return LLMResponse(content='result', tool_calls=[])


def make_loop(workspace, sessions, bus, provider):
    return AgentLoop(AgentLoopDeps(bus=bus, provider=cast(Any, provider), tools=ToolRegistry(),
        session_manager=sessions, workspace=workspace,
        memory_services=MemoryServices(engine=FakeMemoryEngine(workspace))),
        AgentLoopConfig(llm=LLMConfig(max_iterations=3)))


@pytest.mark.asyncio
async def test_operator_close_survives_admission_recovery(tmp_path):
    journal = ExecutionJournal(tmp_path)
    root = parent(journal)
    completed_child(journal, root)
    journal.finish(root, 'completed')
    claim = journal.claim_spawn_completion()
    consumer, _ = journal.admit_spawn_consumer('child', claim['claim_token'], 'telegram:42')
    journal.close()
    journal = ExecutionJournal(tmp_path)
    bus = bus_for(tmp_path, journal)
    try:
        state = journal.get(consumer['turn_id'])
        journal.review(consumer['turn_id'], expected_seq=state['last_seq'], action_id='close', decision='close', note='stop processing')
        with pytest.raises(ExecutionProjectionError):
            with journal._transaction():
                journal._event(journal._row(consumer['turn_id']), 'spawn.admission.reclaimed', {'epoch': journal.epoch})
        item = await bus.consume_inbound()
        async def forbidden():
            raise AssertionError('Closed work must not restart')
        reply = await run_spawn_recorded(journal, item, item.session_key, forbidden)
        assert '已关闭' in reply.content
        await bus.complete_inbound(item)
        assert journal.get(consumer['turn_id'])['status'] == 'interrupted'
        assert journal.spawn_completion('child')['acked'] == 1
    finally:
        close_bus(bus)
        journal.close()


@pytest.mark.asyncio
async def test_cancel_completed_pending_result_skips_parent_model(tmp_path):
    sessions = SessionManager(tmp_path)
    journal = sessions.execution_journal
    bus = bus_for(tmp_path, journal)
    provider = Provider()
    try:
        root = parent(journal)
        child, _ = completed_child(journal, root)
        journal.finish(root, 'completed')
        manager = SubagentManager(provider=cast(Any, provider), workspace=tmp_path, bus=bus,
            model='fake', max_tokens=100, fetch_requester=cast(Any, object()))
        manager.execution_journal = journal
        assert await manager.cancel('child')
        item = await bus.consume_inbound()
        loop = make_loop(tmp_path, sessions, bus, provider)
        reply = await loop._process_session_scoped(item, item.session_key, dispatch_outbound=False)
        assert '已取消' in reply.content
        await bus.complete_inbound(item)
        assert provider.calls == 0
        assert journal.get(child)['status'] == 'completed'
        assert journal.spawn_completion('child')['payload']['event']['exit_reason'] == 'completed'
        assert journal.spawn_completion('child')['acked'] == 1
    finally:
        close_bus(bus)
        journal.close()
        sessions._store.close()


@pytest.mark.parametrize('account', ['replacement-account', None])
@pytest.mark.asyncio
async def test_changed_or_unready_account_keeps_result_pending(tmp_path, account):
    journal = ExecutionJournal(tmp_path)
    bus = bus_for(tmp_path, journal)
    try:
        root = parent(journal)
        completed_child(journal, root)
        journal.finish(root, 'completed')
        if account:
            bus.outbox.accounts['telegram'] = account
        else:
            bus.outbox.accounts.clear()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(bus.consume_inbound(), .15)
        receipt = journal.spawn_completion('child')
        assert not receipt['acked'] and receipt['consumer_turn_id'] is None
        bus.outbox.accounts['telegram'] = 'test-account'
        assert (await bus.consume_inbound()).event.job_id == 'child'
    finally:
        close_bus(bus)
        journal.close()


@pytest.mark.asyncio
async def test_account_change_after_claim_blocks_parent_admission(tmp_path):
    sessions = SessionManager(tmp_path)
    journal = sessions.execution_journal
    bus = bus_for(tmp_path, journal)
    provider = Provider()
    try:
        root = parent(journal)
        completed_child(journal, root)
        journal.finish(root, 'completed')
        item = await bus.consume_inbound()
        bus.outbox.accounts['telegram'] = 'replacement-account'
        loop = make_loop(tmp_path, sessions, bus, provider)
        with pytest.raises(ExecutionUnavailable):
            await loop._process_session_scoped(item, item.session_key)
        await bus.complete_inbound(item)
        assert provider.calls == 0
        assert journal.spawn_completion('child')['consumer_turn_id'] is None
        assert not journal.spawn_completion('child')['acked']
    finally:
        close_bus(bus)
        journal.close()
        sessions._store.close()


@pytest.mark.parametrize('change_during_operation', [True, False])
@pytest.mark.asyncio
async def test_account_change_cannot_rebind_reply_or_ack(tmp_path, change_during_operation):
    journal = ExecutionJournal(tmp_path)
    bus = bus_for(tmp_path, journal)
    try:
        root = parent(journal)
        completed_child(journal, root)
        journal.finish(root, 'completed')
        item = await bus.consume_inbound()
        async def operation():
            if change_during_operation:
                bus.outbox.accounts['telegram'] = 'replacement-account'
                with pytest.raises(ExecutionUnavailable):
                    await bus.publish_outbound(OutboundMessage('telegram', '42', 'must not leak'))
            return OutboundMessage('telegram', '42', 'result')
        await run_spawn_recorded(journal, item, item.session_key, operation)
        bus.outbox.accounts['telegram'] = 'replacement-account'
        await bus.complete_inbound(item)
        assert not journal.spawn_completion('child')['acked']
        assert bus.transport.db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
        bus.outbox.accounts['telegram'] = 'test-account'
        retry = await bus.consume_inbound()
        async def forbidden():
            raise AssertionError('Completed consumer must not repeat')
        await run_spawn_recorded(journal, retry, retry.session_key, forbidden)
        await bus.complete_inbound(retry)
        assert journal.spawn_completion('child')['acked']
        assert bus.transport.get('reply:spawn:child')['payload']['_transport_account'] == 'test-account'
    finally:
        close_bus(bus)
        journal.close()


@pytest.mark.parametrize('channel', ['desktop', 'cli'])
@pytest.mark.parametrize('outcome', ['normal', 'cancelled', 'closed', 'replay'])
@pytest.mark.asyncio
async def test_full_loop_publishes_one_local_completion_reply(tmp_path, channel, outcome):
    sessions = SessionManager(tmp_path)
    journal = sessions.execution_journal
    bus = bus_for(tmp_path, journal)
    provider = Provider()
    try:
        root = parent(journal)
        journal.reserve_spawn('child', root, 'research', {**route(), 'channel': channel, 'metadata': {}})
        child, _ = journal.admit('subagent:child', 'subagent', 'child', {})
        journal.start(child['turn_id'])
        journal.finish(child['turn_id'], 'completed', {'result_summary': 'done', 'status': 'completed', 'exit_reason': 'completed'})
        journal.finish(root, 'completed')
        if outcome == 'cancelled':
            journal.cancel_spawn('child')
        elif outcome in {'closed', 'replay'}:
            item = await bus.consume_inbound()
            if outcome == 'closed':
                consumer, _ = journal.admit_spawn_consumer('child', item.metadata['_spawn_claim'], item.session_key)
                journal.finish(consumer['turn_id'], 'interrupted')
                state = journal.get(consumer['turn_id'])
                journal.review(consumer['turn_id'], expected_seq=state['last_seq'], action_id='close', decision='close', note='stop')
            else:
                async def cached():
                    return OutboundMessage(channel, '42', 'cached reply')
                await run_spawn_recorded(journal, item, item.session_key, cached)
            item.metadata['_spawn_handled'] = False
            await bus.complete_inbound(item)
        loop = make_loop(tmp_path, sessions, bus, provider)
        original = bus.complete_inbound
        async def finish(item):
            await original(item)
            loop.stop()
        bus.complete_inbound = finish
        await asyncio.wait_for(loop.run(), 5)
        assert provider.calls == (1 if outcome == 'normal' else 0)
        assert bus.outbound_size == 1
    finally:
        close_bus(bus)
        journal.close()
        sessions._store.close()


def test_spawn_stop_notice_does_not_mark_unrelated_assistant_delivered():
    class Sessions:
        def mark_latest_assistant_delivery(self, *args, **kwargs):
            raise AssertionError('No unrelated message delivery mutation')
    ChannelHub(Sessions()).mark_delivery(
        OutboundMessage('telegram', '42', 'cancelled', metadata={'spawn_completion_notice': True}),
        default_channel='telegram', delivery_status='delivered')
