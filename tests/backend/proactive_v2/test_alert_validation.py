from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from agent.plugins.specs import ProactiveSourceSpec, RegisteredProactiveSource
from agent.turns.orchestrator import TurnOrchestrator, TurnOrchestratorDeps
from agent.turns.result import TurnOutbound, TurnResult
from plugins.default_proactive.context import AgentTickContext
from plugins.default_proactive.resolve import ProactiveResolver
from plugins.wake_proactive.runtime import WakeRuntime
from proactive_v2.config import ProactiveConfig
from proactive_v2.mcp_sources import validate_alert_async
from proactive_v2.source_catalog import workspace_sources

NOW = datetime(2026, 10, 16, 9, tzinfo=UTC)
SOURCE = RegisteredProactiveSource(
    "workspace",
    ProactiveSourceSpec(
        id="calendar",
        channels=("alert",),
        server="calendar",
        fetch_tool="events",
        ack_tool="ack",
        validate_tool="validate",
    ),
)
EVENT = {
    "event_id": "task@v1",
    "ack_server": "workspace:calendar",
    "expires_at": (NOW + timedelta(hours=1)).isoformat(),
}


@pytest.mark.parametrize(
    "response,expected",
    [
        ({"valid_event_ids": ["task@v1"]}, "valid"),
        ({"valid_event_ids": []}, "invalid"),
        ({"valid_event_ids": [], "unavailable_event_ids": ["task@v1"]}, "unavailable"),
        ({"valid_event_ids": "task@v1"}, "unavailable"),
        ({"valid_event_ids": [False]}, "unavailable"),
        ("bad protocol", "unavailable"),
    ],
)
async def test_validation_protocol_never_consumes(response, expected):
    gateway = SimpleNamespace(call=AsyncMock(return_value=response))
    assert await validate_alert_async(gateway, [SOURCE], EVENT, now=NOW) == expected
    gateway.call.assert_awaited_once_with(
        "calendar", "validate", {"event_ids": ["task@v1"]}
    )


async def test_expiry_and_temporary_failure_remain_distinct():
    gateway = SimpleNamespace(call=AsyncMock(side_effect=RuntimeError("offline")))
    assert (
        await validate_alert_async(gateway, [SOURCE], EVENT, now=NOW) == "unavailable"
    )
    gateway.call.reset_mock()
    assert (
        await validate_alert_async(
            gateway, [SOURCE], EVENT, now=NOW + timedelta(hours=1)
        )
        == "invalid"
    )
    gateway.call.assert_not_awaited()
    assert (
        await validate_alert_async(
            gateway, [SOURCE], {**EVENT, "expires_at": "bad"}, now=NOW
        )
        == "unavailable"
    )


def test_legacy_source_keys_do_not_change_and_validator_is_declared(tmp_path):
    row = {
        "server": "feed",
        "channel": "content",
        "get_tool": "fetch",
        "ack_tool": "ack",
    }
    path = tmp_path / "proactive_sources.json"
    path.write_text(json.dumps({"sources": [row]}), encoding="utf-8")
    key = sha256(json.dumps(("feed", "fetch", "ack", "{}", "{}")).encode()).hexdigest()[
        :12
    ]
    assert workspace_sources(tmp_path)[0].spec.id == f"feed-fetch-{key}"
    row.update(channel="alert", validate_tool="validate")
    path.write_text(json.dumps({"sources": [row]}), encoding="utf-8")
    assert workspace_sources(tmp_path)[0].spec.validate_tool == "validate"


async def test_default_resolution_rejects_changed_alert_without_ack():
    validate = AsyncMock(return_value=False)
    ack = AsyncMock()
    resolver = ProactiveResolver(
        cfg=ProactiveConfig(message_dedupe_enabled=False),
        session_key="desktop:1",
        state_store=SimpleNamespace(is_delivery_duplicate=lambda *_: False),
        deduper=None,
        recent_proactive_fn=None,
        ack_fn=ack,
        alert_ack_fn=ack,
        validate_alert_fn=validate,
    )
    ctx = AgentTickContext(terminal_action="reply", final_message="提醒")
    ctx.fetched_alerts = [EVENT]
    ctx.cited_item_ids = ["workspace:calendar:task@v1"]
    decision = await resolver.resolve(ctx)
    assert decision.action == "skip" and decision.result.outbound is None
    for effect in decision.result.side_effects:
        await effect.run()
    ack.assert_not_awaited()


@pytest.mark.parametrize("cited", [[], ["workspace:feed:article@v1"]])
async def test_default_unrelated_alert_does_not_block_reply_or_delivery(cited):
    validate = AsyncMock(return_value=False)
    ack = AsyncMock()
    resolver = ProactiveResolver(
        cfg=ProactiveConfig(message_dedupe_enabled=False),
        session_key="desktop:1",
        state_store=SimpleNamespace(
            is_delivery_duplicate=lambda *_: False,
            mark_delivery=lambda *_: None,
        ),
        deduper=None,
        recent_proactive_fn=None,
        ack_fn=ack,
        alert_ack_fn=ack,
        validate_alert_fn=validate,
    )
    ctx = AgentTickContext(terminal_action="reply", final_message="Feed 或上下文消息")
    ctx.fetched_alerts = [EVENT]
    ctx.fetched_contents = [{"ack_server": "workspace:feed", "event_id": "article@v1"}]
    ctx.cited_item_ids = cited
    decision = await resolver.resolve(ctx)
    assert decision.action == "send"
    assert decision.result.delivery_guard is not None
    assert await decision.result.delivery_guard()
    for effect in decision.result.success_side_effects:
        await effect.run()
    validate.assert_not_awaited()
    if cited:
        ack.assert_awaited_once_with(cited[0], "interesting")
    else:
        ack.assert_not_awaited()


