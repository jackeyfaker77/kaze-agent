import asyncio
from unittest.mock import AsyncMock

import pytest

from agent.mcp.client import McpClient, McpToolError
from agent.plugins import ProactiveSourceSpec, RegisteredProactiveSource
from proactive_v2.mcp_sources import acknowledge_async


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
