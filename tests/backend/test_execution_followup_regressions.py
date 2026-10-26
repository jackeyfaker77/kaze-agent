"""Full entry-point coverage for the second execution review."""
import asyncio
import shlex
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest

from agent.provider import LLMResponse, ToolCall
from agent.tools.base import Tool
from core.execution.contracts import ExecutionConflict
from core.execution.journal import ExecutionJournal
from tests.backend.test_session_desktop_services import desktop  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("background,failure", [(False, "timeout"), (True, "timeout"), (False, "provider"), (True, "provider"), (True, "cancel")])
async def test_real_spawn_records_shell_and_stops_unknown_child(desktop, tmp_path, background, failure):
    runtime, bridge = desktop
    counter = tmp_path / "spawn-count.txt"
    script = tmp_path / "spawn-effect.py"
    script.write_text("from pathlib import Path\nimport time\n"
        f"p=Path({str(counter)!r})\n"
        "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')\n" + ("time.sleep(20)\n" if failure != "provider" else ""))
    child_calls = 0
    main_calls = 0
    release = asyncio.Event()
    async def model(**kwargs):
        nonlocal child_calls, main_calls
        if any(t["function"]["name"] == "spawn" for t in kwargs.get("tools", [])):
            main_calls += 1
            if main_calls == 1:
                return LLMResponse(content="", tool_calls=[ToolCall(id="spawn", name="spawn", arguments={
                    "task": "Run the supplied local fixture", "profile": "general", "run_in_background": background,
                })])
            return LLMResponse(content="parent done")
        if background:
            await release.wait()  # The child must remain protected after its parent ends.
        child_calls += 1
        if failure == "provider" and child_calls % 2 == 0:
            raise RuntimeError("child provider failed after successful write")
        if child_calls > 2:
            return LLMResponse(content="child done")
        return LLMResponse(content="", tool_calls=[ToolCall(id=f"child-{child_calls}", name="shell", arguments={
            "command": f"{shlex.quote(sys.executable.replace(chr(92), '/'))} {shlex.quote(script.as_posix())}",
            "timeout": 15 if failure == "cancel" else 1, "auto_promote": False,
        })])
    runtime.provider.chat = model
    response = await bridge.handle({"method": "chat.send", "payload": {
        "session_key": "desktop:spawn-review", "content": "run once", "execution_request_id": "spawn-review",
    }})
    journal = runtime.session_manager.execution_journal
    if background:
        assert not response.error
        release.set()
        if failure == "cancel":
            async with asyncio.timeout(10):
                while not counter.exists():
                    await asyncio.sleep(.02)
            manager = runtime.tools._tools["spawn"]._manager
            job_id = manager.list_running_jobs()[0]["job_id"]
            assert await manager.cancel(job_id)
        item = await asyncio.wait_for(runtime.bus.consume_inbound(), 15)
        assert item.event.exit_reason == "needs_review"
        # Exercise the real completion handler: no main model may retry this job.
        from agent.looping.handlers import process_spawn_completion_event
        from types import SimpleNamespace
        model_loop = AsyncMock(side_effect=AssertionError("must not reason after safety stop"))
        pipeline = SimpleNamespace(post_reasoning=AsyncMock(return_value="notified"))
        await process_spawn_completion_event(item=item, key="desktop:spawn-review",
            session_svc=SimpleNamespace(session_manager=runtime.session_manager), pipeline=pipeline,
            tools=runtime.tools, memory_window=10, run_agent_loop_fn=model_loop, prompt_render_fn=AsyncMock())
        model_loop.assert_not_called()
        row, = [r for r in journal.list_executions()["executions"] if r["source"] == "subagent"]
    else:
        assert response.error and response.error.code == "execution_needs_review"
        row = journal.get(response.error.details["turn_id"])
        assert main_calls == 1
    assert child_calls == (2 if failure == "provider" else 1) and counter.read_text() == "1"
    assert row["status"] == ("failed" if background and failure == "provider" else "needs_review")
    shell, = [e for e in row["effects"] if e.get("name") == "shell"]
    assert shell["certainty"] == ("confirmed" if failure == "provider" else "unknown")
    if not background:
        spawn, = [e for e in row["effects"] if e.get("name") == "spawn"]
        assert shell["parent_effect_id"] == spawn["effect_id"] and spawn["certainty"] == "unknown"
    for _ in range(2):
        journal.rebuild_projection()
        assert journal.get(row["turn_id"]) == row
    assert counter.read_text() == "1"


