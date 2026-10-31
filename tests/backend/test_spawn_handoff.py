"""P0a: real SQLite, fake work, process death; never touch a running Host."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any, cast

import pytest

from agent.background.subagent_manager import SubagentManager
from agent.background.runtime import AgentBackgroundJobResult
from agent.looping.core import AgentLoop
from agent.looping.ports import AgentLoopConfig, AgentLoopDeps, LLMConfig, MemoryServices
from agent.provider import LLMResponse
from agent.tools.registry import ToolRegistry
from bus.events import OutboundMessage
from bus.queue import MessageBus
from core.execution.contracts import ExecutionConflict
from core.execution.journal import ExecutionJournal
from core.execution.runner import run_spawn_recorded
from core.execution.scope import ExecutionScope, current_execution
from session.manager import SessionManager
from tests.backend.memory_fakes import FakeMemoryEngine


def parent(journal):
    row, _ = journal.admit('telegram:42', 'telegram', 'request', {'content': 'do work'})
    journal.start(row['turn_id'])
    return row['turn_id']


def route():
    return {'channel': 'telegram', 'chat_id': '42', 'label': 'report', 'task': 'do work',
            'timestamp': '2026-11-01T00:00:00+00:00', 'metadata': {'_spawn_account': 'test-account'}}


def completed_child(journal, parent_id, job='child', reason='completed', profile='research'):
    reservation = journal.reserve_spawn(job, parent_id, profile, route())
    row, _ = journal.admit(f'subagent:{job}', 'subagent', job, {'task': 'do work'})
    journal.start(row['turn_id'])
    result = AgentBackgroundJobResult(job, 'conversation_spawn', 'report',
        'completed' if reason == 'completed' else 'incomplete', reason, 'answer', '', '',
        'message_bus', 'ephemeral')
    journal.finish(row['turn_id'], 'completed' if reason == 'completed' else 'failed', asdict(result))
    return row['turn_id'], reservation


def bus_for(tmp_path, journal):
    bus = MessageBus()
    bus.enable_durability(tmp_path, journal)
    bus.outbox.accounts['telegram'] = 'test-account'
    return bus


def close_bus(bus):
    bus.transport.close()


def test_terminal_and_intent_are_atomic_and_projection_rebuild_preserves_receipt(tmp_path):
    journal = ExecutionJournal(tmp_path)
    try:
        root = parent(journal)
        journal.reserve_spawn('child', root, 'research', route())
        child, _ = journal.admit('subagent:child', 'subagent', 'child', {'task': 'work'})
        journal.start(child['turn_id'])
        journal._conn.execute("CREATE TRIGGER reject_completion BEFORE INSERT ON spawn_completions "
                              "BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
        with pytest.raises(sqlite3.IntegrityError):
            journal.finish(child['turn_id'], 'completed', {'result_summary': 'done', 'exit_reason': 'completed'})
        assert journal.get(child['turn_id'])['status'] == 'running'
        journal._conn.execute('DROP TRIGGER reject_completion')
        journal.finish(child['turn_id'], 'completed', {'result_summary': 'done', 'exit_reason': 'completed'})
        assert journal.spawn_completion('child')['payload']['event']['child_turn_id'] == child['turn_id']
        journal.finish(root, 'completed')
        claim = journal.claim_spawn_completion()
        consumer, _ = journal.admit_spawn_consumer('child', claim['claim_token'], 'telegram:42')
        journal.start(consumer['turn_id'])
        journal.finish(consumer['turn_id'], 'completed', asdict(OutboundMessage('telegram', '42', 'reply')))
        journal.settle_spawn_completion('child', claim['claim_token'])
        journal.rebuild_projection()
        journal.reconcile_spawn_completions()
        assert journal.claim_spawn_completion() is None
        assert journal.spawn_completion('child')['acked'] == 1
    finally:
        journal.close()


def test_concurrent_claim_is_unique_live_owner_fenced_and_stale_token_rejected(tmp_path):
    first = ExecutionJournal(tmp_path)
    second = None
    try:
        root = parent(first)
        completed_child(first, root)
        first.finish(root, 'completed')
        second = ExecutionJournal(tmp_path)
        with ThreadPoolExecutor(2) as pool:
            claims = list(pool.map(lambda j: j.claim_spawn_completion(), [first, second]))
        assert sum(c is not None for c in claims) == 1
        owner, other = (first, second) if claims[0] else (second, first)
        claim = claims[0] or claims[1]
        assert other.claim_spawn_completion() is None
        owner.close()
        new_claim = other.claim_spawn_completion()
        assert new_claim and new_claim['claim_token'] != claim['claim_token']
        with pytest.raises(ExecutionConflict):
            other.admit_spawn_consumer('child', claim['claim_token'], 'telegram:42')
    finally:
        first.close()
        if second:
            second.close()


def test_live_peer_recovers_admitted_only_consumer_after_owner_exit(tmp_path):
    first = ExecutionJournal(tmp_path)
    second = None
    try:
        root = parent(first)
        completed_child(first, root)
        first.finish(root, 'completed')
        claim = first.claim_spawn_completion()
        consumer, _ = first.admit_spawn_consumer('child', claim['claim_token'], 'telegram:42')
        second = ExecutionJournal(tmp_path)
        first.close()
        new_claim = second.claim_spawn_completion()
        recovered, created = second.admit_spawn_consumer('child', new_claim['claim_token'], 'telegram:42')
        assert created and recovered['turn_id'] == consumer['turn_id']
        second.start(recovered['turn_id'])
        second.finish(recovered['turn_id'], 'completed')
        second.settle_spawn_completion('child', new_claim['claim_token'])
        assert second.settle_spawn_completion('child', new_claim['claim_token'])
        second.rebuild_projection()
    finally:
        first.close()
        if second:
            second.close()


@pytest.mark.asyncio
async def test_admission_conflict_releases_claim_instead_of_acknowledging_result(tmp_path):
    journal = ExecutionJournal(tmp_path)
    bus = bus_for(tmp_path, journal)
    try:
        root = parent(journal)
        completed_child(journal, root)
        item = await bus.consume_inbound()
        with pytest.raises(ExecutionConflict):
            journal.admit_spawn_consumer('child', item.metadata['_spawn_claim'], 'telegram:42')
        await bus.complete_inbound(item)
        assert journal.spawn_completion('child')['acked'] == 0
        journal.finish(root, 'completed')
        retry = await bus.consume_inbound()
        assert retry.metadata['_spawn_claim'] != item.metadata['_spawn_claim']
        async def operation():
            return OutboundMessage('telegram', '42', 'reply')
        await run_spawn_recorded(journal, retry, retry.session_key, operation)
        await bus.complete_inbound(retry)
        reply = bus.transport.get('reply:spawn:child')
        assert reply['payload']['content'] == 'reply'
        assert bus.transport.db.execute('SELECT turn_id FROM transport_execution_links WHERE job=?',
            ('reply:spawn:child',)).fetchone()[0] == journal.spawn_completion('child')['consumer_turn_id']
        assert journal.spawn_completion('child')['acked'] == 1
    finally:
        close_bus(bus)
        journal.close()


@pytest.mark.asyncio
async def test_actual_background_retry_uses_same_parent_and_ignores_model_counter(tmp_path):
    journal = ExecutionJournal(tmp_path)
    bus = bus_for(tmp_path, journal)
    manager = SubagentManager(provider=cast(Any, object()), workspace=tmp_path, bus=bus,
                              model='fake', max_tokens=100, fetch_requester=cast(Any, object()))
    manager.execution_journal = journal
    class Child:
        last_exit_reason = 'max_iterations'
        calls = 0
        async def run(self, task):
            Child.calls += 1
            return 'partial'
    manager._build_subagent = lambda **kwargs: Child()
    try:
        root = parent(journal)
        token = current_execution.set(ExecutionScope(journal, root))
        try:
            await manager.spawn(task='first', label='first', retry_count=999,
                                origin_channel='telegram', origin_chat_id='42')
        finally:
            current_execution.reset(token)
        journal.finish(root, 'completed')
        await asyncio.gather(*list(manager._running_tasks.values()))
        first = await bus.consume_inbound()
        assert first.event.retry_count == 0
        async def supplement():
            await manager.spawn(task='different task', label='different', retry_count=0,
                                origin_channel='telegram', origin_chat_id='42')
            return OutboundMessage('telegram', '42', 'supplement started')
        await run_spawn_recorded(journal, first, first.session_key, supplement)
        await bus.complete_inbound(first)
        await asyncio.gather(*list(manager._running_tasks.values()))
        second = await bus.consume_inbound()
        assert second.event.parent_task_id == first.event.parent_task_id == root
        assert second.event.parent_turn_id == journal.spawn_completion(first.event.job_id)['consumer_turn_id']
        assert second.event.retry_count == 1
        assert second.event.artifact_refs
        async def blocked():
            with pytest.raises(ExecutionConflict, match='额度'):
                await manager.spawn_sync(task='try synchronous bypass', label='reset')
            return OutboundMessage('telegram', '42', 'partial results')
        await run_spawn_recorded(journal, second, second.session_key, blocked)
        await bus.complete_inbound(second)
        assert Child.calls == 2
    finally:
        close_bus(bus)
        journal.close()


@pytest.mark.asyncio
async def test_restart_cannot_restore_exhausted_dispatch_allowance(tmp_path):
    journal = ExecutionJournal(tmp_path)
    bus = None
    try:
        root = parent(journal)
        completed_child(journal, root, reason='max_iterations')
        journal.finish(root, 'completed')
        first = journal.claim_spawn_completion()
        consumer, _ = journal.admit_spawn_consumer('child', first['claim_token'], 'telegram:42')
        journal.start(consumer['turn_id'])
        completed_child(journal, consumer['turn_id'], job='second', reason='tool_loop')
        journal.finish(consumer['turn_id'], 'completed')
        journal.settle_spawn_completion('child', first['claim_token'])
        journal.close()
        journal = ExecutionJournal(tmp_path)
        journal.rebuild_projection()
        bus = bus_for(tmp_path, journal)
        manager = SubagentManager(provider=cast(Any, object()), workspace=tmp_path, bus=bus,
                                  model='fake', max_tokens=100, fetch_requester=cast(Any, object()))
        manager.execution_journal = journal
        item = await bus.consume_inbound()
        assert item.event.job_id == 'second'
        async def blocked():
            with pytest.raises(ExecutionConflict, match='额度'):
                await manager.spawn(task='reset after restart', label='new', retry_count=0,
                                    origin_channel='telegram', origin_chat_id='42')
            with pytest.raises(ExecutionConflict, match='额度'):
                await manager.spawn_sync(task='sync after restart', label='new')
            return OutboundMessage('telegram', '42', 'budget exhausted')
        await run_spawn_recorded(journal, item, item.session_key, blocked)
        await bus.complete_inbound(item)
    finally:
        if bus:
            close_bus(bus)
        journal.close()


@pytest.mark.asyncio
async def test_retry_budget_is_host_owned_durable_and_cannot_switch_to_sync(tmp_path):
    journal = ExecutionJournal(tmp_path)
    bus = bus_for(tmp_path, journal)
    manager = SubagentManager(provider=cast(Any, object()), workspace=tmp_path, bus=bus,
                              model='fake', max_tokens=100, fetch_requester=cast(Any, object()))
    manager.execution_journal = journal
    try:
        root = parent(journal)
        completed_child(journal, root, reason='max_iterations')
        journal.finish(root, 'completed')
        item = await bus.consume_inbound()
        consumer, _ = journal.admit_spawn_consumer('child', item.metadata['_spawn_claim'], item.session_key)
        journal.start(consumer['turn_id'])
        token = current_execution.set(ExecutionScope(journal, consumer['turn_id']))
        async def never_run(**kwargs):
            raise AssertionError('budget must block before model work')
        manager._build_subagent = never_run
        try:
            # Profile widening is blocked without charging a dispatch.
            with pytest.raises(ExecutionConflict, match='工具权限'):
                await manager.spawn(task='changed', label='changed', profile='general', retry_count=0,
                                    origin_channel='telegram', origin_chat_id='42')
            # A synchronous followup consumes the very same allowance.
            journal.reserve_spawn('supplement', consumer['turn_id'], 'research', route())
            with pytest.raises(ExecutionConflict, match='额度'):
                await manager.spawn_sync(task='new task and label', label='different')
            with pytest.raises(ExecutionConflict, match='额度'):
                await manager.spawn(task='new parameters', label='different', retry_count=0,
                                    origin_channel='telegram', origin_chat_id='42')
        finally:
            current_execution.reset(token)
        journal.finish(consumer['turn_id'], 'completed')
        close_bus(bus)
        journal.close()
        journal = ExecutionJournal(tmp_path)
        # Count does not reset after projection rebuild or runtime restart.
        assert journal.spawn_parent_context(consumer['turn_id'])['parent_task_id'] == root
        assert journal._conn.execute('SELECT count(*) FROM spawn_tasks WHERE parent_task_id=?', (root,)).fetchone()[0] == 2
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_cancel_before_first_coroutine_step_persists_result_and_revokes_budget(tmp_path):
    journal = ExecutionJournal(tmp_path)
    bus = bus_for(tmp_path, journal)
    manager = SubagentManager(provider=cast(Any, object()), workspace=tmp_path, bus=bus,
                              model='fake', max_tokens=100, fetch_requester=cast(Any, object()))
    manager.execution_journal = journal
    try:
        root = parent(journal)
        token = current_execution.set(ExecutionScope(journal, root))
        try:
            await manager.spawn(task='work', label='job', origin_channel='telegram', origin_chat_id='42')
            job = manager.list_running_jobs()[0]['job_id']
            assert await manager.cancel(job)
            with pytest.raises(ExecutionConflict, match='取消'):
                await manager.spawn(task='new', label='new', retry_count=0,
                                    origin_channel='telegram', origin_chat_id='42')
        finally:
            current_execution.reset(token)
        journal.finish(root, 'completed')
        item = await bus.consume_inbound()
        assert item.event.exit_reason == 'cancelled'
        assert journal.spawn_completion(job)['payload']['event']['parent_task_id'] == root
        # A cancelled completion is a notification; never another model loop.
    finally:
        close_bus(bus)
        journal.close()


@pytest.mark.asyncio
async def test_real_loop_consumes_once_and_replays_without_calling_fake_provider(tmp_path):
    class Provider:
        calls = 0
        async def chat(self, **kwargs):
            self.calls += 1
            return LLMResponse(content='已整理完成。', tool_calls=[])
    provider = Provider()
    sessions = SessionManager(tmp_path)
    journal = sessions.execution_journal
    bus = bus_for(tmp_path, journal)
    try:
        root = parent(journal)
        child, _ = completed_child(journal, root)
        journal.finish(root, 'completed')
        loop = AgentLoop(AgentLoopDeps(bus=bus, provider=cast(Any, provider), tools=ToolRegistry(),
            session_manager=sessions, workspace=tmp_path,
            memory_services=MemoryServices(engine=FakeMemoryEngine(tmp_path))),
            AgentLoopConfig(llm=LLMConfig(max_iterations=3)))
        item = await bus.consume_inbound()
        first = await loop._process_session_scoped(item, item.session_key, dispatch_outbound=True)
        second = await loop._process_session_scoped(item, item.session_key, dispatch_outbound=True)
        assert first == second
        assert provider.calls == 1
        assert len(sessions.get_or_create(item.session_key).messages) == 2
        receipt = journal.spawn_completion('child')
        assert receipt['payload']['event']['child_turn_id'] == child
        assert journal.get(receipt['consumer_turn_id'])['source'] == 'spawn_completion'
        await bus.complete_inbound(item)
        assert journal.spawn_completion('child')['acked'] == 1
        assert bus.transport.db.execute("SELECT count(*) FROM jobs WHERE kind='bus:telegram'").fetchone()[0] == 1
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(bus.consume_inbound(), .15)
    finally:
        close_bus(bus)
        journal.close()


@pytest.mark.parametrize('reason', ['needs_review', 'cancelled'])
@pytest.mark.asyncio
async def test_safety_stop_completion_never_calls_parent_model(tmp_path, reason):
    class Provider:
        async def chat(self, **kwargs):
            raise AssertionError('Safety stops must not invoke a model')
    sessions = SessionManager(tmp_path)
    journal = sessions.execution_journal
    bus = bus_for(tmp_path, journal)
    try:
        root = parent(journal)
        completed_child(journal, root, reason=reason)
        journal.finish(root, 'completed')
        loop = AgentLoop(AgentLoopDeps(bus=bus, provider=cast(Any, Provider()), tools=ToolRegistry(),
            session_manager=sessions, workspace=tmp_path,
            memory_services=MemoryServices(engine=FakeMemoryEngine(tmp_path))),
            AgentLoopConfig(llm=LLMConfig(max_iterations=3)))
        item = await bus.consume_inbound()
        await loop._process_session_scoped(item, item.session_key, dispatch_outbound=False)
        await bus.complete_inbound(item)
        assert journal.spawn_completion('child')['acked'] == 1
    finally:
        close_bus(bus)
        journal.close()


_CRASH_SCRIPT = r'''
import asyncio, json, os, sys
from pathlib import Path
from dataclasses import asdict
from agent.background.subagent_manager import SubagentManager
from bus.events import OutboundMessage
from bus.queue import MessageBus
from core.execution.journal import ExecutionJournal
from core.execution.runner import run_spawn_recorded
from core.execution.scope import ExecutionScope, current_execution

workspace, point, phase = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
journal = ExecutionJournal(workspace)
bus = MessageBus()
bus.enable_durability(workspace, journal)
bus.outbox.accounts['telegram'] = 'test-account'
def count(name):
    with (workspace / (name + '.txt')).open('a', encoding='utf-8') as f:
        f.write('1\n'); f.flush(); os.fsync(f.fileno())
async def action(name):
    count(name)
    if name == 'child_action' and point == 'child_unknown' and phase == 'crash':
        os._exit(71)
    return 'written'
class FakeChild:
    last_exit_reason = 'completed'
    async def run(self, task):
        count('child_model')
        await current_execution.get().effect('tool', {'name':'isolated_write', 'retry_policy':'never'},
            lambda: action('child_action'))
        return 'answer'
async def parent_operation():
    count('parent_model')
    await current_execution.get().effect('tool', {'name':'isolated_write', 'retry_policy':'never'},
        lambda: action('parent_action'))
    if point == 'after_parent_action' and phase == 'crash':
        os._exit(71)
    return OutboundMessage('telegram', '42', 'reply')
async def main():
    if phase == 'crash':
        root, _ = journal.admit('telegram:42', 'telegram', 'input', {'content':'work'})
        journal.start(root['turn_id'])
        manager = SubagentManager(provider=object(), workspace=workspace, bus=bus,
            model='fake', max_tokens=10, fetch_requester=object())
        manager.execution_journal = journal
        manager._build_subagent = lambda **kwargs: FakeChild()
        if point == 'after_terminal':
            original_finish = journal.finish
            def finish(turn, status, result=None):
                original_finish(turn, status, result)
                if journal.get(turn)['source'] == 'subagent': os._exit(71)
            journal.finish = finish
        if point == 'before_enqueue':
            async def announce(**kwargs): os._exit(71)
            manager._announce_result = announce
        token = current_execution.set(ExecutionScope(journal, root['turn_id']))
        await manager.spawn(task='work', label='job', origin_channel='telegram', origin_chat_id='42')
        current_execution.reset(token)
        journal.finish(root['turn_id'], 'completed')
        await asyncio.gather(*list(manager._running_tasks.values()))
        if point == 'after_acceptance':
            original_admit = journal.admit_spawn_consumer
            def admit(*args):
                value = original_admit(*args)
                (workspace / 'consumer_id.txt').write_text(value[0]['turn_id'])
                os._exit(71)
            journal.admit_spawn_consumer = admit
    item = await asyncio.wait_for(bus.consume_inbound(), 3)
    async def notify():
        return OutboundMessage('telegram', '42', item.event.result)
    result = await run_spawn_recorded(journal, item, item.session_key,
        notify if item.event.exit_reason == 'needs_review' else parent_operation)
    consumer = journal.spawn_completion(item.event.job_id)['consumer_turn_id']
    if (workspace / 'consumer_id.txt').exists():
        assert (workspace / 'consumer_id.txt').read_text() == consumer
    if point == 'before_ack' and phase == 'crash': os._exit(71)
    await bus.complete_inbound(item)
    assert journal.spawn_completion(item.event.job_id)['acked'] == 1
    assert journal.claim_spawn_completion() is None
    print(json.dumps({'result':result.content, 'consumer':consumer}))
    bus.transport.close()
    journal.close()
asyncio.run(main())
'''


@pytest.mark.parametrize('point', ['after_terminal', 'before_enqueue', 'after_acceptance', 'before_ack', 'after_parent_action', 'child_unknown'])
def test_process_death_handoff_never_repeats_model_or_external_actions(tmp_path, point):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path('apps/backend').resolve()), str(Path.cwd())]))
    command = [sys.executable, '-c', _CRASH_SCRIPT, str(tmp_path), point]
    crashed = subprocess.run(command + ['crash'], env=env, capture_output=True, text=True, timeout=30)
    assert crashed.returncode == 71, crashed.stdout + crashed.stderr
    recovered = subprocess.run(command + ['recover'], env=env, capture_output=True, text=True, timeout=30)
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    for name in ('child_model', 'child_action', 'parent_model', 'parent_action'):
        if point == 'child_unknown' and name.startswith('parent'):
            assert not (tmp_path / f'{name}.txt').exists()
            continue
        assert (tmp_path / f'{name}.txt').read_text().splitlines() == ['1']
    if point == 'after_parent_action':
        assert '不会自动重跑' in json.loads(recovered.stdout)['result']
