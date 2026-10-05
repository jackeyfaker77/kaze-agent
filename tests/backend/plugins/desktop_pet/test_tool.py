from __future__ import annotations

import asyncio
import json

import pytest

from agent.tools.registry import ToolRegistry
from bus.event_bus import EventBus
from bus.events_lifecycle import DesktopPetActionRequested
from core.pets.packages import PetPackageService
from plugins.desktop_pet.tool import DesktopPetActionTool


def _build_tool(tmp_path, *, clock_value, selected=True):
    packages = PetPackageService(tmp_path)
    package = packages.root / "pet-1"
    package.mkdir()
    (package / "pet.json").write_text(json.dumps({"displayName": "Pet", "actions": {"greeting": "waving"}}), encoding="utf-8")
    if selected:
        packages.select("pet-1")
    events = []
    event_bus = EventBus()

    async def dispatch(event):
        events.append(event)
        event.dispatched = True
        return event

    event_bus.on(DesktopPetActionRequested, dispatch)
    registry = ToolRegistry()
    registry.set_context(channel="desktop", session_key="desktop:pet")
    tool = DesktopPetActionTool(packages=packages, event_bus=event_bus, tool_registry=registry, clock=lambda: clock_value[0])
    return tool, registry, events


def test_schema_exposes_selected_package_actions(tmp_path):
    tool, registry, _ = _build_tool(tmp_path, clock_value=[0.0])
    registry.register(tool, always_on=True)
    assert registry.get_schemas(names={"pet_action"})[0]["function"]["parameters"]["properties"]["name"]["enum"] == ["greeting"]


async def test_missing_selection_has_no_actions_and_rejects_dispatch(tmp_path):
    tool, _, events = _build_tool(tmp_path, clock_value=[0.0], selected=False)
    assert tool.to_schema()["function"]["parameters"]["properties"]["name"]["enum"] == []
    assert json.loads(await tool.execute(action="move", target="center")) == {"accepted": False, "reason": "no_pet_package"}
    assert events == []


async def test_external_channel_cannot_dispatch(tmp_path):
    tool, registry, events = _build_tool(tmp_path, clock_value=[0.0])
    registry.set_context(channel="telegram", session_key="telegram:1")
    assert json.loads(await tool.execute(action="move", target="center"))["reason"] == "unsupported_channel"
    assert events == []


async def test_declared_play_action_carries_session_and_sprite_state(tmp_path):
    tool, _, events = _build_tool(tmp_path, clock_value=[0.0])
    assert json.loads(await tool.execute(action="play", name="greeting"))["accepted"] is True
    assert (events[0].session_key, events[0].kind, events[0].name, events[0].state) == ("desktop:pet", "play", "greeting", "waving")


@pytest.mark.parametrize("arguments", [{"action": "play", "name": "unknown"}, {"action": "move", "target": "arbitrary"}, {"action": "move", "target": "center", "animation": "invalid"}, {"action": "unknown"}])
async def test_invalid_commands_never_dispatch(tmp_path, arguments):
    tool, _, events = _build_tool(tmp_path, clock_value=[0.0])
    with pytest.raises(ValueError):
        await tool.execute(**arguments)
    assert events == []


async def test_cooldown_is_shared_between_sessions_and_expires(tmp_path):
    clock = [0.0]
    tool, registry, events = _build_tool(tmp_path, clock_value=clock)
    assert json.loads(await tool.execute(action="move", target="center"))["accepted"] is True
    registry.set_context(channel="desktop", session_key="desktop:other")
    clock[0] = 2.9
    assert json.loads(await tool.execute(action="move", target="top_left"))["reason"] == "rate_limited"
    clock[0] = 3.0
    assert json.loads(await tool.execute(action="move", target="top_left"))["accepted"] is True
    assert len(events) == 2


async def test_concurrent_calls_are_serialized(tmp_path):
    tool, _, _ = _build_tool(tmp_path, clock_value=[0.0])
    started, release = asyncio.Event(), asyncio.Event()
    async def dispatch(event):
        started.set()
        await release.wait()
        event.dispatched = True
        return event
    event_bus = EventBus()
    event_bus.on(DesktopPetActionRequested, dispatch)
    tool._event_bus = event_bus
    first = asyncio.create_task(tool.execute(action="move", target="center"))
    await started.wait()
    second = asyncio.create_task(tool.execute(action="move", target="top_left"))
    await asyncio.sleep(0)
    assert not second.done()
    release.set()
    results = [json.loads(result) for result in await asyncio.gather(first, second)]
    assert results[0]["accepted"] is True
    assert results[1]["reason"] == "rate_limited"
