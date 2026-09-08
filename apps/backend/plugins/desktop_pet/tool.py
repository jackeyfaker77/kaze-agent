from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from copy import deepcopy
from typing import Any
from uuid import uuid4

from agent.tools.base import Tool
from bus.events_lifecycle import DesktopPetActionRequested
from core.pets.packages import PetPackageService

_COOLDOWN_SECONDS = 3.0
_POSITION_TARGETS = frozenset(
    {"top_left", "top_right", "center", "bottom_left", "bottom_right"}
)
_MOVE_ANIMATIONS = frozenset({"", "idle", "run"})


class DesktopPetActionTool(Tool):
    """Validates one role-scoped desktop-pet command before publishing it."""

    name = "pet_action"
    description = (
        "操控当前绑定的桌宠。支持移动到受限位置，或播放当前桌宠包声明的语义动作。"
        "仅桌面端会话可用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["move", "play"],
                "description": "命令类型：move 移动位置，play 播放桌宠包动作。",
            },
            "target": {
                "type": "string",
                "enum": ["top_left", "top_right", "center", "bottom_left", "bottom_right"],
                "description": "move 的语义位置目标。",
            },
            "name": {
                "type": "string",
                "description": "play 的桌宠包动作名称。",
            },
            "animation": {
                "type": "string",
                "enum": ["", "idle", "run"],
                "description": "move 的移动表现。",
            },
        },
        "required": ["action"],
    }

    def __init__(self, *, packages: PetPackageService, event_bus, tool_registry, clock=time.monotonic):
        self._packages = packages
        self._event_bus = event_bus
        self._tool_registry = tool_registry
        self._clock = clock
        self._lock = asyncio.Lock()
        self._last_action = float("-inf")

    def _package(self):
        snapshot = self._packages.snapshot()
        return next((p for p in snapshot["packages"] if p["id"] == snapshot["selected_package_id"]), None)

    def to_schema(self):
        schema = deepcopy(super().to_schema())
        package = self._package()
        schema["function"]["parameters"]["properties"]["name"]["enum"] = list(package["actions"]) if package else []
        return schema

    async def execute(self, *, action, target="", name="", animation="", **kwargs):
        context = self._tool_registry.get_context()
        if context.get("channel") != "desktop":
            return json.dumps({"accepted": False, "reason": "unsupported_channel"})
        package = self._package()
        if not package:
            return json.dumps({"accepted": False, "reason": "no_pet_package"})
        if action == "move" and (target not in _POSITION_TARGETS or animation not in _MOVE_ANIMATIONS):
            raise ValueError("invalid pet move")
        if action == "play" and name not in package["actions"]:
            raise ValueError("unsupported pet action")
        if action not in {"move", "play"}:
            raise ValueError("invalid pet action")
        async with self._lock:
            if self._clock() - self._last_action < _COOLDOWN_SECONDS:
                return json.dumps({"accepted": False, "reason": "rate_limited"})
            event = await self._event_bus.emit(DesktopPetActionRequested(
                action_id=uuid4().hex, session_key=str(context.get("session_key") or ""),
                channel="desktop", kind=action, target=target, name=name, animation=animation,
                state=package["actions"].get(name, "")))
            if event.dispatched:
                self._last_action = self._clock()
            return json.dumps({"accepted": event.dispatched, "reason": event.error})
