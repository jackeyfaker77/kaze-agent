import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.provider import LLMResponse
from bus.durable import Outbox, TransportStore
from bus.queue import MessageBus
from core.execution.contracts import ExecutionConflict, ExecutionUnavailable, JournalWriteError
from core.execution.journal import ExecutionJournal
from core.execution.proactive import (
    RecordedProactiveProvider, claim_candidates, observe, run_proactive_recorded, stage,
    available_candidates, defer_before_delivery,
)
from core.execution.projection import ExecutionProjectionError


@pytest.fixture
def journal(tmp_path):
    value = ExecutionJournal(tmp_path)
    yield value
    value.close()


@pytest.mark.asyncio
async def test_completed_attempt_replay_restores_projection_without_model_or_queue_writes(journal, tmp_path):
    model = SimpleNamespace(chat=AsyncMock(return_value=LLMResponse('decision')))
    provider = RecordedProactiveProvider(model)
    transport = TransportStore(tmp_path)
    box = Outbox(transport, MessageBus().chat_lane)
    async def operation():
        with stage('judge'):
            assert claim_candidates('alert', [{'id': 'once', 'ack_server': 'calendar'}])
            await provider.chat(messages=[{'role': 'user', 'content': 'remind'}], model='test')
            observe('resolve', decision='reply', evidence=['once'])
        box.enqueue('fixture', {'channel': 'telegram', 'chat_id': '7'}, 'send-once')
        return 0.5
    try:
        assert await run_proactive_recorded(journal, 's', 'tick', {}, operation) == 0.5
        original = journal.find('s', 'proactive', 'tick')
        events = journal.events(original['turn_id'])
        for _ in range(2):
            journal._conn.execute('DELETE FROM proactive_claims')
            journal._conn.commit()
            journal.rebuild_projection()
            assert await run_proactive_recorded(journal, 's', 'tick', {}, operation) == 0.5
            assert journal.get(original['turn_id']) == original
            assert journal.events(original['turn_id']) == events
        assert model.chat.await_count == 1
        assert transport.db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
        assert transport.db.execute('SELECT turn_id FROM transport_execution_links').fetchone()[0] == original['turn_id']
        assert journal._conn.execute('SELECT count(*) FROM proactive_claims').fetchone()[0] == 1
        with pytest.raises(ExecutionConflict):
            await run_proactive_recorded(journal, 's', 'tick', {'different': True}, operation)
    finally:
        transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['proactive.stage.started', 'proactive.model.requested', 'proactive.model.returned'])
async def test_journal_failure_fences_model_and_outbox(journal, tmp_path, monkeypatch, boundary):
    model = SimpleNamespace(chat=AsyncMock(return_value=LLMResponse('decision')))
    transport = TransportStore(tmp_path)
    box = Outbox(transport, MessageBus().chat_lane)
    append = journal.append
    def broken(turn, kind, payload):
        if kind == boundary:
            raise OSError('disk full')
        append(turn, kind, payload)
    monkeypatch.setattr(journal, 'append', broken)
    async def operation():
        try:
            with stage('judge'):
                await RecordedProactiveProvider(model).chat(model='test', messages=[])
        except JournalWriteError:
            # Even a plugin swallowing this exception cannot enqueue a send.
            pass
        box.enqueue('fixture', {'channel': 'telegram', 'chat_id': '7'})
    try:
        with pytest.raises(JournalWriteError):
            await run_proactive_recorded(journal, 's', 'tick', {}, operation)
        assert model.chat.await_count == int(boundary == 'proactive.model.returned')
        assert transport.db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
        with pytest.raises(ExecutionUnavailable):
            await run_proactive_recorded(journal, 's', 'tick', {}, operation)
    finally:
        transport.close()


@pytest.mark.asyncio
async def test_cancelled_model_is_interrupted_and_blocks_new_scheduled_work(journal):
    entered = asyncio.Event()
    async def wait(**kwargs):
        entered.set()
        await asyncio.Future()
    provider = RecordedProactiveProvider(SimpleNamespace(chat=wait))
    task = asyncio.create_task(run_proactive_recorded(journal, 's', 'tick', {},
        lambda: provider.chat(model='test', messages=[])))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = journal.proactive_blocker('s')
    assert row['status'] == 'interrupted'
    journal.rebuild_projection()
    assert journal.proactive_blocker('s') == row
    journal.review(row['turn_id'], expected_seq=row['last_seq'], action_id='review', decision='close', note='checked')
    assert journal.proactive_blocker('s') is None


def test_proactive_admission_does_not_block_passive_chat_and_retains_both_uniqueness_guards(journal):
    proactive, _ = journal.admit('s', 'proactive', 'tick', {})
    chat, _ = journal.admit('s', 'desktop', 'input', {})
    for source in ('proactive', 'desktop', 'telegram'):
        with pytest.raises(ExecutionConflict):
            journal.admit('s', source, 'second', {})
    journal.rebuild_projection()
    assert journal.get(proactive['turn_id'])['status'] == journal.get(chat['turn_id'])['status'] == 'accepted'