@pytest.mark.asyncio
async def test_completed_write_with_lost_reply_returns_identity_and_replays_once(desktop):
    runtime, bridge = desktop
    calls = []
    class Write(Tool):
        name = "lost_reply_counter"
        description = "counter"
        parameters = {"type": "object", "properties": {}}
        async def execute(self, **kwargs):
            calls.append("write")
            return "done"
    runtime.tools.register(Write())
    model_calls = 0
    async def model(**kwargs):
        nonlocal model_calls
        model_calls += 1
        if model_calls % 2:
            return LLMResponse(content="", tool_calls=[ToolCall(id="write", name=Write.name, arguments={})])
        return LLMResponse(content="completed reply")
    runtime.provider.chat = model
    def lost(event):
        if event["method"] == "chat.done":
            raise BrokenPipeError("response lost after durable completion")
    bridge.add_event_listener(lost)
    payload = {"session_key": "desktop:lost-reply", "content": "write once"}
    first = await bridge.handle({"method": "chat.send", "payload": payload})
    assert first.error and first.error.code == "execution_unavailable"
    assert first.error.details["status"] == "completed"
    journal = runtime.session_manager.execution_journal
    payload["execution_request_id"] = first.error.details["execution_request_id"]
    bridge.remove_event_listener(lost)
    for _ in range(2):
        journal.rebuild_projection()
        replay = await bridge.handle({"method": "chat.send", "payload": payload})
        assert not replay.error and replay.payload["reply"] == "completed reply"
    assert calls == ["write"] and model_calls == 2


def cleared(journal):
    row, _ = journal.admit("desktop:clearance", "desktop", "one", {"content": "write"})
    journal.start(row["turn_id"])
    journal.append(row["turn_id"], "tool.dispatched", {"effect_id": "write"})
    journal.finish(row["turn_id"], "failed")
    row = journal.get(row["turn_id"])
    return journal.review(row["turn_id"], expected_seq=row["last_seq"], action_id="clear",
                          decision="no_effect", note="Initial target check")


def test_clearance_revocation_is_durable_idempotent_and_prevents_retry(tmp_path):
    journal = ExecutionJournal(tmp_path)
    try:
        row = cleared(journal)
        args = dict(expected_seq=row["last_seq"], action_id="revoke", decision="close", note="Late receipt found")
        revoked = journal.review(row["turn_id"], **args)
        assert not revoked["retry"]["allowed"]
        assert journal.review(row["turn_id"], **args) == revoked
        for _ in range(2):
            journal.rebuild_projection()
            assert journal.get(row["turn_id"]) == revoked
        facts = journal.events(row["turn_id"])["events"]
        assert [f["payload"]["decision"] for f in facts if f["kind"] == "execution.reviewed"] == ["no_effect", "close"]
        with pytest.raises(ExecutionConflict):
            journal.admit_retry(row["turn_id"], expected_seq=revoked["last_seq"], action_id="retry")
    finally:
        journal.close()


def test_clearance_revoke_races_retry_with_only_one_winner(tmp_path):
    first, second = ExecutionJournal(tmp_path), ExecutionJournal(tmp_path)
    try:
        row = cleared(first)
        def revoke():
            try:
                first.review(row["turn_id"], expected_seq=row["last_seq"], action_id="revoke", decision="close", note="Late receipt")
                return "revoke"
            except ExecutionConflict:
                return "conflict"
        def retry():
            try:
                second.admit_retry(row["turn_id"], expected_seq=row["last_seq"], action_id="retry")
                return "retry"
            except ExecutionConflict:
                return "conflict"
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(revoke), pool.submit(retry)]
            results = [f.result() for f in futures]
        assert results.count("conflict") == 1
        state = first.get(row["turn_id"])
        assert bool(state["retry_turn_id"]) != (state["review"]["decision"] == "close")
        first.rebuild_projection()
        assert first.get(row["turn_id"]) == state
    finally:
        first.close(); second.close()
