"""Task-local execution context. Critical persistence never uses observer fanout."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from typing import Any
from uuid import uuid4

from .contracts import JournalWriteError
from .journal import ExecutionJournal


def fact(value: Any) -> Any:
    """Preserve structured tool results; never silently stringify unknown objects."""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value


class ExecutionScope:
    def __init__(self, journal: ExecutionJournal, turn_id: str):
        self.journal = journal
        self.turn_id = turn_id
        self.broken = False
        self.failed = False
        self.active = True

    def record(self, kind: str, payload: dict):
        if self.broken or not self.active:
            raise JournalWriteError("执行记录写入失败，已停止后续操作，请核对执行状态")
        try:
            self.journal.append(self.turn_id, kind, payload)
        except Exception as exc:
            self.broken = True
            raise JournalWriteError("执行记录写入失败，已停止后续操作，请核对执行状态") from exc

    def error(self, error: BaseException):
        self.failed = True
        self.record("run.error", {"error_type": type(error).__name__})

    async def effect(self, kind: str, payload: dict, operation):
        effect_id = uuid4().hex
        self.record(f"{kind}.dispatched", {**payload, "effect_id": effect_id})
        try:
            result = await operation()
        except BaseException as exc:
            # An exception is not proof that a side effect did not happen.
            self.record(f"{kind}.returned", {"effect_id": effect_id, "certainty": "unknown", "error_type": type(exc).__name__})
            raise
        self.record(f"{kind}.returned", {"effect_id": effect_id, "certainty": "confirmed", "result": fact(result)})
        return result


current_execution: ContextVar[ExecutionScope | None] = ContextVar("current_execution", default=None)


def active_execution() -> ExecutionScope | None:
    scope = current_execution.get()
    # Background observers may inherit a context at task creation. They must not
    # append to a completed turn; a poisoned context still fences further effects.
    return scope if scope is not None and (scope.active or scope.broken) else None


def record_fact(kind: str, payload: dict):
    scope = active_execution()
    if scope is not None:
        scope.record(kind, payload)
