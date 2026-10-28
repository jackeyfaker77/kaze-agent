import asyncio
from types import SimpleNamespace

import httpx
import pytest
from telegram.error import NetworkError, RetryAfter

from bus.chat_lane import ChatLane
from bus.durable import Outbox, TransportStore, DeliveryStopped
from infra.channels.telegram_channel.durable import _DurableMixin


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['connect_error', 'connect_timeout', 'pool_timeout', 'rate_limited', 'unknown'])
async def test_telegram_attempt_evidence_survives_success_and_restart(tmp_path, monkeypatch, kind, request):
    monkeypatch.setenv('PTB_TIMEDELTA', '1')
    store = TransportStore(tmp_path)
    request.addfinalizer(store.close)
    box = Outbox(store, ChatLane())
    calls = []
    async def send_message(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            if kind == 'rate_limited':
                raise RetryAfter(2)
            error = {'connect_error': httpx.ConnectError, 'connect_timeout': httpx.ConnectTimeout,
                     'pool_timeout': httpx.PoolTimeout, 'unknown': httpx.ReadTimeout}[kind]
            raise NetworkError('SECRET_BOT_TOKEN') from error('SECRET_BOT_TOKEN')
        return SimpleNamespace(message_id=42)
    async def limited(*args, action, **kwargs):
        return await action()
    channel = SimpleNamespace(_bus=SimpleNamespace(outbox=box), _resolve_chat_id=str,
        _app=SimpleNamespace(bot=SimpleNamespace(send_message=send_message)),
        _telegram_outbound_limiter=SimpleNamespace(run=limited))
    async def handler(payload):
        result = await _DurableMixin._durable_send(channel, 'text', '1', text='probe')
        return {'message_id': result.message_id}
    box.handlers['test'] = handler
    # Skip wall-clock waits while preserving the persisted delay for assertions.
    import bus.durable as module
    real_time = module.time.time
    offset = 0
    async def fast_sleep(delay):
        nonlocal offset
        offset += delay
    monkeypatch.setattr(module.time, 'time', lambda: real_time() + offset)
    monkeypatch.setattr(asyncio, 'sleep', fast_sleep)
    key = box.enqueue('test', {'channel': 'telegram', 'chat_id': '1'}, 'probe')
    if kind == 'unknown':
        with pytest.raises(NetworkError):
            await box.deliver(key)
    else:
        await box.deliver(key)
    evidence = store.detail(key)['attempt_history']
    assert evidence[0]['reason'] == ('outcome_unknown' if kind == 'unknown' else kind)
    assert evidence[0]['finished'] >= evidence[0]['started']
    assert 'SECRET' not in str(evidence)
    if kind != 'unknown':
        assert evidence[0]['retry_delay'] == (2 if kind == 'rate_limited' else 1)
        assert evidence[1]['outcome'] == 'done'
    store.close()
    store = TransportStore(tmp_path)
    box = Outbox(store, ChatLane())
    box.handlers['test'] = handler
    try:
        assert store.detail(key)['attempt_history'] == evidence
        if kind == 'unknown':
            with pytest.raises(DeliveryStopped):
                await box.deliver(key)
            assert len(calls) == 1
        else:
            assert await box.deliver(key) == {'message_id': 42}
            assert len(calls) == 2
        assert store.detail(key)['attempt_history'] == evidence
    finally:
        store.close()


def test_existing_queue_has_no_fabricated_attempt_history(tmp_path):
    store = TransportStore(tmp_path)
    store.put('old', 'test', {})
    store.db.execute("UPDATE jobs SET attempts=4,state='done' WHERE id='old'")
    store.db.commit()
    store.close()
    store = TransportStore(tmp_path)
    try:
        assert store.detail('old')['attempt_history'] == []
        assert store.get('old')['attempts'] == 4
    finally:
        store.close()


@pytest.mark.asyncio
async def test_interrupted_attempt_stays_unfinished_and_cannot_send(tmp_path):
    store = TransportStore(tmp_path)
    store.put('interrupted', 'test', {})
    store.attempt('interrupted')
    store.db.execute("INSERT INTO parts VALUES ('interrupted','0','{}','sending',NULL)")
    store.db.commit()
    store.close()
    store = TransportStore(tmp_path)
    try:
        box = Outbox(store, ChatLane())
        async def forbidden(payload):
            pytest.fail('Unknown send must not be replayed')
        box.handlers['test'] = forbidden
        with pytest.raises(DeliveryStopped):
            await box.deliver('interrupted')
        attempt = store.detail('interrupted')['attempt_history'][0]
        assert attempt['finished'] is None and attempt['outcome'] is None
        assert store.get('interrupted')['attempts'] == 1
    finally:
        store.close()
