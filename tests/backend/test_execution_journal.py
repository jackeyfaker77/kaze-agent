"""Execution contract, transactional admission, and real crash recovery."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from bus.events import InboundMessage, OutboundMessage
from core.execution.contracts import ExecutionConflict, ExecutionUnavailable, JournalWriteError
from core.execution.journal import ExecutionJournal
from core.execution.runner import run_recorded
from core.execution.scope import ExecutionScope, current_execution
from agent.tools.registry import ToolRegistry
from agent.tools.base import Tool


@pytest.fixture
def journal(tmp_path):
    store = ExecutionJournal(tmp_path)
    yield store
    store.close()


def accept(journal, key="stable", session="desktop:test"):
    return journal.admit(session, "desktop", key, {"content": "original", "media": ["file.png"]})[0]


def test_admission_is_atomic_and_key_content_is_immutable(journal):
    row = accept(journal)
    same, created = journal.admit("desktop:test", "desktop", "stable", row["input"])
    assert not created and same["turn_id"] == row["turn_id"]
    with pytest.raises(ExecutionConflict):
        journal.admit("desktop:test", "desktop", "stable", {"content": "changed"})
    with pytest.raises(ExecutionConflict):
        accept(journal, "other")
    assert len(journal.events(row["turn_id"])["events"]) == 1
    journal._conn.execute("CREATE TRIGGER reject_events BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        accept(journal, session="desktop:other")
    assert journal.find("desktop:other", "desktop", "stable") is None


def test_event_order_immutability_and_transition_checks(journal):
    row = accept(journal)
    turn = row["turn_id"]
    with pytest.raises(ExecutionConflict):
        journal.append(turn, "reply.delta", {"content": "not started"})
    journal.start(turn)
    journal.append(turn, "reply.delta", {"content": "中文片段"})
    journal.finish(turn, "completed", {"reply": "done"})
    with pytest.raises(ExecutionConflict):
        journal.start(turn)
    with pytest.raises(ExecutionConflict):
        journal.finish(turn, "failed")
    page = journal.events(turn, limit=2)
    rest = journal.events(turn, after=page["next_cursor"])
    assert [e["kind"] for e in page["events"] + rest["events"]] == ["request.accepted", "run.started", "reply.delta", "run.ended"]
    for sql in ["DELETE FROM events", "UPDATE events SET kind='forged'"]:
        with pytest.raises(sqlite3.IntegrityError):
            journal._conn.execute(sql)
        journal._conn.rollback()
    with pytest.raises(ValueError):
        journal.events(turn, limit=501)


def test_live_epoch_is_not_recovered_and_closed_epoch_is_recovered_once(tmp_path):
    first = ExecutionJournal(tmp_path)
    second = None
    try:
        row = accept(first)
        first.start(row["turn_id"])
        second = ExecutionJournal(tmp_path)
        assert second.root_id == first.root_id and second.epoch != first.epoch
        assert second.get(row["turn_id"])["status"] == "running"
        first.close()
        second.recover_abandoned()
        assert second.get(row["turn_id"])["status"] == "interrupted"
        count = len(second.events(row["turn_id"])["events"])
        second.recover_abandoned()
        assert len(second.events(row["turn_id"])["events"]) == count
    finally:
        first.close()
        if second:
            second.close()


@pytest.mark.asyncio
async def test_concurrent_retry_and_completed_retry_do_not_execute_twice(journal):
    item = InboundMessage("desktop", "user", "test", "hello", metadata={"execution_request_id": "same"})
    began, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def execute():
        nonlocal calls
        calls += 1
        began.set()
        await release.wait()
        return OutboundMessage("desktop", "test", "reply")
    task = asyncio.create_task(run_recorded(journal, item, item.session_key, execute))
    await began.wait()
    try:
        with pytest.raises(ExecutionUnavailable):
            await run_recorded(journal, item, item.session_key, execute)
    finally:
        release.set()
        await task
    assert (await run_recorded(journal, item, item.session_key, execute)).content == "reply"
    assert calls == 1


class CounterTool(Tool):
    name = "counter"
    description = "Records an effect"
    parameters = {"type": "object", "properties": {}}
    def __init__(self):
        self.calls = 0
    async def execute(self, **kwargs):
        self.calls += 1
        return "saved"


@pytest.mark.asyncio
async def test_tool_fact_captures_arguments_actually_dispatched(journal):
    row = accept(journal)
    journal.start(row["turn_id"])
    token = current_execution.set(ExecutionScope(journal, row["turn_id"]))
    received = {}
    class ContextTool(CounterTool):
        context_precedence = frozenset({"session_key"})
        async def execute(self, **kwargs):
            received.update(kwargs)
            return "result"
    registry = ToolRegistry()
    registry.register(ContextTool())
    try:
        await registry.execute("counter", {"session_key": "untrusted", "value": 2},
                               context={"session_key": "desktop:test", "value": 1})
        events = journal.events(row["turn_id"])["events"]
        dispatched = next(e for e in events if e["kind"] == "tool.dispatched")
        assert dispatched["payload"]["arguments"] == received == {"session_key": "desktop:test", "value": 2}
        returned = next(e for e in events if e["kind"] == "tool.returned")
        assert returned["payload"]["effect_id"] == dispatched["payload"]["effect_id"]
        assert returned["payload"]["result"] == "result"
    finally:
        current_execution.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary,expected_calls", [("tool.dispatched", 0), ("tool.returned", 1)])
async def test_failed_durable_tool_boundary_blocks_further_effects(journal, monkeypatch, boundary, expected_calls):
    row = accept(journal)
    journal.start(row["turn_id"])
    scope = ExecutionScope(journal, row["turn_id"])
    token = current_execution.set(scope)
    registry, tool = ToolRegistry(), CounterTool()
    registry.register(tool)
    append = journal.append
    def fail(turn, kind, payload):
        if kind == boundary:
            raise OSError("disk unavailable")
        append(turn, kind, payload)
    monkeypatch.setattr(journal, "append", fail)
    try:
        with pytest.raises(JournalWriteError):
            await registry.execute("counter", {})
        monkeypatch.setattr(journal, "append", append)
        with pytest.raises(JournalWriteError):
            await registry.execute("counter", {})
        assert tool.calls == expected_calls
    finally:
        current_execution.reset(token)


@pytest.mark.parametrize("fault,status", [
    ("accepted", "interrupted"), ("partial", "interrupted"),
    ("tool_before", "needs_review"), ("tool_after", "needs_review"),
    ("tool_returned", "interrupted"), ("delivery_after", "needs_review"),
    ("completed", "completed"),
])
def test_real_process_exit_recovers_committed_prefix_without_replaying(tmp_path, fault, status):
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(root / "apps" / "backend"), "PYTHONIOENCODING": "utf-8"}
    runner = root / "tests" / "e2e" / "execution_crash_backend.py"
    crashed = subprocess.run([sys.executable, str(runner), str(tmp_path), fault], cwd=root, env=env,
                             capture_output=True, text=True, timeout=45)
    assert crashed.returncode == 73, crashed.stdout + crashed.stderr
    before = (tmp_path / "effects.txt").read_text() if (tmp_path / "effects.txt").exists() else ""
    # Recovery is performed in another real process as well, then repeated.
    for _ in range(2):
        recovered = subprocess.run([sys.executable, str(runner), str(tmp_path), "recover"], cwd=root, env=env,
                                   capture_output=True, text=True, timeout=45)
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        report = json.loads(recovered.stdout.strip().splitlines()[-1])
        assert report["status"] == status
        assert report["input"]["content"] == "original input"
        assert report["input"]["media"] == ["original.png"]
        if fault == "partial":
            assert report["partial"] == "saved partial"
    after = (tmp_path / "effects.txt").read_text() if (tmp_path / "effects.txt").exists() else ""
    assert before == after


@pytest.mark.parametrize("mode,status", [("partial", "interrupted"), ("completed", "completed")])
def test_actual_local_bridge_recovers_input_partial_and_completed_result(tmp_path, mode, status):
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(root / "apps" / "backend"), "PYTHONIOENCODING": "utf-8"}
    runner = root / "tests" / "e2e" / "execution_local_crash.py"
    crash = subprocess.run([sys.executable, str(runner), str(tmp_path), mode], cwd=root, env=env,
                           capture_output=True, text=True, encoding="utf-8", timeout=45)
    assert crash.returncode == 73, crash.stdout + crash.stderr
    for _ in range(2):
        restored = subprocess.run([sys.executable, str(runner), str(tmp_path), "recover"], cwd=root, env=env,
                                  capture_output=True, text=True, encoding="utf-8", timeout=45)
        assert restored.returncode == 0, restored.stdout + restored.stderr
        report = json.loads(restored.stdout.strip().splitlines()[-1])
        assert report["status"] == status
        assert report["input"]["content"] == "original input"
        assert report["input"]["media"] == ["original.txt"]
        if mode == "partial":
            assert "".join(d.get("content", "") for d in report["deltas"]) == "partial answer"
            assert "".join(d.get("thinking", "") for d in report["deltas"]) == "partial reasoning"
