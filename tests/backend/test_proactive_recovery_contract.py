import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bus.chat_lane import ChatLane
from bus.durable import Outbox, TransportStore, DeliveryStopped
from core.execution.journal import ExecutionJournal
from core.execution.scope import ExecutionScope, current_execution
from core.execution.proactive_recovery import delivery_evidence
from infra.channels.telegram_channel.durable import _DurableMixin
from plugins.wake_proactive.state import WakeStateStore


@pytest.mark.asyncio
@pytest.mark.parametrize('allow_remaining', [True, False])
async def test_real_telegram_chunk_manifest_distinguishes_partial_receipt(tmp_path, allow_remaining):
    store = TransportStore(tmp_path)
    journal = ExecutionJournal(tmp_path)
    box = Outbox(store, ChatLane())
    sent = []
    async def send_message(**kwargs):
        sent.append(kwargs['text'])
        return SimpleNamespace(message_id=len(sent))
    async def limited(*args, action, **kwargs):
        return await action()
    channel = SimpleNamespace(_bus=SimpleNamespace(outbox=box), _resolve_chat_id=str,
        _app=SimpleNamespace(bot=SimpleNamespace(send_message=send_message)),
        _telegram_outbound_limiter=SimpleNamespace(run=limited))
    async def handler(payload):
        result = await _DurableMixin._durable_send(channel, 'text', payload['chat_id'], text=payload['message'])
        return {'message_id': result.message_id}
    box.handlers['push:telegram'] = handler
    row, _ = journal.admit('telegram:1', 'proactive', 'original', {})
    journal.start(row['turn_id'])
    scope = ExecutionScope(journal, row['turn_id'])
    scope.proactive = True
    token = current_execution.set(scope)
    part = box.part
    async def crash_after_receipt(*args):
        await part(*args)
        raise InterruptedError('process boundary')
    box.part = crash_after_receipt
    try:
        key = box.enqueue('push:telegram', {'channel': 'telegram', 'chat_id': '1', 'message': 'abcdef ' * 1400})
        with pytest.raises(InterruptedError):
            await box.deliver(key)
        assert len(sent) == 1
        parts, complete, known = delivery_evidence(store, store.get(key))
        assert known and not complete and len(parts) == 1
        expected = json.loads(store.db.execute('SELECT payload FROM part_manifests').fetchone()[0])
        assert len(expected) > 1
        box.part = part
        async def guard():
            return allow_remaining
        scope.proactive_host_guard = guard
        store.state(key, 'pending')
        if allow_remaining:
            await box.deliver(key)
            assert len(sent) == len(expected)
            assert delivery_evidence(store, store.get(key))[1]
        else:
            with pytest.raises(DeliveryStopped, match='revalidation'):
                await box.deliver(key)
            assert len(sent) == 1
    finally:
        current_execution.reset(token)
        journal.close()
        store.close()


@pytest.mark.asyncio
async def test_restart_worker_and_manual_delivery_cannot_bypass_proactive_confirmation(tmp_path):
    store = TransportStore(tmp_path)
    box = Outbox(store, ChatLane())
    calls = []
    async def physical():
        calls.append('sent')
    async def handler(payload):
        await box.part('0:text', {'text': 'old'}, physical)
    box.handlers['push:telegram'] = handler
    # Legacy pending proactive rows also require a recovery contract; a manual
    # queue retry cannot silently authorize their old delivery guard.
    key = box.enqueue('push:telegram', {'channel': 'telegram', 'chat_id': '1', 'message': 'old',
        '_outbound_metadata': {'source': 'proactive'}})
    task = asyncio.create_task(box.run())
    try:
        await asyncio.sleep(.3)
        assert store.get(key)['attempts'] == 0 and calls == []
        with pytest.raises(DeliveryStopped, match='confirmation'):
            await box.deliver(key)
        assert calls == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        store.close()


def test_finalization_receipt_is_atomic_and_ack_is_not_requeued(tmp_path):
    state = WakeStateStore(tmp_path / 'wake.db')
    args = dict(item_ids=[], acknowledgements={'source': ['event']}, now=datetime.now(timezone.utc), delivery_id='stable')
    try:
        state._conn.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON event_finalizations BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
        with pytest.raises(Exception, match='disk failure'):
            state.consume_and_queue_ack(**args)
        assert state.pending_acknowledgements() == {} and state.event_finalization('stable') is None
        state._conn.execute('DROP TRIGGER fail_receipt')
        state.consume_and_queue_ack(**args)
        state.begin_acknowledgement('source', ['event'])
        assert state.unknown_acknowledgements()
        state.mark_acknowledged('source', ['event'])
        state.consume_and_queue_ack(**args)
        assert state.event_finalization('stable')['completed']
        assert not state.pending_acknowledgements()
    finally:
        state.close()


def test_manual_queue_statement_does_not_authorize_unknown_proactive_send(tmp_path):
    store = TransportStore(tmp_path)
    try:
        payload = {'channel': 'telegram', 'chat_id': '1', 'message': 'unknown', '_proactive_owner': 'source'}
        store.put('unknown', 'push:telegram', payload)
        with store.db:
            store.db.execute("INSERT INTO parts VALUES ('unknown','0:text','{}','sending',NULL)")
        store.state('unknown', 'needs_review')
        detail = store.detail('unknown', handlers={'push:telegram': object()}, accounts={})
        assert not detail['retry']['allowed']
        with pytest.raises(ValueError, match='未知发送'):
            store.review_job('unknown', expected_revision=detail['revision'], decision='retry',
                         action_id='human', note='operator statement', absent_parts=['0:text'],
                         handlers={'push:telegram': object()}, accounts={})
        assert store.db.execute('SELECT state FROM parts').fetchone()[0] == 'sending'
        assert store.db.execute('SELECT count(*) FROM transport_reviews').fetchone()[0] == 0
    finally:
        store.close()
