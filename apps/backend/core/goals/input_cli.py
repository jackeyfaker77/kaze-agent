"""Explicit local subscriptions and an isolated P1 demonstration; no live sends."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile

from core.execution.journal import ExecutionJournal
from .cli import demo_authorization, demo_definition
from .service import DemoExecutor, GoalService
from .store import GoalStore


async def demo(workspace):
    workspace.mkdir(parents=True, exist_ok=True)
    trusted = workspace / "trusted"
    trusted.mkdir(exist_ok=True)
    journal = ExecutionJournal(workspace)
    try:
        store = GoalStore(journal, workspace)
        service = GoalService(store, DemoExecutor())
        store.create("demo", demo_definition(trusted), demo_authorization())
        service.inputs.publish(
            "wake.content",
            "isolated:dependency",
            "library",
            "1",
            1,
            {"release": "1.0"},
            owner_session="cli:demo",
        )
        service.input_sources.subscribe(
            "release",
            "demo",
            kind="wake.content",
            source_id="isolated:dependency",
            fields=["release"],
            owner_session="cli:demo",
            expected_version=store.get("demo")["version"],
        )
        for _ in range(3):
            await service.tick()
        idle_calls = store.get("demo")["budget"]["used"]["model"]
        service.inputs.publish(
            "wake.content",
            "isolated:dependency",
            "library",
            "3",
            3,
            {"release": "2.0"},
            owner_session="cli:demo",
        )
        service.inputs.publish(
            "wake.content",
            "isolated:dependency",
            "library",
            "2",
            2,
            {"release": "1.5"},
            owner_session="cli:demo",
        )
        service.inputs.publish(
            "wake.content",
            "isolated:dependency",
            "library",
            "3",
            3,
            {"release": "2.0"},
            owner_session="cli:demo",
        )
        await service.tick()
        await service.tick()
        failed = store.get("demo")["evidence"][0]
        assert not failed["passed"]
    finally:
        journal.close()
    transitions = []
    for _ in range(8):
        # Reopen the real SQLite after every stage, including failed acceptance.
        journal = ExecutionJournal(workspace)
        try:
            store = GoalStore(journal, workspace)
            service = GoalService(store, DemoExecutor())
            await service.tick()
            state = store.get("demo")
            transitions.append(
                {
                    "status": state["status"],
                    "step": state["step"],
                    "reason": state["reason"],
                }
            )
            if state["status"] == "succeeded":
                before = state["budget"]["used"]["model"]
                for _ in range(3):
                    service.inputs.publish(
                        "wake.content",
                        "isolated:dependency",
                        "library",
                        "3",
                        3,
                        {"release": "2.0"},
                        owner_session="cli:demo",
                    )
                    await service.tick()
                assert store.get("demo")["budget"]["used"]["model"] == before
                store.create(
                    "cancelled", demo_definition(trusted), demo_authorization()
                )
                service.input_sources.subscribe(
                    "cancel-release",
                    "cancelled",
                    kind="wake.content",
                    source_id="isolated:cancel",
                    fields=["release"],
                    owner_session="cli:demo",
                    expected_version=store.get("cancelled")["version"],
                )
                store.cancel(
                    "cancelled", expected_version=store.get("cancelled")["version"]
                )
                service.inputs.cancel("cancel-release")
                service.inputs.publish(
                    "wake.content",
                    "isolated:cancel",
                    "library",
                    "1",
                    1,
                    {"release": "3.0"},
                    owner_session="cli:demo",
                )
                await service.tick()
                cancelled = store.get("cancelled")
                return {
                    "schema_version": 1,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "workspace": str(workspace),
                    "mode": "offline_fake_executor_real_sqlite_and_python_validator",
                    "idle_model_calls": idle_calls,
                    "goal_status": state["status"],
                    "attempts": len(state["attempts"]),
                    "budget": state["budget"],
                    "receipts": service.inputs.receipts("release"),
                    "source_input": state["source_inputs"]["input:release"],
                    "test_returncodes": [
                        e["checks"][1]["returncode"] for e in state["evidence"]
                    ],
                    "phase_execution_ids": [p["turn_id"] for p in state["phases"]],
                    "transitions_after_restart": transitions,
                    "duplicate_model_calls": store.get("demo")["budget"]["used"][
                        "model"
                    ]
                    - before,
                    "cancelled_goal_status": cancelled["status"],
                    "cancelled_model_calls": cancelled["budget"]["used"]["model"],
                }
        finally:
            journal.close()
    raise RuntimeError("Isolated event demonstration did not converge")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo")
    subscribe = commands.add_parser("subscribe")
    subscribe.add_argument(
        "spec", type=Path, help="Host subscription JSON including expected_version"
    )
    for command in ("show", "cancel"):
        commands.add_parser(command).add_argument("subscription_id")
    args = parser.parse_args()
    if args.command == "demo":
        if args.workspace and args.workspace.exists():
            parser.error("Demo requires a new isolated workspace")
        workspace = (
            args.workspace.resolve()
            if args.workspace
            else Path(tempfile.mkdtemp(prefix="kaze-input-demo-"))
        )
        result = asyncio.run(demo(workspace))
    else:
        if not args.workspace:
            parser.error("--workspace is required")
        workspace = args.workspace.resolve()
        journal = ExecutionJournal(workspace)
        try:
            service = GoalService(GoalStore(journal, workspace), DemoExecutor())
            if args.command == "subscribe":
                spec = json.loads(args.spec.read_text(encoding="utf-8"))
                result = service.input_sources.subscribe(**spec)
            elif args.command == "cancel":
                result = {"cancelled": service.inputs.cancel(args.subscription_id)}
            else:
                result = {
                    "subscription": service.inputs.get(args.subscription_id),
                    "receipts": service.inputs.receipts(args.subscription_id),
                }
        finally:
            journal.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
