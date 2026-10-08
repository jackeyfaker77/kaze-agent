import asyncio
from unittest.mock import AsyncMock

import pytest

from agent.mcp.client import McpClient, McpToolError
from agent.plugins import ProactiveSourceSpec, RegisteredProactiveSource
from proactive_v2.mcp_sources import acknowledge_async
from agent.plugins.specs import proactive_source_key
from tests.backend.bootstrap import test_proactive as proactive_fixtures

proactive_setup = proactive_fixtures.proactive_setup


@pytest.mark.asyncio
async def test_passive_and_proactive_calls_share_a_serial_rpc_connection():
    client = McpClient("shared", ["unused"])
    active = 0
    maximum = 0

    async def call(name, args, *, timeout):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        return name

    client._call_locked = call
    results = await asyncio.gather(
        client.call("passive", {}), client.call("proactive", {})
    )
    assert results == ["passive", "proactive"]
    assert maximum == 1


@pytest.mark.asyncio
async def test_remote_tool_error_is_not_ack_success():
    client = McpClient("shared", ["unused"])
    client._send = AsyncMock()
    client._recv = AsyncMock(
        return_value={
            "result": {"isError": True, "content": [{"type": "text", "text": "failed"}]}
        }
    )
    with pytest.raises(McpToolError):
        await client.call("ack", {})
    gateway = AsyncMock()
    gateway.call.return_value = {"ok": False}
    source = RegisteredProactiveSource(
        "example",
        ProactiveSourceSpec(
            id="main",
            channels=("alert",),
            server="shared",
            fetch_tool="fetch",
            ack_tool="ack",
        ),
    )
    with pytest.raises(RuntimeError, match="ACK 未成功"):
        await acknowledge_async(gateway, [source], "example:main", ["one"])


@pytest.mark.asyncio
async def test_running_proactive_gateway_tracks_mcp_add_remove_reconnect(proactive_setup, monkeypatch):
    import json

    from agent.mcp.client import McpToolInfo
    from agent.mcp.registry import McpServerRegistry
    from agent.plugins.snapshot import bind_runtime_snapshot, reset_runtime_snapshot
    from proactive_v2.source_refresh import SourceRefresher

    setup = proactive_setup
    clients = []

    class OfflineClient:
        def __init__(self, name, command, env=None, cwd=None):
            self.name, self.command, self.env, self.cwd = name, command, env, cwd
            self.closed, self.calls = False, []
            clients.append(self)

        async def connect(self):
            return [McpToolInfo(name, "offline", {"type": "object", "properties": {}})
                    for name in ("fetch", "ack", "poll")]

        async def disconnect(self):
            self.closed = True

        async def call(self, name, args):
            if self.closed:
                raise ConnectionError("old MCP connection closed")
            self.calls.append(name)
            return "[]" if name == "fetch" else '{"ok": true}'

    monkeypatch.setattr("agent.mcp.registry.McpClient", OfflineClient)
    (setup.workspace / "proactive_sources.json").write_text(json.dumps({"sources": [{
        "server": "not_feed", "channel": "content", "get_tool": "fetch",
        "ack_tool": "ack", "poll_tool": "poll",
    }]}), encoding="utf-8")
    servers = McpServerRegistry(setup.workspace / "mcp_servers.json", setup.runtime.tools)
    try:
        await servers.add("not_feed", ["offline"])
        loop = await setup.start()
        gateway = loop._mcp_gateway
        refresher = SourceRefresher(setup.workspace, gateway, active_routes={("not_feed", "fetch")}, interval_seconds=150)
        assert await gateway.call("not_feed", "fetch", {}) == []
        await servers.add("new_source", ["offline"])
        assert await gateway.call("new_source", "fetch", {}) == []
        await servers.remove("not_feed")
        with pytest.raises(RuntimeError, match="MCP tool 不可用"):
            await gateway.call("not_feed", "fetch", {})
        await servers.add("not_feed", ["offline"])
        current = clients[-1]
        assert await gateway.call("not_feed", "fetch", {}) == []
        source_id = proactive_source_key(loop._plugin_proactive_sources[0])
        await acknowledge_async(gateway, loop._plugin_proactive_sources, source_id, ["one"])
        await refresher.refresh_once()
        lease = await loop._runtime_snapshot_store.acquire()
        token = bind_runtime_snapshot(lease)
        try:
            # Drift 同样通过运行时 scope 访问共享 MCP 工具。
            scope = loop._build_runtime_scope()
            await scope.shared_tools.execute("mcp_not_feed__fetch", {}, raise_errors=True)
        finally:
            reset_runtime_snapshot(token)
            await lease.release()
        assert current.calls == ["fetch", "ack", "poll", "fetch"]
    finally:
        await servers.shutdown()
