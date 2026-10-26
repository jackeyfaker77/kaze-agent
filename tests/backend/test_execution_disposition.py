"""Real desktop service and model/tool boundary regression coverage."""
import pytest

from agent.provider import LLMResponse, ToolCall
from agent.tools.base import Tool
from tests.backend.test_session_desktop_services import desktop  # noqa: F401


@pytest.mark.asyncio
async def test_retry_rpc_runs_once_and_rebuild_keeps_result(desktop):
    runtime, bridge = desktop
    calls = 0
    async def model(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("offline")
        return LLMResponse(content="recovered reply")
    runtime.provider.chat = model
    request = {"session_key": "desktop:retry", "content": "original input", "execution_request_id": "original"}
    assert (await bridge.handle({"method": "chat.send", "payload": request})).error
    journal = runtime.session_manager.execution_journal
    original = journal.find("desktop:retry", "desktop", "original")
    assert original["retry"]["allowed"]
    payload = {"turn_id": original["turn_id"], "expected_seq": original["last_seq"], "action_id": "retry-action"}
    for _ in range(2):
        result = await bridge.handle({"method": "execution.retry", "payload": payload})
        assert result.error is None
        assert result.payload["reply"] == "recovered reply"
        journal.rebuild_projection()
    assert calls == 2
    assert len([m for m in runtime.session_manager.get_or_create("desktop:retry").messages if m["role"] == "assistant"]) == 1
    listed = await bridge.handle({"method": "executions.list", "payload": {"status": "failed"}})
    assert listed.payload["executions"][0]["retry_turn_id"]


@pytest.mark.asyncio
async def test_unknown_effect_stops_model_and_rpc_retry_cannot_repeat_it(desktop):
    runtime, bridge = desktop
    calls = []
    class UncertainTool(Tool):
        name = "uncertain_write"
        description = "fixture"
        parameters = {"type": "object", "properties": {}}
        async def execute(self, **kwargs):
            calls.append("effect")
            raise TimeoutError("response lost after write")
    runtime.tools.register(UncertainTool())
    async def model(**kwargs):
        calls.append("model")
        return LLMResponse(content="", tool_calls=[ToolCall(id="call", name="uncertain_write", arguments={})])
    runtime.provider.chat = model
    result = await bridge.handle({"method": "chat.send", "payload": {"session_key": "desktop:unknown", "content": "write", "execution_request_id": "unknown"}})
    assert result.error and "核对" in result.error.message
    assert calls == ["model", "effect"]
    journal = runtime.session_manager.execution_journal
    original = journal.find("desktop:unknown", "desktop", "unknown")
    assert original["status"] == "needs_review"
    payload = {"turn_id": original["turn_id"], "expected_seq": original["last_seq"], "action_id": "retry"}
    assert (await bridge.handle({"method": "execution.retry", "payload": payload})).error
    assert calls == ["model", "effect"]
    payload.update(action_id="close", decision="close", note="Checked target; write exists")
    for _ in range(2):
        closed = await bridge.handle({"method": "execution.review", "payload": payload})
        assert not closed.error and closed.payload["status"] == "needs_review"
    assert calls == ["model", "effect"]
