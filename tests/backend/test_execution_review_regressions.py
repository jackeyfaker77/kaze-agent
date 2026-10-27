"""Review counterexamples at real shell, delivery, and desktop boundaries."""
import json
import shlex
import sys

import pytest

from agent.provider import LLMResponse, ToolCall
from agent.tools.base import Tool
from agent.tools.message_push import MessagePushTool
from agent.tools.shell.tools import ShellTool
from agent.tools.shell.background import ShellTaskOutputTool, _BG_REGISTRY
from core.execution.contracts import ExecutionNeedsReview
from core.execution.projection import ExecutionProjectionError
from tests.backend.test_session_desktop_services import desktop  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_shell_timeout_after_effect_stops_model_and_survives_replay(desktop, tmp_path, background):
    runtime, bridge = desktop
    counter = tmp_path / "counter.txt"
    script = tmp_path / "effect.py"
    script.write_text(
        "from pathlib import Path\nimport time\n"
        f"p = Path({str(counter)!r})\n"
        "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n"
        "time.sleep(20)\n", encoding="utf-8",
    )
    launches = []
    unknown_at_model_calls = []
    class RecordingShell(ShellTool):
        async def execute(self, **kwargs):
            result = await super().execute(**kwargs)
            launches.append(json.loads(result))
            return result
    runtime.tools.register(RecordingShell(working_dir=tmp_path))
    class RecordingOutput(ShellTaskOutputTool):
        async def execute(self, **kwargs):
            result = await super().execute(**kwargs)
            if json.loads(result).get("outcome_unknown"):
                unknown_at_model_calls.append(model_calls)
            return result
    runtime.tools.register(RecordingOutput())
    model_calls = 0
    async def model(**kwargs):
        nonlocal model_calls
        model_calls += 1
        # A model that would repeat the timed-out command must never be asked again.
        if model_calls > 3:
            return LLMResponse(content="done")
        if background and launches:
            return LLMResponse(content="", tool_calls=[ToolCall(id=f"poll-{model_calls}", name="task_output", arguments={
                "task_id": launches[0]["background_task_id"], "block": True, "timeout_ms": 2000,
            })])
        return LLMResponse(content="", tool_calls=[ToolCall(id=f"call-{model_calls}", name="shell", arguments={
            "command": f"{shlex.quote(sys.executable.replace(chr(92), '/'))} {shlex.quote(script.as_posix())}",
            "timeout": 1, "auto_promote": False,
            "run_in_background": background,
        })])
    runtime.provider.chat = model
    response = await bridge.handle({"method": "chat.send", "payload": {
        "session_key": "desktop:shell-timeout", "content": "run once", "execution_request_id": "shell-timeout",
    }})
    assert response.error and response.error.code == "execution_needs_review"
    assert counter.read_text() == "1" and len(launches) == 1
    if background:
        # A poll may return running just before the hard deadline (the remaining
        # wait is rounded to milliseconds). Only an unknown result must halt
        # model calls; legitimate earlier polls do not violate that boundary.
        assert unknown_at_model_calls == [model_calls]
        assert model_calls >= 2
    else:
        assert model_calls == 1
    # A timed-out background pump is cancelled by its timer; finish cleanup
    # before fixture teardown closes the event loop.
    if background:
        import asyncio
        await asyncio.sleep(.1)
        assert launches[0]["background_task_id"] not in _BG_REGISTRY
    journal = runtime.session_manager.execution_journal
    row = journal.get(response.error.details["turn_id"])
    assert row["status"] == "needs_review"
    assert row["can_review_no_effect"] is (not background)
    effect, = [e for e in row["effects"] if e["name"] == ("task_output" if background else "shell")
               and e["certainty"] == "unknown"]
    assert effect["certainty"] == "unknown"
    assert json.loads(effect["outcome"]["result"])["outcome_unknown" if background else "interrupted"] is True
    for _ in range(2):
        journal.rebuild_projection()
        assert journal.get(row["turn_id"]) == row
    assert counter.read_text() == "1" and len(launches) == 1


@pytest.mark.asyncio
async def test_pending_unknown_input_cannot_be_resent_with_fresh_or_missing_key(desktop):
    runtime, bridge = desktop
    effects = 0
    class Write(Tool):
        name = "uncertain_counter"
        description = "counter fixture"
        parameters = {"type": "object", "properties": {}}
        async def execute(self, **kwargs):
            nonlocal effects
            effects += 1
            raise TimeoutError("write done, receipt lost")
    runtime.tools.register(Write())
    async def model(**kwargs):
        return LLMResponse(content="", tool_calls=[ToolCall(id="write", name=Write.name, arguments={})])
    runtime.provider.chat = model
    payload = {"session_key": "desktop:resend", "content": "increment", "media": []}
    first = await bridge.handle({"method": "chat.send", "payload": payload})
    assert first.error and first.error.code == "execution_needs_review"
    journal = runtime.session_manager.execution_journal
    for key in [None, "fresh", first.error.details["execution_request_id"]]:
        journal.rebuild_projection()
        retry = {**payload, **({"execution_request_id": key} if key else {})}
        response = await bridge.handle({"method": "chat.send", "payload": retry})
        assert response.error and response.error.code == "execution_needs_review"
        assert response.error.details == first.error.details
    assert effects == 1
    row = journal.get(first.error.details["turn_id"])
    reviewed = journal.review(row["turn_id"], expected_seq=row["last_seq"], action_id="verify",
                              decision="no_effect", note="Test fixture: operator verified target")
    # Human clearance still uses the explicit, linked retry admission.
    blocked = await bridge.handle({"method": "chat.send", "payload": payload})
    assert blocked.error and blocked.error.code == "execution_needs_review" and effects == 1
    async def recovered(**kwargs):
        return LLMResponse(content="recovered")
    runtime.provider.chat = recovered
    action = {"turn_id": row["turn_id"], "expected_seq": reviewed["last_seq"], "action_id": "retry"}
    for _ in range(2):
        assert not (await bridge.handle({"method": "execution.retry", "payload": action})).error
    assert effects == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed_part", [False, True])
