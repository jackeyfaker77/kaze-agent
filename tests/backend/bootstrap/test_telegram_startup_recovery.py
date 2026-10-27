"""Real Telegram SDK lifecycle with an isolated wire; no live bot/config changes."""
import asyncio
from collections import Counter
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, Conflict, Forbidden, InvalidToken, NetworkError, RetryAfter, TimedOut
from telegram.ext import Application
from telegram.request import BaseRequest

from bootstrap.channel_host import ChannelHost
from bus.durable import DeliveryStopped
from bus.event_bus import EventBus
from bus.queue import MessageBus
from infra.channels.telegram_channel import TelegramChannel
from session.manager import SessionManager


async def wait_until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.01)


class Wire(BaseRequest):
    def __init__(self, state):
        self.state = state
        self.closed = True
        state['resources'].append(self)

    @property
    def read_timeout(self): return 1

    async def initialize(self): self.closed = False
    async def shutdown(self): self.closed = True

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit('/', 1)[-1]
        self.state['calls'][endpoint] += 1
        if endpoint == self.state.get('blocked'):
            self.state['entered'].set()
            await asyncio.Event().wait()
        if endpoint == self.state.get('fail') and self.state['remaining']:
            self.state['remaining'] -= 1
            raise NetworkError('offline test')
        result = True
        if endpoint == 'getMe':
            result = {'id':123, 'is_bot':True, 'first_name':'Test', 'username':'test_bot'}
        elif endpoint == 'getUpdates':
            await asyncio.sleep(.01)
            result = []
        elif endpoint == 'sendMessage':
            params = request_data.parameters
            result = {'message_id':71, 'date':1700000000,
                      'chat':{'id':int(params['chat_id']), 'type':'private'}, 'text':params['text']}
        return 200, json.dumps({'ok':True, 'result':result}).encode()


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    state = {'calls':Counter(), 'resources':[], 'remaining':1, 'entered':asyncio.Event()}
    builder = Application.builder
    monkeypatch.setattr(Application, 'builder', staticmethod(lambda: builder().request(Wire(state)).get_updates_request(Wire(state))))
    sessions = SessionManager(tmp_path)
    bus = MessageBus()
    bus.enable_durability(tmp_path, sessions.execution_journal)
    channel = TelegramChannel('123:offline', bus, sessions)
    registrations = []
    ctx = SimpleNamespace(bus=bus, event_bus=EventBus(), interrupt_controller=None,
                          push_tool=SimpleNamespace(register_channel=lambda *a, **k: registrations.append(a)))
    host = ChannelHost(lambda _: ctx)
    host.add(channel)
    yield SimpleNamespace(state=state, sessions=sessions, bus=bus, channel=channel,
                          host=host, registrations=registrations)
    bus.transport.close()
    sessions._store.close()


@pytest.mark.parametrize('endpoint', ['getMe', 'setMyCommands', 'deleteWebhook'])
async def test_transient_start_failure_cleans_sdk_then_recovers_once(runtime, monkeypatch, endpoint):
    r = runtime
    r.state['fail'] = endpoint
    monkeypatch.setattr(r.channel, 'startup_retry_delay', lambda error, attempt: .02)
    key = r.bus.outbox.enqueue('telegram:telegram', {'channel':'telegram', 'chat_id':'43',
        'method':'text', 'args':{'text':'pending before startup'}}, 'pending-before-start')
    try:
        await r.host.start_all()
        assert r.host.health() == {'telegram':False}
        assert len(r.host.failures) == 1
        assert all(w.closed for w in r.state['resources'])
        assert not r.registrations and not r.bus._subscribers and not r.bus.outbox.handlers
        with pytest.raises(DeliveryStopped):
            await r.bus.outbox.deliver(key)
        assert r.bus.transport.get(key)['attempts'] == 0
        await wait_until(lambda: r.host.health()['telegram'])
        assert r.host.failures == [] and len(r.registrations) == 1
        assert len(r.bus._subscribers['telegram']) == 1
        consumer = r.channel._durable_task
        await asyncio.gather(r.host.start_all(), r.host.start_all(), r.channel.start())
        assert consumer is r.channel._durable_task
        assert len(r.registrations) == len(r.bus._subscribers['telegram']) == 1
        await r.bus.outbox.deliver(key)
        await r.bus.outbox.deliver(key)
        assert r.state['calls']['sendMessage'] == 1
        assert r.bus.transport.get(key)['attempts'] == 1
    finally:
        await r.host.stop_all()
    assert not r.host._recovery_tasks and not r.channel.is_running
    assert all(w.closed for w in r.state['resources'])


