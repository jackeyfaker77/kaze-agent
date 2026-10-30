import asyncio
import json

import pytest

from agent.mcp.client import McpClient, McpToolInfo
from agent.mcp.registry import McpServerRegistry
from agent.tools.registry import ToolRegistry


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.005)


@pytest.fixture
def setup_registry(tmp_path, monkeypatch):
    path = tmp_path/'mcp_servers.json'
    configs = {'calendar': {'command': ['offline'], 'env': {'KEEP': 'yes'}},
               'feed': {'command': ['offline'], 'cwd': str(tmp_path)}}
    path.write_text(json.dumps({'servers': configs}), encoding='utf-8')
    instances = []
    attempts = {}
    failures = {'calendar': 2, 'feed': 1}
    class Client:
        def __init__(self, name, command, env=None, cwd=None):
            self.name, self.command, self.env, self.cwd = name, command, env, cwd
            self.connected = False
            self.calls = []
            instances.append(self)
        async def connect(self):
            attempts[self.name] = attempts.get(self.name, 0) + 1
            await asyncio.sleep(.001)
            if attempts[self.name] <= failures.get(self.name, 0):
                raise TimeoutError('cold startup')
            self.connected = True
            return [McpToolInfo('fetch', 'fixture', {'type': 'object'})]
        async def disconnect(self):
            self.connected = False
        async def call(self, name, arguments):
            self.calls.append((name, arguments))
            return '[]'
    monkeypatch.setattr('agent.mcp.registry.McpClient', Client)
    tools = ToolRegistry()
    registry = McpServerRegistry(path, tools)
    registry._retry_base = registry._health_interval = .01
    registry._retry_cap = .02
    return registry, path, instances, attempts, failures, tools


async def test_failed_start_recovers_without_config_write_or_tool_calls(setup_registry):
    registry, path, instances, attempts, _, tools = setup_registry
    original = path.read_bytes()
    try:
        await registry.load_and_connect_all()
        assert not all(registry.health().values())
        assert 'calendar' in registry.list_servers()
        await until(lambda: all(registry.health().values()))
        assert attempts == {'calendar': 3, 'feed': 2}
        assert path.read_bytes() == original
        assert not any(c.calls for c in instances)
        assert len([c for c in instances if c.connected]) == 2
        old = registry._clients['calendar']
        old.connected = False
        await until(lambda: registry._clients.get('calendar') is not old and registry.health()['calendar'])
        assert len([c for c in instances if c.name == 'calendar' and c.connected]) == 1
        assert not any(c.calls for c in instances)
        assert path.read_bytes() == original
    finally:
        await registry.shutdown()
    assert not any(c.connected for c in instances)
    assert 'mcp_calendar__fetch' not in tools._tools


async def test_add_preserves_offline_configs_and_remove_cancels_recovery(setup_registry):
    registry, path, instances, attempts, failures, _ = setup_registry
    failures['calendar'] = failures['feed'] = 1000
    try:
        await registry.load_and_connect_all()
        await asyncio.gather(registry.add('new', ['offline']), registry.add('new', ['offline']))
        assert attempts['new'] == 1
        saved = json.loads(path.read_text())['servers']
        assert saved['calendar']['env'] == {'KEEP': 'yes'}
        assert 'feed' in saved
        await registry.remove('calendar')
        count = attempts['calendar']
        await asyncio.sleep(.06)
        assert attempts['calendar'] == count
        assert 'calendar' not in json.loads(path.read_text())['servers']
    finally:
        await registry.shutdown()
    counts = dict(attempts)
    await asyncio.sleep(.03)
    assert counts == attempts and not any(c.connected for c in instances)


@pytest.mark.parametrize('failure', [ConnectionResetError, TimeoutError, asyncio.CancelledError])
async def test_transport_failure_is_not_replayed(failure):
    client = McpClient('calendar', ['unused'])
    calls = []
    async def fail(name, arguments, *, timeout):
        calls.append(name)
        raise failure('response unknown')
    client._call_locked = fail
    with pytest.raises(failure):
        await client.call('fetch', {})
    with pytest.raises(ConnectionError):
        await client.call('fetch', {})
    assert calls == ['fetch']