@pytest.mark.parametrize('kind,payload', [
    ('proactive.stage.completed', {'stage_id': 'missing'}),
    ('proactive.model.returned', {'call_id': 'missing', 'response': {}}),
    ('proactive.observed', {'stage': 'resolve', 'detail': 'invalid'}),
])
def test_invalid_proactive_prefix_fails_closed(journal, kind, payload):
    row, _ = journal.admit('s', 'proactive', 'tick', {})
    journal.start(row['turn_id'])
    with pytest.raises(ExecutionProjectionError):
        journal.append(row['turn_id'], kind, payload)
    assert journal.get(row['turn_id'])['last_seq'] == 2


@pytest.mark.asyncio
async def test_candidate_claim_survives_projection_loss_and_requires_explicit_close(journal):
    async def failed():
        assert claim_candidates('alert', [{'id': 'one', 'ack_server': 'calendar'}])
        raise ValueError('model failed')
    with pytest.raises(ValueError):
        await run_proactive_recorded(journal, 's', 'first', {}, failed)
    original = journal.find('s', 'proactive', 'first')
    async def duplicate():
        assert not claim_candidates('alert', [{'id': 'one', 'ack_server': 'calendar'}])
    for n in range(2):
        journal._conn.execute('DELETE FROM proactive_claims')
        journal._conn.commit()
        journal.rebuild_projection()
        await run_proactive_recorded(journal, 's', f'next-{n}', {}, duplicate)
    journal.review(original['turn_id'], expected_seq=original['last_seq'], decision='close', action_id='close', note='checked')
    async def fresh():
        assert claim_candidates('alert', [{'id': 'one', 'ack_server': 'calendar'}])
    await run_proactive_recorded(journal, 's', 'after-review', {}, fresh)


@pytest.mark.asyncio
async def test_old_candidate_does_not_starve_new_version_or_unrelated_candidate(journal):
    old = {'id': 'stable-item', 'event_id': 'version-1', 'ack_server': 'calendar'}
    new = {**old, 'event_id': 'version-2'}
    other = {'id': 'other-item', 'ack_server': 'calendar'}
    async def first():
        assert claim_candidates('alert', [old])
    await run_proactive_recorded(journal, 's', 'first', {}, first)
    journal.rebuild_projection()
    async def second():
        assert available_candidates('alert', [old, new, other]) == [new, other]
        assert claim_candidates('alert', [new, other])
    await run_proactive_recorded(journal, 's', 'next', {}, second)


@pytest.mark.asyncio
async def test_pre_delivery_deferral_rebuilds_without_replaying_model(journal):
    model = SimpleNamespace(chat=AsyncMock(return_value=LLMResponse('decision')))
    async def deferred():
        assert claim_candidates('alert', [{'id': 'one'}])
        await RecordedProactiveProvider(model).chat(model='test', messages=[])
        defer_before_delivery()
        return 0.1
    await run_proactive_recorded(journal, 's', 'first', {}, deferred)
    original = journal.find('s', 'proactive', 'first')
    for _ in range(2):
        journal.rebuild_projection()
        assert await run_proactive_recorded(journal, 's', 'first', {}, deferred) == 0.1
        assert journal.get(original['turn_id']) == original
        assert journal._conn.execute('SELECT count(*) FROM proactive_claims').fetchone()[0] == 0
    assert model.chat.await_count == 1
    async def later():
        assert claim_candidates('alert', [{'id': 'one'}])
    await run_proactive_recorded(journal, 's', 'new-quiet-tick', {}, later)
    journal.rebuild_projection()
    assert journal._conn.execute('SELECT count(*) FROM proactive_claims').fetchone()[0] == 1


@pytest.mark.parametrize('boundary', ['queue.enqueued', 'queue.linked', 'delivery'])
def test_claim_release_cannot_bypass_queued_or_physical_delivery(journal, boundary):
    row, _ = journal.admit('s', 'proactive', 'tick', {})
    turn = row['turn_id']
    journal.start(turn)
    assert journal.claim_proactive(turn, ['candidate'])
    if boundary == 'delivery':
        journal.append(turn, 'delivery.dispatched', {'effect_id': 'send'})
    else:
        journal.append(turn, 'proactive.observed', {'stage': boundary, 'detail': {'job_id': 'send'}})
    with pytest.raises(ExecutionProjectionError):
        journal.append(turn, 'proactive.candidates.released', {
            'candidates': ['candidate'], 'reason': 'user_activity_before_delivery',
        })
    journal.rebuild_projection()
    assert not journal.available_proactive_candidates('s', ['candidate'])[0]