@pytest.mark.parametrize('blocked', [False, True])
async def test_stop_cancels_backoff_or_partial_retry_without_restarting(runtime, monkeypatch, blocked):
    r = runtime
    r.state['fail'] = 'getMe'
    monkeypatch.setattr(r.channel, 'startup_retry_delay', lambda error, attempt: .01 if blocked else 60)
    await r.host.start_all()
    if blocked:
        r.state['blocked'] = 'getMe'
        await asyncio.wait_for(r.state['entered'].wait(), 5)
    await asyncio.wait_for(r.host.stop_all(), 2)
    before = dict(r.state['calls'])
    await r.host.start_all()
    await asyncio.sleep(.03)
    assert dict(r.state['calls']) == before and not r.host._recovery_tasks
    assert all(w.closed for w in r.state['resources'])


@pytest.mark.parametrize('error', [BadRequest('bad request'), InvalidToken('bad token'), Forbidden('forbidden'),
                                  Conflict('duplicate poller'), ValueError('configuration')])
async def test_permanent_error_is_not_retried(runtime, monkeypatch, error):
    r = runtime
    async def fail(): raise error
    monkeypatch.setattr(r.channel._app, 'initialize', fail)
    await r.host.start_all()
    await asyncio.sleep(.02)
    assert not r.host._recovery_tasks
    assert r.host.health() == {'telegram':False} and len(r.host.failures) == 1
    await r.host.stop_all()


def test_retry_policy_is_bounded_and_respects_server_delay(monkeypatch):
    monkeypatch.setenv('PTB_TIMEDELTA', '1')
    assert [TelegramChannel.startup_retry_delay(NetworkError('offline'), n) for n in range(7)] == [5,10,20,40,60,60,60]
    assert TelegramChannel.startup_retry_delay(TimedOut(), 0) == 5
    assert TelegramChannel.startup_retry_delay(TimeoutError(), 0) == 5
    assert TelegramChannel.startup_retry_delay(RetryAfter(120), 0) == 120
    assert TelegramChannel.startup_retry_delay(RetryAfter(timedelta(seconds=90)), 0) == 90


async def test_failed_rollback_disables_automatic_restart(runtime, monkeypatch):
    r = runtime
    r.state['fail'] = 'getMe'
    async def fail_cleanup(): raise NetworkError('cleanup failed')
    monkeypatch.setattr(r.channel, '_stop_runtime', fail_cleanup)
    await r.host.start_all()
    await asyncio.sleep(.02)
    assert not r.host._recovery_tasks
    assert r.host.failures[0].error_type == 'RuntimeError'
    # Restore cleanup for fixture disposal, without retrying the startup.
    monkeypatch.undo()
    await r.host.stop_all()


async def test_telegram_backoff_does_not_block_other_channels(runtime, monkeypatch):
    from unittest.mock import AsyncMock
    r = runtime
    r.state['fail'] = 'getMe'
    monkeypatch.setattr(r.channel, 'startup_retry_delay', lambda error, attempt: 60)
    other = SimpleNamespace(name='other', start=AsyncMock(), stop=AsyncMock())
    r.host.add(other)
    try:
        await asyncio.wait_for(r.host.start_all(), 2)
        other.start.assert_awaited_once()
        assert r.host.health() == {'telegram':False, 'other':True}
    finally:
        await r.host.stop_all()
    other.stop.assert_awaited_once()


async def test_shutdown_racing_initial_failure_cannot_schedule_recovery(runtime, monkeypatch):
    r = runtime
    entered, release = asyncio.Event(), asyncio.Event()
    policy_calls = []
    async def fail(ctx):
        entered.set()
        await release.wait()
        raise NetworkError('initial connection failed during shutdown')
    monkeypatch.setattr(r.channel, 'start', fail)
    monkeypatch.setattr(r.channel, 'startup_retry_delay', lambda *args: policy_calls.append(args) or 0)
    starting = asyncio.create_task(r.host.start_all())
    await entered.wait()
    stopping = asyncio.create_task(r.host.stop_all())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(asyncio.gather(starting, stopping), 2)
    assert not r.host._recovery_tasks and not policy_calls
