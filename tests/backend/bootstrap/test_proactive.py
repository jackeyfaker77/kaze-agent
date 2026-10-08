"""验证完整主动 Kernel 与 Kaze 宿主之间的真实接线。"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from agent.provider import LLMResponse, ToolCall
from agent.tools.base import Tool
from agent.tools.message_push import DeliveryReceipt, MessagePushTool
from agent.tools.registry import ToolRegistry
from bootstrap.proactive import (
    build_proactive_loop,
    prepare_proactive_loop,
    run_proactive,
)
from bus.event_bus import EventBus
from bus.events_lifecycle import ProactiveMessageCommitted
from plugins.wake_proactive.state import WakeStateStore
from proactive_v2.config_loader import load_proactive_config
from proactive_v2.presence import PresenceStore
from session.manager import SessionManager

NOW = datetime(2026, 10, 8, 4, tzinfo=timezone.utc)
KEY = "telegram:42"


def structured(name="send_event", **arguments):
    return LLMResponse(
        content=None,
        tool_calls=[
            ToolCall(
                "decision", name, arguments or {"message": "提醒：该处理这件事了。"}
            )
        ],
    )


class SourceTool(Tool):
    name = "test_source"
    description = "测试缓存源"
    parameters = {
        "type": "object",
        "properties": {
            "consumer": {"type": "string"},
            "event_ids": {"type": "array"},
            "reason": {"type": "string"},
        },
    }

    def __init__(self, name, callback):
        self.name, self.callback = name, callback

    async def execute(self, **kwargs):
        return json.dumps(await self.callback(**kwargs), ensure_ascii=False)


@pytest_asyncio.fixture
async def proactive_setup(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=lambda: NOW)
    monkeypatch.setattr("plugins.wake_proactive.runtime.clock_from_env", lambda: clock)
    sessions = SessionManager(tmp_path)
    presence = PresenceStore(sessions._store)
    presence.record_user_message(KEY, NOW - timedelta(days=1))
    tools, bus = ToolRegistry(), EventBus()
    fetch = AsyncMock(
        return_value=[{"event_id": "one", "kind": "alert", "title": "该处理这件事了"}]
    )
    ack = AsyncMock(return_value={"ok": True})
    for name, callback in (("fetch", fetch), ("ack", ack)):
        tools.register(
            SourceTool(f"mcp_not_feed__{name}", callback),
            source_type="mcp",
            source_name="not_feed",
        )
    (tmp_path / "proactive_sources.json").write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "server": "not_feed",
                        "channel": "alert",
                        "get_tool": "fetch",
                        "ack_tool": "ack",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    sender = AsyncMock(
        return_value=DeliveryReceipt(True, "telegram", "42", "sent", "99")
    )
    push = MessagePushTool(bus)
    push.register_channel("telegram", text=sender)
    committed = []
    bus.on(ProactiveMessageCommitted, lambda event: committed.append(event))
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            proactive=load_proactive_config(
                {
                    "enabled": True,
                    "lifecycle": "wake",
                    "session_key": KEY,
                    "default_channel": "telegram",
                    "default_chat_id": "42",
                    "anyaction_enabled": False,
                    "message_dedupe_enabled": False,
                }
            ),
            model="fake",
            max_tokens=2048,
        ),
        session_manager=sessions,
        presence=presence,
        tools=tools,
        event_bus=bus,
        provider=SimpleNamespace(chat=AsyncMock(return_value=structured())),
        push_tool=push,
        memory_runtime=None,
        plugin_manager=None,
        loop=SimpleNamespace(_active_tasks={}, process_direct=AsyncMock()),
    )
    loops = []

    async def start(rng=None):
        loop = build_proactive_loop(runtime)
        assert loop is not None
        loop._rng = rng
        loops.append(loop)
        await loop._start_current_snapshot()
        return loop

    async def stop(loop):
        await loop._stop_active_kernel()
        loop.close()

    yield SimpleNamespace(
        runtime=runtime,
        start=start,
        stop=stop,
        sender=sender,
        fetch=fetch,
        ack=ack,
        sessions=sessions,
        committed=committed,
        workspace=tmp_path,
        clock=clock,
    )
    for loop in reversed(loops):
        await stop(loop)
    sessions._store.close()


@pytest.mark.asyncio
async def test_wake_delivers_without_heartbeat_and_persists_only_after_receipt(
    proactive_setup,
):
    setup = proactive_setup

    async def send(*args):
        assert not setup.sessions.get_or_create(KEY).messages
        return DeliveryReceipt(True, "telegram", "42", "sent", "99")

    setup.sender.side_effect = send
    loop = await setup.start()
    await loop._tick()
    setup.runtime.loop.process_direct.assert_not_awaited()
    setup.sender.assert_awaited_once()
    setup.ack.assert_awaited_once()
    message = setup.sessions.get_or_create(KEY).messages[-1]
    assert message["proactive"] and message["delivery_id"]
    assert message["metadata"]["delivery_ref"] == "telegram:99"
    assert message["evidence_item_ids"][0].endswith(":one")
    assert len(setup.committed) == 1
    assert not (setup.workspace / "HEARTBEAT.md").exists()


@pytest.mark.asyncio
async def test_failed_delivery_keeps_alert_unread_and_no_visible_history(
    proactive_setup,
):
    setup = proactive_setup
    setup.sender.return_value = DeliveryReceipt(
        False, "telegram", "42", "failed", error="offline"
    )
    loop = await setup.start()
    await loop._tick()
    assert not setup.sessions.get_or_create(KEY).messages
    assert not setup.committed
    setup.ack.assert_not_awaited()
    state = WakeStateStore(setup.workspace / "wake_proactive.db")
    try:
        assert len(state.unread("alert")) == 1
    finally:
        state.close()
    setup.sender.return_value = DeliveryReceipt(True, "telegram", "42", "sent", "100")
    await loop._tick()
    assert len(setup.sessions.get_or_create(KEY).messages) == 1


@pytest.mark.asyncio
async def test_ack_failure_survives_restart_without_resending(proactive_setup):
    setup = proactive_setup
    setup.ack.side_effect = RuntimeError("ACK offline")
    first = await setup.start()
    await first._tick()
    assert setup.sender.await_count == 1
    await setup.stop(first)
    setup.ack.side_effect = None
    second = await setup.start()
    await second._tick()
    assert setup.ack.await_count >= 2
    assert setup.sender.await_count == 1
    assert setup.runtime.provider.chat.await_count == 1
    state = WakeStateStore(setup.workspace / "wake_proactive.db")
    try:
        assert not state.pending_acknowledgements()
    finally:
        state.close()


@pytest.mark.asyncio
async def test_user_reply_during_model_work_suppresses_send_and_consumption(
    proactive_setup,
):
    setup = proactive_setup

    async def decide(**kwargs):
        setup.runtime.presence.record_user_message(KEY, NOW)
        return structured()

    setup.runtime.provider.chat.side_effect = decide
    loop = await setup.start()
    await loop._tick()
    setup.sender.assert_not_awaited()
    setup.ack.assert_not_awaited()
    assert not setup.sessions.get_or_create(KEY).messages


@pytest.mark.asyncio
async def test_busy_session_does_not_fetch_or_generate(proactive_setup):
    setup = proactive_setup
    setup.runtime.loop._active_tasks[KEY] = object()
    loop = await setup.start()
    await loop._tick()
    setup.fetch.assert_not_awaited()
    setup.runtime.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_alert_delivery_does_not_depend_on_embedding_service(proactive_setup):
    setup = proactive_setup
    embed = AsyncMock(side_effect=RuntimeError("embedding unavailable"))
    setup.runtime.memory_runtime = SimpleNamespace(
        embedding_api=SimpleNamespace(model_id="fake", embed_batch=embed)
    )
    loop = await setup.start()
    await loop._tick()
    setup.sender.assert_awaited_once()
    embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_desktop_notification_contains_committed_history(proactive_setup):
    from agent.config_models import Config
    from desktop_bridge.session_service import DesktopBridgeService

    setup = proactive_setup
    cfg = setup.runtime.config.proactive
    cfg.default_channel, cfg.default_chat_id, cfg.session_key = (
        "desktop",
        "desktop:daily",
        "desktop:daily",
    )
    setup.runtime.config = Config(
        provider="test", model="fake", api_key="unused", proactive=cfg
    )
    service = DesktopBridgeService(setup.runtime)
    events = []

    def notify(event):
        assert setup.sessions._store.count_messages("desktop:daily") == 1
        events.append(event)

    service.add_event_listener(notify)
    try:
        loop = await setup.start()
        await loop._tick()
        assert len(events) == 1 and events[0]["method"] == "message.pushed"
        messages = events[0]["payload"]["session"]["messages"]
        assert len(messages) == 1 and messages[0]["proactive"]
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_disabled_runner_does_not_create_proactive_databases(proactive_setup):
    setup = proactive_setup
    setup.runtime.config.proactive.enabled = False
    await run_proactive(setup.runtime)
    assert not (setup.workspace / "proactive.db").exists()
    setup.fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_runner_uses_shared_tools_and_closes_kernel_and_refresh_task(
    proactive_setup, monkeypatch, cancel
):
    from proactive_v2.source_refresh import SourceRefresher

    setup = proactive_setup
    refreshers = []

    class TrackedRefresher(SourceRefresher):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            refreshers.append(self)

    monkeypatch.setattr("bootstrap.proactive.SourceRefresher", TrackedRefresher)
    delivered = asyncio.Event()
    setup.runtime.event_bus.on(ProactiveMessageCommitted, lambda event: delivered.set())
    loop = await prepare_proactive_loop(setup.runtime)
    assert loop is not None
    task = asyncio.create_task(run_proactive(setup.runtime))
    try:
        await asyncio.wait_for(delivered.wait(), timeout=3)
        assert (
            refreshers[0]._gateway._tools
            is loop._runtime_snapshot_store.current.tool_registry
        )
        assert refreshers[0]._gateway._tools is setup.runtime.tools
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            loop.stop()
            await asyncio.wait_for(task, timeout=3)
        assert setup.runtime.proactive_loop is None
        assert loop._state_closed and not loop._kernel_started
        assert not loop._runtime_snapshot_store._counts
        assert refreshers[0]._task is None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