async def test_nested_delivery_uncertainty_allows_only_truthful_review(desktop, confirmed_part):
    runtime, bridge = desktop
    push = MessagePushTool()
    sent = []
    async def text(chat_id, message):
        sent.append("text")
        if not confirmed_part:
            raise TimeoutError("send outcome unknown")
        return None
    async def image(chat_id, path):
        sent.append("image")
        raise TimeoutError("image outcome unknown")
    push.register_channel("test", text=text, image=image)
    runtime.tools.register(push)
    async def model(**kwargs):
        return LLMResponse(content="", tool_calls=[ToolCall(id="push", name="message_push", arguments={
            "channel": "test", "chat_id": "target", "message": "hello",
            **({"image": "fixture.png"} if confirmed_part else {}),
        })])
    runtime.provider.chat = model
    response = await bridge.handle({"method": "chat.send", "payload": {
        "session_key": "desktop:nested", "content": "send", "execution_request_id": "nested",
    }})
    assert response.error and response.error.code == "execution_needs_review"
    journal = runtime.session_manager.execution_journal
    row = journal.get(response.error.details["turn_id"])
    wrapper, = [effect for effect in row["effects"] if effect["kind"] == "tool"]
    deliveries = [effect for effect in row["effects"] if effect["kind"] == "delivery"]
    assert wrapper["certainty"] == "unknown"
    assert all(effect["parent_effect_id"] == wrapper["effect_id"] for effect in deliveries)
    assert row["can_review_no_effect"] is (not confirmed_part)
    before = list(sent)
    for _ in range(2):
        journal.rebuild_projection()
        assert journal.get(row["turn_id"]) == row
    args = dict(expected_seq=row["last_seq"], action_id="review", decision="no_effect", note="Checked destination")
    if confirmed_part:
        with pytest.raises(ExecutionProjectionError):
            journal.review(row["turn_id"], **args)
    else:
        reviewed = journal.review(row["turn_id"], **args)
        assert reviewed["retry"]["allowed"]
        journal.rebuild_projection()
        assert journal.get(row["turn_id"]) == reviewed
    assert sent == before


def test_unknown_hold_is_not_general_content_deduplication(tmp_path):
    from core.execution.journal import ExecutionJournal
    journal = ExecutionJournal(tmp_path)
    try:
        for key in ["first", "second"]:
            row, created = journal.admit("desktop:test", "desktop", key, {"content": "same"})
            assert created
            journal.start(row["turn_id"])
            journal.finish(row["turn_id"], "completed")
        row, _ = journal.admit("desktop:test", "desktop", "unknown", {"content": "same"})
        journal.start(row["turn_id"])
        journal.append(row["turn_id"], "tool.dispatched", {"effect_id": "e"})
        journal.finish(row["turn_id"], "failed")
        with pytest.raises(ExecutionNeedsReview):
            journal.admit("desktop:test", "desktop", "resend", {"content": "same"})
        other, created = journal.admit("desktop:test", "desktop", "other", {"content": "different"})
        assert created
        journal.start(other["turn_id"])
        journal.finish(other["turn_id"], "completed")
        state = journal.get(row["turn_id"])
        journal.review(row["turn_id"], expected_seq=state["last_seq"], action_id="close", decision="close", note="Already happened")
        assert journal.admit("desktop:test", "desktop", "intentional", {"content": "same"})[1]
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_confirmed_write_then_model_failure_cannot_be_resent(desktop):
    runtime, bridge = desktop
    effects = 0
    model_calls = 0
    class Write(Tool):
        name = "confirmed_counter"
        description = "counter fixture"
        parameters = {"type": "object", "properties": {}}
        async def execute(self, **kwargs):
            nonlocal effects
            effects += 1
            return "write confirmed"
    runtime.tools.register(Write())
    async def model(**kwargs):
        nonlocal model_calls
        model_calls += 1
        if model_calls % 2 == 0:
            raise RuntimeError("provider unavailable after write")
        return LLMResponse(content="", tool_calls=[ToolCall(id="write", name=Write.name, arguments={})])
    runtime.provider.chat = model
    payload = {"session_key": "desktop:confirmed", "content": "increment"}
    original = None
    journal = runtime.session_manager.execution_journal
    for identity in [None, "fresh"]:
        response = await bridge.handle({"method": "chat.send", "payload": {
            **payload, **({"execution_request_id": identity} if identity else {}),
        }})
        assert response.error and response.error.code == "execution_unavailable"
        original = original or response.error.details
        assert response.error.details == original
        journal.rebuild_projection()
    assert effects == 1 and model_calls == 2
    row = journal.get(original["turn_id"])
    assert row["status"] == "failed" and not row["can_review_no_effect"] and not row["retry"]["allowed"]
