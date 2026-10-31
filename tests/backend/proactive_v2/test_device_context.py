from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from plugins.default_proactive.gateway import DataGateway
from plugins.default_proactive.gateway import GatewayResult
from plugins.default_proactive.context import AgentTickContext
from plugins.default_proactive.source import McpGatewaySource
from plugins.wake_proactive.runtime import WakeRuntime
from plugins.wake_proactive.runtime import WakeRunState
from plugins.wake_proactive.context import WakeContext
from plugins.wake_proactive.state import WakeStateStore
from plugins.proactive_flow.prompt import ProactivePromptBuilder
from proactive_v2.config import ProactiveConfig
from proactive_v2.context_freshness import current_context_snapshot
from proactive_v2.contracts import normalize_context
from proactive_v2.source_catalog import workspace_sources


NOW = datetime(2026, 10, 16, 8, tzinfo=timezone.utc)


def snapshot(**changes):
    return {
        "_source": "workspace:device",
        "context_type": "windows_device",
        "available": True,
        "presence": "active",
        "confidence": 0.9,
        "locked": False,
        "idle_seconds": 5,
        "foreground": {"process_name": "Example.exe"},
        "observed_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(seconds=30)).isoformat(),
        **changes,
    }


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"available": False}, "unavailable"),
        ({"expires_at": NOW.isoformat()}, "invalid_lifetime"),
        (
            {
                "observed_at": (NOW - timedelta(seconds=60)).isoformat(),
                "expires_at": NOW.isoformat(),
            },
            "expired",
        ),
        ({"expires_at": "broken"}, "invalid_timestamp"),
        ({"observed_at": "2026-10-16T08:00:00"}, "invalid_timestamp"),
    ],
)
def test_unusable_context_loses_observations_in_default_and_wake(
    tmp_path, changes, reason
):
    item = snapshot(**changes)
    payload = normalize_context(item, now=NOW).to_prompt_item()
    assert payload["available"] is False
    assert payload["presence"] == "unknown"
    assert payload["unavailable_reason"] == reason
    assert "foreground" not in payload and "locked" not in payload
    store = WakeStateStore(tmp_path / "wake.db")
    try:
        result = store.ingest_context([item], NOW)[0]
        assert result.context.presence == "unknown"
        assert result.signal == "refresh"
        assert result.should_contact is False
        assert WakeRuntime._active_contexts(SimpleNamespace(_state=store), NOW) == []
    finally:
        store.close()


def test_expiry_boundary_and_unavailable_snapshot_replace_old_context(tmp_path):
    store = WakeStateStore(tmp_path / "wake.db")
    runtime = SimpleNamespace(_state=store)
    try:
        store.ingest_context([snapshot()], NOW)
        assert len(WakeRuntime._active_contexts(runtime, NOW)) == 1
        assert WakeRuntime._active_contexts(runtime, NOW + timedelta(seconds=30)) == []
        store.ingest_context([snapshot(available=False)], NOW + timedelta(seconds=1))
        assert WakeRuntime._active_contexts(runtime, NOW + timedelta(seconds=1)) == []
    finally:
        store.close()


def test_legacy_context_without_lifetime_is_unchanged():
    item = {
        "_source": "legacy",
        "presence": "idle",
        "confidence": 0.8,
        "custom": "value",
    }
    assert current_context_snapshot(item, now=NOW) == item


def test_legacy_replay_clock_keeps_naive_timestamps_in_the_same_domain():
    now = NOW.replace(tzinfo=None)
    item = snapshot(
        observed_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=30)).isoformat(),
    )
    assert current_context_snapshot(item, now=now) == item
    assert (
        current_context_snapshot(item, now=now + timedelta(seconds=30))["available"]
        is False
    )


def test_default_checks_actual_clock_after_slow_fetch():
    clock = SimpleNamespace(now=lambda: NOW + timedelta(seconds=31))
    builder = ProactivePromptBuilder(
        cfg=ProactiveConfig(),
        memory=None,
        veda_fn=lambda: "",
        workspace_context_fn=None,
        clock=clock,
    )
    message = builder.build_runtime_context_message(
        AgentTickContext(now_utc=NOW),
        GatewayResult(context=[snapshot()]),
    )
    assert '"unavailable_reason": "expired"' in message["content"]
    assert "Example.exe" not in message["content"]


def test_wake_ingestion_checks_actual_clock_after_slow_fetch(tmp_path):
    store = WakeStateStore(tmp_path / "wake.db")
    runtime = WakeRuntime.__new__(WakeRuntime)
    runtime._state = store
    runtime._clock = SimpleNamespace(now=lambda: NOW + timedelta(seconds=31))
    state = WakeRunState(ctx=WakeContext(now_utc=NOW), alerts=[], contents=[])
    try:
        runtime._ingest_source_channels(
            state, {"alert": [], "content": [], "context": [snapshot()]}
        )
        assert store.list_contexts()[0].presence == "unknown"
        assert state.context_event is None
    finally:
        store.close()


