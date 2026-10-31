"""Serial goal controller, driven by the existing scheduler's internal tick."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import asyncio
from datetime import datetime, timezone
import json
from typing import Any

from core.execution.scope import (
    ExecutionScope,
    current_execution,
    current_effect,
    current_model_tool,
)
from .contracts import GoalConflict, TERMINAL, digest
from .validators import input_versions, validate_goal


class GoalContext:
    def __init__(self, store, state, phase):
        self.store, self.goal_id, self.phase = store, state["goal_id"], phase
        self.artifact_dir = store.artifact_dir(self.goal_id)

    async def call(self, kind, action, operation, *, units=1):
        reservation = self.store.reserve(
            self.goal_id, self.phase["phase_id"], kind, action, units=units
        )
        cancelled = False
        try:
            if kind == "validation":
                # Task cancellation cannot kill to_thread's subprocess. Keep ownership
                # until its bounded operation returns, even after repeated cancellation.
                worker = asyncio.create_task(operation())
                while True:
                    try:
                        result = await asyncio.shield(worker)
                        break
                    except asyncio.CancelledError:
                        if worker.cancelled():
                            raise
                        cancelled = True
            else:
                result = await operation()
        except BaseException as exc:
            self.store.settle(
                self.goal_id,
                reservation,
                known=False,
                receipt={"error_type": type(exc).__name__},
            )
            raise
        value = (
            asdict(result)
            if is_dataclass(result) and not isinstance(result, type)
            else result
        )
        try:
            receipt: dict[str, Any] = {"result_digest": digest(value)}
        except (TypeError, ValueError):
            receipt = {"result_type": type(value).__name__}
        if kind == "validation":
            receipt["result"] = value
        self.store.settle(self.goal_id, reservation, known=True, receipt=receipt)
        if cancelled:
            raise asyncio.CancelledError()
        return result


class GoalService:
    def __init__(self, store, executor):
        self.store, self.executor = store, executor

    def bind_scheduler(self, scheduler):
        scheduler.bind_internal_tick("goals", self.tick)

    def refresh_inputs(self, goal_id):
        state = self.store.get(goal_id)
        if state["status"] in TERMINAL:
            return state
        try:
            versions = input_versions(state, self.store.artifact_dir(goal_id))
        except (OSError, ValueError):
            self.store.hold(
                goal_id,
                "acceptance_input_unavailable",
                expected_version=state["version"],
            )
            return self.store.get(goal_id)
        self.store.observe_source(goal_id, "__local_files__", digest(versions))
        return self.store.get(goal_id)

    async def tick(self, now=None):
        self.store.recover()
        now = now or datetime.now(timezone.utc)
        for state in self.store.list():
            if state["status"] in TERMINAL:
                continue
            try:
                fresh = self.refresh_inputs(state["goal_id"])
                fresh = self.store.wake_time(state["goal_id"], now)
                if fresh["status"] == "ready":
                    await self.advance(
                        state["goal_id"], expected_version=fresh["version"]
                    )
                    return  # P0: one serial phase per scheduler tick.
            except GoalConflict:
                continue  # Another Host already owns this exact version.

    async def advance(self, goal_id, *, expected_version):
        claimed = self.store.claim(goal_id, expected_version=expected_version)
        if claimed is None:
            return self.store.get(goal_id)
        state, phase = claimed
        scope = ExecutionScope(self.store.journal, phase["turn_id"])
        token = current_execution.set(scope)
        effect_token = current_effect.set(None)
        owner_token = current_model_tool.set(None)
        result, status = {}, "completed"
        try:
            context = GoalContext(self.store, state, phase)
            context.artifact_dir.mkdir(parents=True, exist_ok=True)
            if phase["step"] == "execute":
                result = await self.executor.run(context, state)
            elif phase["step"] == "verify":
                result = await validate_goal(context, state)
            else:

                async def replan():
                    return {
                        "instruction": "Revise the artifacts using the persisted failed acceptance checks.",
                        "previous_evidence_digest": digest(state["evidence"][-1]),
                    }

                result = await context.call("replan", "replan", replan)
        except BaseException as exc:
            result, status = (
                {"error_type": type(exc).__name__, "detail": str(exc)[:2000]},
                "needs_review",
            )
            if scope.broken:
                raise
            self.store.finish(
                goal_id, phase["phase_id"], result, execution_status=status
            )
            if not isinstance(exc, Exception):
                raise
        else:
            # A swallowed SubAgent denial still leaves a durable Host block.
            self.store.finish(
                goal_id, phase["phase_id"], result, execution_status=status
            )
        finally:
            scope.active = False
            current_model_tool.reset(owner_token)
            current_effect.reset(effect_token)
            current_execution.reset(token)
        return self.store.get(goal_id)


class DemoExecutor:
    """Deterministic, isolated executor for CLI demonstrations and recovery tests."""

    async def run(self, context, state):
        async def model():
            return {"claim": "completed", "value": len(state["attempts"]) > 1}

        response = await context.call("model", "model", model)

        async def write():
            path = context.artifact_dir / "result.json"
            path.write_text(json.dumps({"ok": response["value"]}), encoding="utf-8")
            return {"path": str(path)}

        artifact = await context.call("tool", "write_file", write)
        return {"claim": response["claim"], "artifact_refs": [artifact["path"]]}
