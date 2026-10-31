"""Reuse SubAgent with a fixed file-only capability set and durable Goal budget."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from agent.provider import LLMProvider
from agent.subagent import SubAgent
from agent.tools.base import Tool
from .contracts import GoalBlocked, inside


class BudgetProvider:
    def __init__(self, provider, context):
        self.provider, self.context = provider, context

    async def chat(self, **kwargs):
        # Reserve a conservative retry envelope. Never refund assumed unused sends.
        units = 1 + max(0, int(getattr(self.provider, "_max_retries", 0)))
        from core.execution.model_recording import model_attempt_guard

        token = model_attempt_guard.set(
            lambda: self.context.store.check_dispatch(
                self.context.goal_id, self.context.phase["phase_id"]
            )
        )
        try:
            return await self.context.call(
                "model", "model", lambda: self.provider.chat(**kwargs), units=units
            )
        finally:
            model_attempt_guard.reset(token)


class GoalReadTool(Tool):
    name = "read_file"
    description = (
        "Read UTF-8. Declared relative source paths resolve from input_dir; "
        "other relative paths resolve from artifact_dir."
    )
    retry_policy = "read_only"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    def __init__(self, context, state):
        self.context = context
        self.root = Path(state["definition"]["input_dir"])
        self.sources = {inside(self.root, p) for p in state["definition"]["sources"]}

    async def execute(self, path, **kwargs):
        async def read():
            candidate = Path(path)
            if not candidate.is_absolute():
                source = (self.root / candidate).resolve()
                candidate = (
                    source
                    if source in self.sources
                    else self.context.artifact_dir / candidate
                )
            candidate = candidate.resolve()
            if candidate not in self.sources:
                candidate = inside(self.context.artifact_dir, str(candidate))
            if candidate.stat().st_size > 1_000_000:
                raise ValueError("Goal file exceeds 1 MB read limit")
            return candidate.read_text(encoding="utf-8")

        async def known_read():
            try:
                return await read()
            except (OSError, ValueError, UnicodeError) as exc:
                # Read-only failures have no uncertain write to reconcile.
                return f"Read failed ({type(exc).__name__}): {exc}"

        return await self.context.call("tool", "read_file", known_read)


class GoalWriteTool(Tool):
    name = "write_file"
    description = "Write UTF-8 text inside this goal artifact directory; trusted inputs are read-only."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }

    def __init__(self, context):
        self.context = context

    async def execute(self, path, content, **kwargs):
        async def write():
            target = inside(self.context.artifact_dir, path)
            if len(content.encode("utf-8")) > 1_000_000:
                raise ValueError("Goal artifact exceeds 1 MB write limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return {"path": str(target), "bytes": target.stat().st_size}

        return json.dumps(
            await self.context.call("tool", "write_file", write), ensure_ascii=False
        )


class GoalSubagentExecutor:
    def __init__(self, provider, model, *, max_tokens=4096):
        self.provider, self.model, self.max_tokens = provider, model, max_tokens

    async def run(self, context, state):
        tools = [GoalReadTool(context, state), GoalWriteTool(context)]
        agent = SubAgent(
            # SubAgent uses only chat; this adapter preserves that provider contract.
            provider=cast(LLMProvider, BudgetProvider(self.provider, context)),
            model=self.model,
            tools=tools,
            system_prompt="Work only on the explicitly authorized goal. Write artifacts; Host validators decide acceptance.",
            max_iterations=state["definition"]["max_iterations"],
            max_tokens=self.max_tokens,
        )
        prompt = json.dumps(
            {
                "objective": state["definition"]["objective"],
                "attempt": len(state["attempts"]),
                "input_dir": state["definition"]["input_dir"],
                "sources": state["definition"]["sources"],
                "artifact_dir": str(context.artifact_dir),
                "criteria": state["definition"]["criteria"],
                "previous_acceptance": state["evidence"][-1:],
                "input_versions": state["observed_sources"],
                "source_inputs": state.get("source_inputs", {}),
            },
            ensure_ascii=False,
        )
        summary = await agent.run(prompt)
        # SubAgent intentionally catches some model errors. The Goal fence is authoritative.
        fresh = context.store.get(context.goal_id)
        if fresh["block"]:
            raise GoalBlocked(fresh["block"]["detail"])
        if agent.last_exit_reason != "completed":
            raise GoalBlocked(
                "SubAgent did not complete; inspect its execution and reservations"
            )
        return {
            "exit_reason": agent.last_exit_reason,
            "result_summary": summary[:12000],
            "child_job_id": context.phase["phase_id"],
            "artifact_refs": [str(context.artifact_dir)],
        }
