import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.mcp.client import McpToolInfo
from proactive_v2.content_tool import ProactiveContentTool, content_scope
from proactive_v2.legacy_mcp_sources import McpClientPool
from proactive_v2.sources import ProactiveSources


def declare(workspace, sources):
    (workspace / "proactive_sources.json").write_text(
        json.dumps({"sources": sources}), encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_three_channels_shared_read_body_prefetch_and_routed_ack(tmp_path):
    declare(
        tmp_path,
        [
            {
                "server": "news",
                "channel": "alert",
                "get_tool": "updates",
                "ack_tool": "consume",
            },
            {
                "server": "news",
                "channel": "content",
                "get_tool": "updates",
                "ack_tool": "consume",
                "poll_tool": "refresh",
            },
            {"server": "sensor", "channel": "context", "get_tool": "state"},
        ],
    )
    calls = []

    async def call(server, tool, args, **kwargs):
        calls.append((server, tool, args, kwargs))
        if tool == "consume":
            return {"ok": True}
        if server == "sensor":
            return {"available": True, "battery": 21}
        return [
            {
                "kind": "alert",
                "event_id": "same",
                "content": "告警完整内容",
                "ack_server": "spoofed",
            },
            {
                "kind": "content",
                "event_id": "first",
                "title": "Article 1",
                "url": "https://example.org/1",
            },
            {
                "kind": "content",
                "event_id": "second",
                "title": "Article 2",
                "url": "https://example.org/2",
            },
        ]

    started = []
    ready = asyncio.Event()

    async def fetch(**kwargs):
        started.append(kwargs["url"])
        if len(started) == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 1)
        return json.dumps({"text": "full body " + kwargs["url"]})

    pool = SimpleNamespace(
        connect_all=AsyncMock(), disconnect_all=AsyncMock(), call=call
    )
    sources = ProactiveSources(
        tmp_path, pool=pool, web_fetch_tool=SimpleNamespace(execute=fetch)
    )
    batch = await sources.fetch("session")
    assert batch is not None and batch.source_ok
    assert [item["item_id"] for item in batch.candidates] == [
        "news:same",
        "news:first",
        "news:second",
    ]
    assert batch.data.alerts[0]["ack_server"] == "news"
    assert batch.data.context == [
        {"available": True, "battery": 21, "_source": "sensor"}
    ]
    assert len([entry for entry in calls if entry[1] == "updates"]) == 1
    assert [entry[1] for entry in calls if entry[0] == "news"] == ["refresh", "updates"]
    assert batch.data.content_store["news:first"] == "full body https://example.org/1"
    assert "mcp_news__consume" in batch.disabled_tools
    await sources.acknowledge(
        "session", batch.candidates[:2], reason="delivered", delivery_ref="telegram:1"
    )
    assert calls[-1][0:3] == ("news", "consume", {"event_ids": ["same", "first"]})
    assert calls[-1][3]["optional_args"]["consumer"] == "session"


@pytest.mark.asyncio
async def test_source_failure_isolated_and_inline_body_survives_fetch_failure(tmp_path):
    declare(
        tmp_path,
        [
            {"server": "broken", "channel": "alert"},
            {"server": "working", "channel": "content", "get_args": {"tag": "kaze"}},
        ],
    )

    async def call(server, tool, args, **kwargs):
        if server == "broken":
            raise ConnectionError("offline")
        assert args == {"tag": "kaze"}
        return {
            "items": [
                {"event_id": "1", "url": "https://example.org", "content": "正文" * 300}
            ]
        }

    pool = SimpleNamespace(
        connect_all=AsyncMock(), disconnect_all=AsyncMock(), call=call
    )
    sources = ProactiveSources(
        tmp_path,
        pool=pool,
        web_fetch_tool=SimpleNamespace(
            execute=AsyncMock(return_value='{"error":"offline"}')
        ),
    )
    batch = await sources.fetch("session")
    assert batch is not None and batch.source_ok
    assert [item["item_id"] for item in batch.candidates] == ["working:1"]
    assert batch.data.content_store["working:1"] == "正文" * 300


