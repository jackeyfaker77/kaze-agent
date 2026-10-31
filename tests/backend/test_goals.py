"""P0b: fake inference, real SQLite/processes/validators; no live workspace."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from agent.provider import LLMResponse, ToolCall
from agent.scheduler import SchedulerService
from core.execution.journal import ExecutionJournal
from core.goals.cli import demo_authorization, demo_definition
from core.goals.contracts import GoalBlocked, GoalConflict
from core.goals.executor import GoalReadTool, GoalSubagentExecutor, GoalWriteTool
from core.goals.service import DemoExecutor, GoalContext, GoalService
from core.goals.store import GoalStore


def setup(tmp_path, auth=None, spec=None):
    inputs = tmp_path / "inputs"
    inputs.mkdir(exist_ok=True)
    journal = ExecutionJournal(tmp_path)
    store = GoalStore(journal, tmp_path)
    store.create("g", spec or demo_definition(inputs), auth or demo_authorization())
    return journal, store, GoalService(store, DemoExecutor())


async def step(store, service):
    state = store.get("g")
    return await service.advance("g", expected_version=state["version"])


async def finish(store, service):
    for _ in range(10):
        state = store.get("g")
        if state["status"] in {"succeeded", "failed", "cancelled", "needs_review"}:
            return state
        await step(store, service)
    raise AssertionError("Goal did not converge within bounded test steps")


async def test_completed_claim_does_not_pass_test_and_revision_has_durable_evidence(
    tmp_path,
):
    journal, store, service = setup(tmp_path)
    try:
        executed = await step(store, service)
        assert executed["status"] == "ready" and executed["step"] == "verify"
        child = executed["phases"][0]
        assert journal.get(child["turn_id"])["status"] == "completed"
        assert child["result"]["claim"] == "completed"
        failed = await step(store, service)
        assert failed["status"] == "ready" and failed["step"] == "replan"
        evidence = failed["evidence"][0]
        assert evidence["checks"][1]["returncode"] == 1
        assert evidence["checks"][1]["stdout"].strip() == "FAIL"
        assert not evidence["passed"]
        result = await finish(store, service)
        assert result["status"] == "succeeded"
        assert len(result["attempts"]) == 2
        assert len(result["evidence"]) == 2
        assert result["evidence"][1]["checks"][1]["returncode"] == 0
        assert result["budget"]["used"] == {
            "total": 9,
            "model": 2,
            "tool": 2,
            "validation": 4,
            "replan": 1,
        }
        assert not any(result["budget"]["held"].values())
        before = store.get("g")
        journal.rebuild_projection()
        store.rebuild()
        assert store.get("g") == before
    finally:
        journal.close()


async def test_budget_exhaustion_is_explained_and_cannot_reset_on_restart(tmp_path):
    auth = demo_authorization()
    auth["limits"]["total"] = 2
    journal, store, service = setup(tmp_path, auth=auth)
    try:
        await step(store, service)
        exhausted = await step(store, service)
        assert exhausted["status"] == "failed"
        assert exhausted["reason"] == "budget_exhausted"
        assert "remaining" in exhausted["block"]["detail"]
        assert exhausted["budget"]["remaining"]["total"] == 0
    finally:
        journal.close()
    journal = ExecutionJournal(tmp_path)
    try:
        store = GoalStore(journal, tmp_path)
        assert store.get("g")["budget"]["remaining"]["total"] == 0
        assert store.claim("g", expected_version=store.get("g")["version"]) is None
    finally:
        journal.close()


async def test_cancel_after_dispatch_keeps_late_receipt_without_reopening(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    class SlowExecutor:
        async def run(self, context, state):
            async def model():
                started.set()
                await release.wait()
                return "completed"

            await context.call("model", "model", model)
            with pytest.raises(GoalBlocked):
                await context.call("tool", "write_file", model)
            return {"claim": "completed"}

    journal, store, _ = setup(tmp_path)
    service = GoalService(store, SlowExecutor())
    try:
        task = asyncio.create_task(step(store, service))
        await started.wait()
        cancelled = store.cancel(
            "g", expected_version=store.get("g")["version"], revoke=True
        )
        release.set()
        result = await task
        assert result["status"] == "cancelled"
        assert result["cancel_version"] == cancelled["cancel_version"] == 1
        assert result["authorization_version"] == 2
        assert result["budget"]["used"]["model"] == 1
        assert len(result["reservations"]) == 1
        assert result["phases"][0]["result"]["claim"] == "completed"
        assert store.claim("g", expected_version=result["version"]) is None
    finally:
        journal.close()


def test_concurrent_claim_across_journals_and_duplicate_receipts_are_unique(tmp_path):
    first, store, _ = setup(tmp_path)
    second = ExecutionJournal(tmp_path)
    peer = GoalStore(second, tmp_path)
    try:
        version = store.get("g")["version"]

        def claim(s):
            try:
                return s.claim("g", expected_version=version)
            except GoalConflict:
                return None

        with ThreadPoolExecutor(2) as pool:
            claimed = list(pool.map(claim, [store, peer]))
        assert sum(x is not None for x in claimed) == 1
        owner = store if claimed[0] else peer
        winner = claimed[0] or claimed[1]
        assert winner is not None
        state, phase = winner
        reservation = owner.reserve("g", phase["phase_id"], "model", "model")
        owner.settle("g", reservation, known=True, receipt={"ok": True})
        before = owner.get("g")["version"]
        owner.settle("g", reservation, known=True, receipt={"ok": True})
        assert owner.get("g")["version"] == before
        with pytest.raises(GoalConflict):
            owner.settle("g", reservation, known=True, receipt={"ok": False})
        assert len(owner.get("g")["phases"]) == 1
    finally:
        second.close()
        first.close()


async def test_waits_do_not_infer_and_changed_sources_invalidate_success(tmp_path):
    journal, store, service = setup(tmp_path)
    try:
        store.observe_source("g", "dependency", "v1")
        state = store.get("g")
        store.wait(
            "g",
            {"kind": "source", "source_id": "dependency"},
            expected_version=state["version"],
        )
        await service.tick()
        stable = store.get("g")
        for _ in range(3):
            await service.tick()
        assert store.get("g")["version"] == stable["version"]
        assert not stable["reservations"] and not stable["phases"]
        store.observe_source("g", "dependency", "v1")
        assert store.get("g")["status"] == "waiting"
        store.observe_source("g", "dependency", "v2")
        assert store.get("g")["status"] == "ready"
        result = await finish(store, service)
        assert result["status"] == "succeeded"
        model_count = result["budget"]["used"]["model"]
        for _ in range(3):
            await service.tick()
        assert store.get("g")["budget"]["used"]["model"] == model_count
        (store.artifact_dir("g") / "result.json").write_text(
            '{"ok":false}', encoding="utf-8"
        )
        await service.tick()
        changed = store.get("g")
        assert changed["status"] == "failed"
        assert not changed["evidence"][-1]["passed"]
        assert changed["budget"]["used"]["model"] == model_count
    finally:
        journal.close()


async def test_existing_scheduler_wakes_timer_and_closes_goal_work(tmp_path):
    journal, store, service = setup(tmp_path)
    now = datetime.now(timezone.utc)
    scheduler = SchedulerService(
        tmp_path / "schedules.json", push_tool=None, _now_fn=lambda: now
    )
    service.bind_scheduler(scheduler)
    try:
        store.wait(
            "g",
            {"kind": "time", "until": (now + timedelta(seconds=30)).isoformat()},
            expected_version=store.get("g")["version"],
        )
        await scheduler._tick()
        await asyncio.gather(*list(scheduler._active_tasks.values()))
        assert store.get("g")["status"] == "waiting"
        now += timedelta(seconds=31)
        await scheduler._tick()
        await asyncio.gather(*list(scheduler._active_tasks.values()))
        assert len(store.get("g")["phases"]) == 1
        assert not scheduler._jobs  # No synthetic notification or ordinary job.
    finally:
        await scheduler.aclose()
        journal.close()


async def test_real_subagent_fake_provider_counts_tools_and_cannot_write_trusted_tests(
    tmp_path,
):
    class Provider:
        _max_retries = 1
        calls = 0

        async def chat(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            "write",
                            "write_file",
                            {"path": "result.json", "content": '{"ok":true}'},
                        )
                    ],
                )
            return LLMResponse(content="completed")

    provider = Provider()
    journal, store, _ = setup(tmp_path)
    service = GoalService(store, GoalSubagentExecutor(provider, "fake"))
    try:
        result = await finish(store, service)
        assert result["status"] == "succeeded"
        assert provider.calls == 2
        assert (
            result["budget"]["used"]["model"] == 4
        )  # Conservative physical retry envelope.
        assert result["budget"]["used"]["tool"] == 1
        assert len(journal.get(result["phases"][0]["turn_id"])["effects"]) == 1
    finally:
        journal.close()


async def test_unauthorized_validation_and_attempt_exhaustion_cannot_be_model_overridden(
    tmp_path,
):
    auth = demo_authorization()
    auth["allowed_actions"].remove("verify:command")
    journal, store, service = setup(tmp_path, auth=auth)
    try:
        result = await finish(store, service)
        assert (
            result["status"] == "failed" and result["reason"] == "action_not_authorized"
        )
        assert len(result["evidence"]) == 0
        assert result["budget"]["used"]["validation"] == 1
    finally:
        journal.close()


def test_definition_versions_and_creation_identity_preserve_budget(tmp_path):
    journal, store, _ = setup(tmp_path)
    try:
        original = store.get("g")
        spec = dict(original["definition"], objective="A revised explicit objective")
        revised = store.revise("g", spec, expected_version=original["version"])
        assert revised["definition_version"] == 2 and len(revised["definitions"]) == 2
        assert store.get("g")["budget"] == original["budget"]
        with pytest.raises(GoalConflict):
            store.create("g", spec, original["authorization"])
        with pytest.raises(ValueError):
            store.create("../escape", spec, original["authorization"])
    finally:
        journal.close()


async def test_artifact_writer_cannot_modify_trusted_acceptance_inputs(tmp_path):
    journal, store, _ = setup(tmp_path)
    trusted = tmp_path / "inputs" / "accept.py"
    trusted.write_text("trusted acceptance", encoding="utf-8")
    try:
        original = store.get("g")
        spec = dict(original["definition"], sources=["accept.py"])
        store.revise("g", spec, expected_version=original["version"])
        claimed = store.claim("g", expected_version=store.get("g")["version"])
        assert claimed is not None
        state, phase = claimed
        context = GoalContext(store, state, phase)
        assert (
            await GoalReadTool(context, state).execute("accept.py")
            == "trusted acceptance"
        )
        with pytest.raises(ValueError, match="inside"):
            await GoalWriteTool(context).execute(str(trusted), "bypassed")
        assert trusted.read_text() == "trusted acceptance"
        assert store.get("g")["budget"]["held"]["tool"] == 1
        with pytest.raises(GoalBlocked, match="unknown"):
            store.reserve("g", phase["phase_id"], "model", "model")
        with pytest.raises(ValueError):
            store.reserve("g", phase["phase_id"], "model", "write_file")
    finally:
        journal.close()


async def test_source_changes_during_acceptance_do_not_reuse_stale_pass(tmp_path):
    journal, store, service = setup(tmp_path)
    original_settle = store.settle
    changed = False

    def source_change(goal_id, reservation, *, known, receipt):
        nonlocal changed
        original_settle(goal_id, reservation, known=known, receipt=receipt)
        item = store.get(goal_id)["reservations"][reservation]
        if item["action"] == "verify:command" and not changed:
            changed = True
            store.observe_source(goal_id, "dependency", "new-version-during-test")

    try:
        # Make the first execution pass, then invalidate its acceptance input mid-check.
        class GoodExecutor:
            async def run(self, context, state):
                async def write():
                    (context.artifact_dir / "result.json").write_text(
                        '{"ok":true}', encoding="utf-8"
                    )

                await context.call("tool", "write_file", write)
                return {"claim": "completed"}

        service.executor = GoodExecutor()
        await step(store, service)
        store.settle = source_change
        stale = await step(store, service)
        assert stale["status"] == "ready" and stale["step"] == "verify"
        assert stale["reason"] == "acceptance_inputs_changed"
        assert stale["evidence"][-1]["passed"]
        store.settle = original_settle
        verified = await step(store, service)
        assert verified["status"] == "succeeded"
        assert len(verified["attempts"]) == 1 and len(verified["evidence"]) == 2
    finally:
        journal.close()


async def test_attempt_limit_and_expiry_are_host_owned(tmp_path):
    auth = demo_authorization()
    auth["max_attempts"] = 1
    journal, store, service = setup(tmp_path, auth=auth)
    try:
        result = await finish(store, service)
        assert (
            result["status"] == "failed"
            and result["reason"] == "acceptance_failed_attempt_limit"
        )
        assert len(result["attempts"]) == 1 and result["budget"]["used"]["replan"] == 0
        auth = demo_authorization()
        auth["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        store.create("expired", demo_definition(tmp_path / "inputs"), auth)
        assert (
            store.claim("expired", expected_version=store.get("expired")["version"])
            is None
        )
        assert store.get("expired")["status"] == "cancelled"
        assert not store.get("expired")["reservations"]
    finally:
        journal.close()


def test_phase_terminal_and_goal_transition_rollback_together(tmp_path):
    journal, store, _ = setup(tmp_path)
    try:
        claimed = store.claim("g", expected_version=store.get("g")["version"])
        assert claimed is not None
        _, phase = claimed
        journal._conn.execute(
            "CREATE TRIGGER reject_goal_transition BEFORE INSERT ON events "
            "WHEN NEW.kind='goal.state' BEGIN SELECT RAISE(ABORT, 'storage failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.finish("g", phase["phase_id"], {"claim": "completed"})
        assert journal.get(phase["turn_id"])["status"] == "running"
        assert store.get("g")["status"] == "running"
        journal._conn.execute("DROP TRIGGER reject_goal_transition")
        result = store.finish("g", phase["phase_id"], {"claim": "completed"})
        assert result["status"] == "ready" and result["step"] == "verify"
        assert journal.get(phase["turn_id"])["status"] == "completed"
    finally:
        journal.close()


async def test_duplicate_advancement_executes_only_one_subtask(tmp_path):
    journal, store, service = setup(tmp_path)
    try:
        version = store.get("g")["version"]
        results = await asyncio.gather(
            service.advance("g", expected_version=version),
            service.advance("g", expected_version=version),
            return_exceptions=True,
        )
        assert sum(isinstance(r, GoalConflict) for r in results) == 1
        state = store.get("g")
        assert len(state["phases"]) == 1
        assert state["budget"]["used"]["model"] == 1
        assert len(journal.list_executions()["executions"]) == 1
    finally:
        journal.close()


_CRASH = r"""
import asyncio, json, os, sys
from pathlib import Path
from core.execution.journal import ExecutionJournal
from core.goals.cli import demo_definition, demo_authorization
from core.goals.store import GoalStore
from core.goals.service import GoalService, DemoExecutor
root, point, mode = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
inputs = root / 'inputs'; inputs.mkdir(parents=True, exist_ok=True)
journal = ExecutionJournal(root); store = GoalStore(journal, root)
if mode == 'crash':
    store.create('g', demo_definition(inputs), demo_authorization())
    if point == 'unknown_call':
        state, phase = store.claim('g', expected_version=store.get('g')['version'])
        store.reserve('g', phase['phase_id'], 'model', 'model')
        with (root / 'calls.txt').open('a') as f:
            f.write('1\n'); f.flush(); os.fsync(f.fileno())
    elif point == 'completed_step':
        asyncio.run(GoalService(store, DemoExecutor()).advance('g', expected_version=store.get('g')['version']))
    elif point == 'cancelled':
        state, phase = store.claim('g', expected_version=store.get('g')['version'])
        store.reserve('g', phase['phase_id'], 'model', 'model')
        store.cancel('g', expected_version=store.get('g')['version'])
    os._exit(73)
