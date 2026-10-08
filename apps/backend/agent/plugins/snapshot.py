"""为主动生命周期固定插件能力目录，并用 lease 保留正在运行的版本。"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from types import MappingProxyType
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from agent.plugins.specs import RegisteredProactiveSource
from agent.tool_hooks import ToolHook


@dataclass(frozen=True)
class ProactiveContributions:
    drift_skill_roots: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ProactivePluginGeneration:
    plugin_id: str
    contributions: ProactiveContributions


@dataclass(frozen=True)
class RuntimeSnapshot:
    tool_registry: Any
    proactive_sources: Mapping[str, RegisteredProactiveSource]
    proactive_modules: tuple[object, ...] = ()
    proactive_lifecycles: tuple[object, ...] = ()
    proactive_module_factories: tuple[object, ...] = ()
    proactive_runtime_factories: tuple[object, ...] = ()
    tool_hooks: tuple[ToolHook, ...] = ()
    generations: tuple[ProactivePluginGeneration, ...] = ()
    snapshot_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "proactive_sources", MappingProxyType(dict(self.proactive_sources))
        )

    def active_generations(self) -> tuple[ProactivePluginGeneration, ...]:
        return self.generations


class RuntimeSnapshotLease:
    def __init__(self, store: RuntimeSnapshotStore, snapshot: RuntimeSnapshot) -> None:
        self._store = store
        self.snapshot = snapshot
        self._released = False
        store._retain(snapshot)

    def fork(self) -> RuntimeSnapshotLease:
        if self._released:
            raise RuntimeError("不能延长已经释放的主动能力目录")
        return RuntimeSnapshotLease(self._store, self.snapshot)

    async def release(self) -> None:
        if not self._released:
            self._released = True
            self._store._release(self.snapshot)

    async def __aenter__(self) -> RuntimeSnapshotLease:
        if self._released:
            raise RuntimeError("主动能力目录 lease 已释放")
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.release()


class RuntimeSnapshotStore:
    def __init__(self, snapshot: RuntimeSnapshot) -> None:
        self.current = snapshot
        self._counts: dict[str, int] = {}

    async def acquire(self) -> RuntimeSnapshotLease:
        return RuntimeSnapshotLease(self, self.current)

    async def publish(self, snapshot: RuntimeSnapshot) -> None:
        if snapshot.snapshot_id == self.current.snapshot_id:
            raise ValueError("新主动能力目录必须使用独立版本")
        self.current = snapshot

    def _retain(self, snapshot: RuntimeSnapshot) -> None:
        key = snapshot.snapshot_id
        self._counts[key] = self._counts.get(key, 0) + 1

    def _release(self, snapshot: RuntimeSnapshot) -> None:
        key = snapshot.snapshot_id
        remaining = self._counts[key] - 1
        if remaining:
            self._counts[key] = remaining
        else:
            del self._counts[key]


_CURRENT_LEASE: ContextVar[RuntimeSnapshotLease | None] = ContextVar(
    "proactive_catalog", default=None
)


def bind_runtime_snapshot(lease: RuntimeSnapshotLease) -> Token:
    return _CURRENT_LEASE.set(lease)


def reset_runtime_snapshot(token: Token) -> None:
    _CURRENT_LEASE.reset(token)


def get_current_runtime_lease() -> RuntimeSnapshotLease | None:
    return _CURRENT_LEASE.get()


def get_current_runtime_snapshot() -> RuntimeSnapshot | None:
    lease = get_current_runtime_lease()
    return lease.snapshot if lease is not None else None
