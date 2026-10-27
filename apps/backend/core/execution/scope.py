"""Task-local execution context. Critical persistence never uses observer fanout."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from typing import Any
from uuid import uuid4

from .contracts import JournalWriteError, ExecutionNeedsReview
from .journal import ExecutionJournal


current_effect: ContextVar[tuple[object, str] | None] = ContextVar("current_effect", default=None)
current_model_tool: ContextVar[tuple[object, str, str] | None] = ContextVar('current_model_tool', default=None)


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
        self.review_required = False

    def record(self, kind: str, payload: dict):
        if self.broken or not self.active:
            raise JournalWriteError("执行记录写入失败，已停止后续操作，请核对执行状态")
        try:
            parent = current_effect.get()
            if kind.endswith(".dispatched") and parent is not None and parent[0] is self:
                payload = {**payload, "parent_effect_id": parent[1]}
            owner = current_model_tool.get()
            if kind.endswith('.dispatched') and owner is not None and owner[0] is self:
                payload = {**payload, 'model_call_id': owner[1], 'tool_call_id': owner[2]}
            self.journal.append(self.turn_id, kind, payload)
        except Exception as exc:
            self.broken = True
            raise JournalWriteError("执行记录写入失败，已停止后续操作，请核对执行状态") from exc

    def error(self, error: BaseException):
        self.failed = True
        self.record("run.error", {"error_type": type(error).__name__})

    def descendant_effects(self, effect_id: str) -> list[dict]:
        descendants = {effect_id}
        result = []
        for effect in sorted(self.journal.get(self.turn_id)["effects"], key=lambda effect: effect["dispatch_seq"]):
            if effect.get("parent_effect_id") in descendants:
                descendants.add(effect["effect_id"])
                result.append(effect)
        return result

    async def effect(self, kind: str, payload: dict, operation, *, certainty=None):
        if self.review_required:
            raise ExecutionNeedsReview("外部操作结果未知，已停止执行，请在工作台核对执行记录")
        effect_id = uuid4().hex
        self.record(f"{kind}.dispatched", {**payload, "effect_id": effect_id})
        token = current_effect.set((self, effect_id))
        try:
            result = await operation()
            outcome = certainty(result) if certainty is not None else "confirmed"
            if outcome not in {"confirmed", "unknown"}:
                raise ValueError("Invalid tool result certainty")
        except BaseException as exc:
            # An exception is not proof that a side effect did not happen.
            self.record(f"{kind}.returned", {"effect_id": effect_id, "certainty": "unknown", "error_type": type(exc).__name__})
            if payload.get("retry_policy") != "read_only":
                self.review_required = True
                if isinstance(exc, Exception):
                    raise ExecutionNeedsReview("外部操作结果未知，已停止执行，请在工作台核对执行记录") from exc
            raise
        finally:
            current_effect.reset(token)
        # A wrapper returning normally does not confirm its nested external work.
        # Use ancestry, so an in-flight ancestor or unrelated parallel effect is
        # not mistaken for this operation's unresolved child.
        nested_unknown = any(not effect["retry_safe"] and effect["certainty"] == "unknown"
                             for effect in self.descendant_effects(effect_id))
        if nested_unknown:
            outcome = "unknown"
        self.record(f"{kind}.returned", {"effect_id": effect_id, "certainty": outcome, "result": fact(result)})
        if nested_unknown or (outcome == "unknown" and payload.get("retry_policy") != "read_only"):
            self.review_required = True
            raise ExecutionNeedsReview("外部操作结果未知，已停止执行，请在工作台核对执行记录")
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