service = GoalService(store, DemoExecutor())
store.recover()
if point == 'completed_step':
    for _ in range(6):
        asyncio.run(service.tick())
result = store.get('g')
print(json.dumps({'status': result['status'], 'budget': result['budget'],
                  'attempts': len(result['attempts']), 'phases': len(result['phases']),
                  'calls': (root/'calls.txt').read_text() if (root/'calls.txt').exists() else ''}))
journal.close()
"""


@pytest.mark.parametrize("point", ["unknown_call", "completed_step", "cancelled"])
def test_hard_process_exit_preserves_progress_budget_and_no_unknown_replay(
    tmp_path, point
):
    root = Path(__file__).resolve().parents[2]
    env = {
        **os.environ,
        "PYTHONPATH": str(root / "apps/backend"),
        "PYTHONIOENCODING": "utf-8",
    }

    def run(mode):
        return subprocess.run(
            [sys.executable, "-c", _CRASH, str(tmp_path), point, mode],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=40,
        )

    crashed = run("crash")
    assert crashed.returncode == 73, crashed.stdout + crashed.stderr
    reports = []
    for _ in range(2):
        recovered = run("recover")
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        reports.append(json.loads(recovered.stdout))
    assert reports[0] == reports[1]
    report = reports[0]
    if point == "unknown_call":
        assert report["status"] == "needs_review"
        assert report["budget"]["held"]["model"] == 1
        assert report["calls"] == "1\n" and report["phases"] == 1
    elif point == "cancelled":
        assert (
            report["status"] == "cancelled" and report["budget"]["held"]["model"] == 1
        )
    else:
        assert report["status"] == "succeeded" and report["attempts"] == 2
        assert report["budget"]["used"]["model"] == 2