async def test_declared_server_cannot_replace_offline_working_directory(setup_registry, tmp_path):
    from types import SimpleNamespace
    registry, path, _, _, failures, _ = setup_registry
    failures['feed'] = 1000
    original = path.read_bytes()
    try:
        await registry.load_and_connect_all()
        spec = SimpleNamespace(name='feed', command=['offline'], env=None, cwd=str(tmp_path/'different'))
        with pytest.raises(ValueError, match='工作目录冲突'):
            await registry.connect_declared_servers([spec])
        assert path.read_bytes() == original
    finally:
        await registry.shutdown()


async def test_connect_cancellation_cleans_up_client(monkeypatch):
    client = McpClient('calendar', ['unused'])
    entered = asyncio.Event()
    cleaned = []
    async def connect():
        entered.set()
        await asyncio.Event().wait()
    async def disconnect():
        cleaned.append(True)
    monkeypatch.setattr(client, '_connect_impl', connect)
    monkeypatch.setattr(client, 'disconnect', disconnect)
    task = asyncio.create_task(client.connect())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned == [True]


async def test_shutdown_waits_for_cancelled_recovery_drop_to_reap_process(tmp_path):
    from unittest.mock import AsyncMock, Mock
    registry = McpServerRegistry(tmp_path/'mcp.json', ToolRegistry())
    client = McpClient('calendar', ['unused'])
    entered, released = asyncio.Event(), asyncio.Event()
    async def wait():
        entered.set()
        await released.wait()
        return 0
    process = Mock(wait=AsyncMock(side_effect=wait), returncode=None)
    client._process = process
    registry._clients['calendar'] = client
    task = asyncio.create_task(registry._drop('calendar'))
    registry._recovery['calendar'] = task
    await entered.wait()
    closing = asyncio.create_task(registry.shutdown())
    await asyncio.sleep(.02)
    assert not closing.done()
    assert client._process is process
    released.set()
    await closing
    assert task.cancelled()
    assert client._process is None
    process.terminate.assert_called_once()
    process.wait.assert_awaited_once()


async def test_disabled_server_is_not_restarted_on_reload(setup_registry):
    registry, path, instances, attempts, failures, tools = setup_registry
    failures.clear()
    try:
        await registry.load_and_connect_all()
        config = json.loads(path.read_text())
        config['servers']['calendar']['enabled'] = False
        path.write_text(json.dumps(config))
        await registry.load_and_connect_all()
        count = attempts['calendar']
        await asyncio.sleep(.05)
        assert attempts['calendar'] == count
        assert not any(c.connected for c in instances if c.name == 'calendar')
        assert 'mcp_calendar__fetch' not in tools._tools
    finally:
        await registry.shutdown()


async def test_real_stdio_timeout_reconnect_and_unknown_call_not_replayed(tmp_path, monkeypatch):
    import sys
    server = tmp_path/'server.py'
    server.write_text('''import sys,json,time,os
from pathlib import Path
root=Path(__file__).parent
counter=root/'starts'
with counter.open('a') as f:f.write('start\\n')
if len(counter.read_text().splitlines())==1:time.sleep(30)
for line in sys.stdin:
 r=json.loads(line);method=r['method']
 if 'id' not in r:continue
 if method=='tools/call':
  with (root/'calls').open('a') as f:f.write('call\\n')
  os._exit(3)
 result={'tools':[{'name':'fetch','inputSchema':{'type':'object'}}]} if method=='tools/list' else {}
 print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)
''', encoding='utf-8')
    monkeypatch.setattr('agent.mcp.client._CONNECT_TIMEOUT', 1.0)
    path = tmp_path/'mcp.json'
    path.write_text(json.dumps({'servers': {'calendar': {'command': [sys.executable, str(server)]}}}))
    original = path.read_bytes()
    registry = McpServerRegistry(path, ToolRegistry())
    registry._retry_base = registry._health_interval = .01
    try:
        await registry.load_and_connect_all()
        await until(lambda: registry.health().get('calendar'))
        old = registry._clients['calendar']
        with pytest.raises(ConnectionError):
            await old.call('fetch', {})
        await until(lambda: registry._clients.get('calendar') is not old and registry.health()['calendar'])
        assert (tmp_path/'calls').read_text().splitlines() == ['call']
        assert len((tmp_path/'starts').read_text().splitlines()) == 3
        assert path.read_bytes() == original
    finally:
        await registry.shutdown()