async def test_wake_final_phase_drops_context_that_expired_during_investigation(
    tmp_path, monkeypatch
):
    store = WakeStateStore(tmp_path / "wake.db")
    runtime = cast(Any, WakeRuntime.__new__(WakeRuntime))
    runtime._state = store
    current = [NOW]
    runtime._clock = SimpleNamespace(now=lambda: current[0])
    runtime._scope = SimpleNamespace(workspace_context_fn=lambda: "", shared_tools=None)
    runtime._read_memory = lambda: ""
    runtime._read_recent_passive_conversation = lambda *args: ""
    runtime._read_recent_proactive_messages = lambda *args: []
    runtime._tool_deps = SimpleNamespace()
    calls = []

    async def phase(messages, ctx, allowed, forced_name):
        calls.append(messages)
        if forced_name:
            current[0] = NOW + timedelta(seconds=31)
        else:
            ctx.terminal_action = "skip"

    runtime._run_phase = phase
    monkeypatch.setattr(
        "plugins.wake_proactive.runtime.execute",
        AsyncMock(return_value="fixture investigation"),
    )
    try:
        store.ingest_context([snapshot()], NOW)
        await runtime._run_content_tools(WakeContext(now_utc=NOW))
        assert "Example.exe" in str(calls[0])
        assert "Example.exe" not in str(calls[1])
    finally:
        store.close()


async def test_expired_context_event_skips_without_calling_model():
    runtime = cast(Any, WakeRuntime.__new__(WakeRuntime))
    runtime._clock = SimpleNamespace(now=lambda: NOW + timedelta(seconds=31))
    runtime._scope = SimpleNamespace(provider=SimpleNamespace(chat=AsyncMock()))
    state = WakeRunState(ctx=WakeContext(now_utc=NOW), alerts=[], contents=[])
    await runtime._decide_event(state, "context", snapshot(event_id="context-change"))
    assert state.ctx.terminal_action == "skip"
    runtime._scope.provider.chat.assert_not_awaited()


def test_default_prompt_retains_complete_device_snapshot_and_strips_expired_facts():
    import json

    builder = ProactivePromptBuilder(
        cfg=ProactiveConfig(),
        memory=None,
        veda_fn=lambda: "",
        workspace_context_fn=None,
    )
    item = snapshot(
        foreground={"process_name": "Editor.exe", "window_title": "标题" * 256},
        power={"battery_present": True, "battery_percent": 45, "charging": False},
    )
    block = builder.render_context_block([item], now=NOW)
    payload = json.loads(block[block.index("[") :].strip())[0]
    assert payload["power"]["battery_percent"] == 45
    expired = builder.render_context_block([item], now=NOW + timedelta(seconds=30))
    payload = json.loads(expired[expired.index("[") :].strip())[0]
    assert payload["available"] is False and "power" not in payload


def test_oversized_context_does_not_truncate_json_or_hide_later_device_snapshot():
    import json

    builder = ProactivePromptBuilder(
        cfg=ProactiveConfig(),
        memory=None,
        veda_fn=lambda: "",
        workspace_context_fn=None,
    )
    large = {"_source": "large-provider", "available": True, "notes": "x" * 8000}
    block = builder.render_context_block([large, snapshot()], now=NOW)
    encoded = next(line for line in block.splitlines() if line.startswith("["))
    payload = json.loads(encoded)
    assert len(encoded) <= 4000
    assert payload[0]["unavailable_reason"] == "context_budget_exceeded"
    assert payload[1]["context_type"] == "windows_device"
    assert "8000" not in encoded


async def test_device_and_feed_share_generic_source_gateway_without_ack_for_context(
    tmp_path,
):
    import json

    sources = [
        {
            "server": "feed",
            "channel": "content",
            "get_tool": "events",
            "ack_tool": "ack",
        },
        {"server": "device", "channel": "context", "get_tool": "get_context"},
    ]
    (tmp_path / "proactive_sources.json").write_text(
        json.dumps({"sources": sources}), encoding="utf-8"
    )

    async def call(server, tool, arguments):
        if server == "device":
            return snapshot()
        return [{"event_id": "article", "kind": "content", "body": "缓存正文"}]

    pool = SimpleNamespace(call=AsyncMock(side_effect=call))
    registered = workspace_sources(tmp_path)
    source = McpGatewaySource(pool, registered, content_limit=5)
    data = await DataGateway(feed_fn=source.feed_fn, context_fn=source.context_fn).run()
    assert len(data.context) == 1 and data.context[0]["locked"] is False
    assert data.context[0]["_source"].startswith("workspace:device-")
    assert list(data.content_store.values()) == ["缓存正文"]
    assert pool.call.await_count == 2
    device = next(item for item in registered if item.spec.server == "device")
    assert device.spec.ack_tool == ""