async def test_default_validates_only_cited_alert_again_at_delivery():
    validate = AsyncMock(side_effect=[True, False])
    resolver = ProactiveResolver(
        cfg=ProactiveConfig(message_dedupe_enabled=False),
        session_key="desktop:1",
        state_store=SimpleNamespace(is_delivery_duplicate=lambda *_: False),
        deduper=None,
        recent_proactive_fn=None,
        ack_fn=None,
        alert_ack_fn=None,
        validate_alert_fn=validate,
    )
    ctx = AgentTickContext(terminal_action="reply", final_message="提醒和 Feed")
    ctx.fetched_alerts = [EVENT, {**EVENT, "event_id": "unrelated@v1"}]
    ctx.cited_item_ids = ["workspace:calendar:task@v1", "workspace:feed:article@v1"]
    decision = await resolver.resolve(ctx)
    assert decision.action == "send"
    assert decision.result.delivery_guard is not None
    assert not await decision.result.delivery_guard()
    assert validate.await_count == 2
    assert all(call.args == ([EVENT],) for call in validate.await_args_list)


@pytest.mark.parametrize(
    "sources", [[], [replace(SOURCE, spec=replace(SOURCE.spec, id="new-route"))]]
)
async def test_missing_alert_source_is_invalid_without_remote_call(sources):
    gateway = SimpleNamespace(call=AsyncMock())
    assert await validate_alert_async(gateway, sources, EVENT, now=NOW) == "invalid"
    gateway.call.assert_not_awaited()


async def test_registered_legacy_alert_source_without_validator_still_works():
    gateway = SimpleNamespace(call=AsyncMock())
    legacy = replace(SOURCE, spec=replace(SOURCE.spec, validate_tool=""))
    assert await validate_alert_async(gateway, [legacy], EVENT, now=NOW) == "valid"
    gateway.call.assert_not_awaited()


@pytest.mark.parametrize(
    "status,expired", [("valid", False), ("invalid", True), ("unavailable", False)]
)
async def test_wake_validation_retires_invalid_only(status, expired, monkeypatch):
    monkeypatch.setattr(
        "plugins.wake_proactive.runtime.mcp_sources.validate_alert_async",
        AsyncMock(return_value=status),
    )
    runtime = WakeRuntime.__new__(WakeRuntime)
    runtime._scope = cast(
        Any, SimpleNamespace(mcp_gateway=object(), proactive_sources=[SOURCE])
    )
    runtime._clock = cast(Any, SimpleNamespace(now=lambda: NOW))
    expire = SimpleNamespace(expire=lambda *args: calls.append(args))
    calls = []
    runtime._state = cast(Any, expire)
    ctx = SimpleNamespace(terminal_action=None)
    state = cast(Any, SimpleNamespace(ctx=ctx))
    assert await runtime._validate_alert(state, {**EVENT, "item_id": "canonical"}) == (
        status == "valid"
    )
    assert bool(calls) == expired


async def test_delivery_guard_runs_inside_lane_before_outbound_or_history():
    order = []

    @asynccontextmanager
    async def scope(channel, chat_id):
        order.append("lane_enter")
        yield
        order.append("lane_exit")

    async def guard():
        order.append("guard")
        return False

    manager = SimpleNamespace(get_or_create=lambda _: SimpleNamespace(metadata={}))
    outbound = SimpleNamespace(dispatch=AsyncMock(return_value=True))
    orchestrator = TurnOrchestrator(
        TurnOrchestratorDeps(
            session=cast(Any, SimpleNamespace(session_manager=manager)),
            outbound=cast(Any, outbound),
            delivery_scope=scope,
        )
    )
    success = SimpleNamespace(run=AsyncMock())
    result = TurnResult(
        "reply",
        TurnOutbound("desktop:1", "过期提醒"),
        success_side_effects=[success],
        delivery_guard=guard,
    )
    assert not await orchestrator.handle_proactive_turn(
        result=result, session_key="desktop:1", channel="desktop", chat_id="1"
    )
    assert order == ["lane_enter", "guard", "lane_exit"]
    outbound.dispatch.assert_not_awaited()
    success.run.assert_not_awaited()


async def test_wake_unavailable_calendar_does_not_block_local_todo():
    runtime = WakeRuntime.__new__(WakeRuntime)
    runtime._tick_interval_seconds = 60
    validation = AsyncMock(side_effect=[False, True])
    decide = AsyncMock()
    runtime._validate_alert = validation
    runtime._decide_event = decide
    unavailable = {"id": "calendar@old"}
    todo = {"id": "todo@current"}
    state = cast(
        Any,
        SimpleNamespace(
            alerts=[unavailable, todo], ctx=SimpleNamespace(terminal_action=None)
        ),
    )
    assert await runtime.decide_content(state)
    decide.assert_awaited_once_with(state, "alert", todo, prevalidated=True)
