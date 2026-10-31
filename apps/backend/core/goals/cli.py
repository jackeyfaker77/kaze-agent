"""Explicit local Goal entry point. Demo uses no real model, Host or channels."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

from core.execution.journal import ExecutionJournal
from .service import DemoExecutor, GoalService
from .store import GoalStore


def demo_definition(input_dir):
    return {
        "objective": "Produce result.json and pass the fixed acceptance test",
        "input_dir": str(input_dir),
        "sources": [],
        "artifacts": ["result.json"],
        "criteria": [
            {"id": "json", "kind": "json", "path": "result.json", "required": ["ok"]},
            {
                "id": "test",
                "kind": "command",
                "argv": [
                    sys.executable,
                    "-I",
                    "-c",
                    "import json,sys; from pathlib import Path; "
                    'v=json.loads((Path(sys.argv[1])/"result.json").read_text()); '
                    'print("PASS" if v["ok"] else "FAIL"); sys.exit(0 if v["ok"] else 1)',
                    "{artifact_dir}",
                ],
            },
        ],
    }


def demo_authorization():
    return {
        "source": "explicit_local_demo",
        "allowed_actions": [
            "model",
            "write_file",
            "read_file",
            "verify:file",
            "verify:json",
            "verify:command",
            "replan",
        ],
        "max_attempts": 2,
        "limits": {"total": 30, "model": 10, "tool": 10, "validation": 8, "replan": 1},
    }


async def demo(workspace):
    workspace.mkdir(parents=True, exist_ok=True)
    inputs = workspace / "trusted-inputs"
    inputs.mkdir(exist_ok=True)
    goal_id = "demo-" + uuid4().hex[:8]
    journal = ExecutionJournal(workspace)
    try:
        store = GoalStore(journal, workspace)
        store.create(goal_id, demo_definition(inputs), demo_authorization())
    finally:
        journal.close()
    transitions = []
    for _ in range(8):
        # Reopen after every stage to demonstrate durable progress and accounting.
        journal = ExecutionJournal(workspace)
        try:
            store = GoalStore(journal, workspace)
            service = GoalService(store, DemoExecutor())
            await service.tick()
            state = store.get(goal_id)
            transitions.append(
                {
                    "status": state["status"],
                    "step": state["step"],
                    "attempts": len(state["attempts"]),
                    "reason": state["reason"],
                }
            )
            if state["status"] in {"succeeded", "failed", "cancelled", "needs_review"}:
                break
        finally:
            journal.close()
    return {
        "workspace": str(workspace),
        "goal_id": goal_id,
        "transitions": transitions,
        "goal": state,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo")
    create = sub.add_parser("create")
    create.add_argument(
        "spec", type=Path, help="JSON with goal_id, definition and authorization"
    )
    show = sub.add_parser("show")
    show.add_argument("goal_id", nargs="?")
    cancel = sub.add_parser("cancel")
    cancel.add_argument("goal_id")
    cancel.add_argument("--version", type=int, required=True)
    cancel.add_argument("--revoke", action="store_true")
    args = parser.parse_args()
    if args.command == "demo":
        if args.workspace:
            # Do not write demo files into a live workspace or an existing directory.
            if args.workspace.exists():
                parser.error("Demo requires a new isolated workspace directory")
            workspace = args.workspace.resolve()
        else:
            workspace = Path(tempfile.mkdtemp(prefix="kaze-goal-demo-"))
        result = asyncio.run(demo(workspace))
    else:
        if not args.workspace:
            parser.error("--workspace is required")
        journal = ExecutionJournal(args.workspace.resolve())
        try:
            store = GoalStore(journal, args.workspace.resolve())
            if args.command == "create":
                spec = json.loads(args.spec.read_text(encoding="utf-8"))
                result = store.create(
                    spec["goal_id"],
                    spec["definition"],
                    spec["authorization"],
                    parent_execution_id=spec.get("parent_execution_id", ""),
                )
            elif args.command == "cancel":
                result = store.cancel(
                    args.goal_id, expected_version=args.version, revoke=args.revoke
                )
            else:
                result = store.get(args.goal_id) if args.goal_id else store.list()
        finally:
            journal.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