@pytest.mark.asyncio
async def test_missing_declaration_falls_back_but_disabled_declaration_stays_empty(
    tmp_path,
):
    pool = SimpleNamespace(
        connect_all=AsyncMock(), disconnect_all=AsyncMock(), call=AsyncMock()
    )
    sources = ProactiveSources(tmp_path, pool=pool)
    assert await sources.fetch("session") is None
    declare(tmp_path, [{"enabled": False}])
    batch = await sources.fetch("session")
    assert batch is not None and batch.source_ok and not batch.candidates
    pool.call.assert_not_awaited()
    (tmp_path / "proactive_sources.json").write_text("{bad json", encoding="utf-8")
    with pytest.raises(ValueError):
        await sources.fetch("session")


@pytest.mark.asyncio
async def test_cancelled_snapshot_drains_all_fetch_tasks(tmp_path):
    declare(
        tmp_path,
        [{"server": server, "channel": "alert"} for server in ["first", "second"]],
    )
    ready = asyncio.Event()
    started = set()
    cancelled = set()

    async def call(server, *args, **kwargs):
        started.add(server)
        if len(started) == 2:
            ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.add(server)

    pool = SimpleNamespace(
        connect_all=AsyncMock(), disconnect_all=AsyncMock(), call=call
    )
    task = asyncio.create_task(ProactiveSources(tmp_path, pool=pool).fetch("session"))
    await asyncio.wait_for(ready.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == started == {"first", "second"}


@pytest.mark.asyncio
async def test_pool_reuses_connections_respects_cwd_and_filters_optional_ledger_args(
    tmp_path, monkeypatch
):
    declare(tmp_path, [{"server": "news", "channel": "content"}])
    config = {
        "servers": {"news": {"command": ["python", "server.py"], "cwd": str(tmp_path)}}
    }
    (tmp_path / "mcp_servers.json").write_text(json.dumps(config), encoding="utf-8")
    clients = []

    def factory(**kwargs):
        client = SimpleNamespace(
            connect=AsyncMock(),
            disconnect=AsyncMock(),
            call=AsyncMock(return_value="[]"),
            tool_infos=[McpToolInfo("ack", "", {"properties": {"event_ids": {}}})],
            config=kwargs,
        )
        clients.append(client)
        return client

    monkeypatch.setattr("agent.mcp.client.McpClient", factory)
    pool = McpClientPool(tmp_path)
    await pool.connect_all()
    await pool.connect_all()
    assert len(clients) == 1 and clients[0].config["cwd"] == str(tmp_path)
    await pool.call(
        "news",
        "ack",
        {"event_ids": ["1"]},
        optional_args={"consumer": "s", "reason": "processed"},
    )
    clients[0].call.assert_awaited_once_with("ack", {"event_ids": ["1"]}, timeout=None)
    declare(tmp_path, [])
    await pool.connect_all()
    clients[0].disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_only_read_operations_replay_after_transport_failure(
    tmp_path, monkeypatch
):
    declare(tmp_path, [{"server": "news", "channel": "content"}])
    (tmp_path / "mcp_servers.json").write_text(
        json.dumps({"servers": {"news": {"command": ["python"]}}}), encoding="utf-8"
    )
    clients = []

    def factory(**kwargs):
        client = SimpleNamespace(
            connect=AsyncMock(),
            disconnect=AsyncMock(),
            tool_infos=[],
            call=AsyncMock(return_value="[]"),
        )
        clients.append(client)
        return client

    monkeypatch.setattr("agent.mcp.client.McpClient", factory)
    pool = McpClientPool(tmp_path)
    await pool.connect_all()
    clients[0].call.side_effect = ConnectionError("lost")
    assert await pool.call("news", "read", {}, retry_on_transport=True) == []
    assert len(clients) == 2
    clients[1].call.side_effect = ConnectionError("lost ACK")
    with pytest.raises(ConnectionError):
        await pool.call("news", "ack", {})
    assert len(clients) == 2
    await pool.disconnect_all()


@pytest.mark.asyncio
async def test_body_cache_is_task_local_and_cleared_on_exit():
    tool = ProactiveContentTool()
    ready = asyncio.Event()

    async def other_task():
        with content_scope({"same": "other body"}):
            ready.set()
            await asyncio.sleep(0)
            assert (
                json.loads(await tool.execute(item_id="same"))["text"] == "other body"
            )

    other = asyncio.create_task(other_task())
    with content_scope({"same": "this body"}):
        await ready.wait()
        assert json.loads(await tool.execute(item_id="same"))["text"] == "this body"
        await other
    assert "error" in json.loads(await tool.execute(item_id="same"))
