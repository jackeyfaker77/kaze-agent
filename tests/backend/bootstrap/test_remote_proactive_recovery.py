import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from tests.backend.bootstrap.test_remote_host import remote, authenticate, rpc  # noqa: F401
from agent.provider import LLMResponse, ToolCall
from agent.tools.base import Tool
from bootstrap.proactive import build_proactive_loop
from proactive_v2.config_loader import load_proactive_config


class Source(Tool):
    name = 'recovery_fixture'
    description = 'Recovery fixture'
    parameters = {'type': 'object', 'properties': {}}

    def __init__(self, name, owner):
        self.name, self.owner = name, owner

    async def execute(self, **kwargs):
        if self.name.endswith('fetch'):
            return json.dumps([{'kind': 'alert', 'event_id': 'reminder:v1', 'title': 'fixture',
                                'expires_at': self.owner.expiry.isoformat()}])
        if self.name.endswith('validate'):
            if getattr(self.owner, 'activity_during_validation', False):
                self.owner.core.presence.record_user_message('telegram:42')
            return json.dumps({'valid_event_ids': ['reminder:v1'] if self.owner.valid else []})
        self.owner.acks += 1
        return '{"ok":true}'


@pytest_asyncio.fixture
async def interrupted(remote, monkeypatch):
    core = remote.server.service.runtime
    probe = SimpleNamespace(models=0, sends=0, acks=0, valid=True,
                            expiry=datetime.now(timezone.utc) + timedelta(hours=2))
    async def model(**kwargs):
        probe.models += 1
        return LLMResponse(None, tool_calls=[ToolCall('send', 'send_event', {'message': 'one reminder'})])
    async def send(*args):
        probe.sends += 1
        return SimpleNamespace(message_id='one')
    core.provider = SimpleNamespace(chat=model)
    core.plugin_manager = None
    core.config.proactive = load_proactive_config({'enabled': True, 'lifecycle': 'wake',
        'session_key': 'telegram:42', 'default_channel': 'telegram', 'default_chat_id': '42',
        'anyaction_enabled': False, 'message_dedupe_enabled': False})
    (remote.workspace / 'proactive_sources.json').write_text(json.dumps({'sources': [
        {'server': 'fixture', 'channel': 'alert', 'get_tool': 'fetch', 'ack_tool': 'ack', 'validate_tool': 'validate'}]}))
    for name in ('fetch', 'validate', 'ack'):
        core.tools.register(Source('mcp_fixture__' + name, probe), source_type='mcp', source_name='fixture')
    core.push_tool.register_channel('telegram', text=send)
    core.presence.record_user_message('telegram:42', datetime.now(timezone.utc) - timedelta(days=1))
    loop = build_proactive_loop(core)
    assert loop is not None
    await loop._start_current_snapshot()
    core.proactive_loop = loop
    journal = core.session_manager.execution_journal
    append = journal.append
    def crash(turn, kind, payload):
        append(turn, kind, payload)
        if kind == 'proactive.model.returned':
            raise RuntimeError('injected interruption')
    monkeypatch.setattr(journal, 'append', crash)
    with pytest.raises(RuntimeError):
        await loop._tick_bound('source')
    monkeypatch.setattr(journal, 'append', append)
    probe.turn = journal.find('telegram:42', 'proactive', 'source')['turn_id']
    if journal.get(probe.turn)['status'] in {'accepted', 'running'}:
        journal.finish(probe.turn, 'interrupted')
    probe.loop = loop
    probe.core = core
    try:
        yield probe
    finally:
        core.proactive_loop = None
        await loop._stop_active_kernel()
        loop.close()


@pytest.mark.asyncio
async def test_two_remote_clients_recover_same_reminder_once(remote, interrupted):
    first, second = await authenticate(remote.url), await authenticate(remote.url)
    probe = interrupted
    journal = probe.core.session_manager.execution_journal
    try:
        before = list(journal._conn.iterdump())
        plans = await asyncio.gather(rpc(first, 'execution.plan', {'turn_id': probe.turn}),
                                    rpc(second, 'execution.plan', {'turn_id': probe.turn}))
        assert before == list(journal._conn.iterdump())
        assert plans[0]['payload'] == plans[1]['payload']
        plan = plans[0]['payload']
        assert plan['allowed'], plan
        payload = {'turn_id': probe.turn, 'confirmed': True, 'plan_digest': plan['plan_digest']}
        results = await asyncio.gather(rpc(first, 'execution.continue', {**payload, 'action_id': 'one'}),
                                       rpc(second, 'execution.continue', {**payload, 'action_id': 'two'}))
        assert all(not r.get('error') for r in results), results
        assert {r['payload']['created'] for r in results} == {True, False}
        assert len({r['payload']['execution']['turn_id'] for r in results}) == 1
        assert probe.models == probe.sends == probe.acks == 1
        repeated = await rpc(first, 'execution.continue', {**payload, 'action_id': 'again'})
        assert not repeated.get('error') and not repeated['payload']['created']
        assert probe.models == probe.sends == probe.acks == 1
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['cancel', 'expire', 'user', 'user_during_validation', 'disable', 'close'])
async def test_recovery_revalidates_and_never_rejudges(remote, interrupted, change):
    socket = await authenticate(remote.url)
    probe = interrupted
    journal = probe.core.session_manager.execution_journal
    try:
        plan = (await rpc(socket, 'execution.plan', {'turn_id': probe.turn}))['payload']
        assert plan['allowed'], plan
        if change == 'cancel':
            probe.valid = False
        elif change == 'expire':
            probe.loop._recovery_runtime._clock = SimpleNamespace(now=lambda: probe.expiry + timedelta(seconds=1))
        elif change == 'user':
            probe.core.presence.record_user_message('telegram:42')
        elif change == 'user_during_validation':
            probe.activity_during_validation = True
        elif change == 'disable':
            probe.core.config.proactive.enabled = False
        else:
            state = journal.get(probe.turn)
            journal.review(probe.turn, expected_seq=state['last_seq'], decision='close', action_id='stop', note='stop reminder')
        result = await rpc(socket, 'execution.continue', {'turn_id': probe.turn, 'confirmed': True,
            'plan_digest': plan['plan_digest'], 'action_id': 'confirm'})
        if change in {'cancel', 'expire', 'user_during_validation'}:
            assert not result.get('error'), result
            assert result['payload']['execution']['result']['cancelled']
        else:
            assert result.get('error'), result
        assert probe.models == 1 and probe.sends == 0 and probe.acks == 0
    finally:
        await socket.close()
