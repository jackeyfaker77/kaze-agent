"""Authenticated remote execution inspection/retry uses the same Host authority."""
import asyncio
import json

import pytest

from agent.provider import LLMResponse, ToolCall
from agent.tools.base import Tool

from tests.backend.bootstrap.test_remote_host import remote, rpc, authenticate  # noqa: F401


@pytest.mark.asyncio
async def test_remote_retry_survives_disconnect_and_duplicate_reconnect(remote):
    journal = remote.server.service.sessions.execution_journal
    original, _ = journal.admit("desktop:retry", "desktop", "original", {
        "content": "hold-input", "media": [], "sender": "user", "channel": "desktop", "chat_id": "desktop:retry",
        "metadata": {"input_method": "text", "raise_on_error": True, "session_key_override": "desktop:retry"},
    })
    journal.start(original["turn_id"])
    journal.finish(original["turn_id"], "failed")
    original = journal.get(original["turn_id"])
    payload = {"turn_id": original["turn_id"], "expected_seq": original["last_seq"], "action_id": "retry"}
    first = await authenticate(remote.url)
    await first.send(json.dumps({"id": "lost", "method": "execution.retry", "payload": payload}))
    await asyncio.wait_for(remote.started.wait(), 5)
    await first.close()
    second = await authenticate(remote.url)
    try:
        snapshot = await rpc(second, "execution.get", {"turn_id": original["turn_id"]})
        child_id = snapshot["payload"]["retry_turn_id"]
        assert (await rpc(second, "execution.get", {"turn_id": child_id}))["payload"]["status"] == "running"
        remote.release.set()
        async with asyncio.timeout(10):
            while journal.get(child_id)["status"] == "running":
                await asyncio.sleep(.01)
        repeated = await rpc(second, "execution.retry", payload)
        assert not repeated.get("error") and repeated["payload"]["reply"] == "remote reply"
        assert len(remote.calls) == 1
        journal.rebuild_projection()
        listed = await rpc(second, "executions.list", {"status": "attention"})
        assert listed["payload"]["executions"][0]["retry_turn_id"] == child_id
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_remote_review_is_idempotent_and_never_invokes_model(remote):
    journal = remote.server.service.sessions.execution_journal
    row, _ = journal.admit("telegram:external", "delivery", "external", {})
    journal.start(row["turn_id"])
    journal.append(row["turn_id"], "delivery.dispatched", {"effect_id": "send"})
    journal.finish(row["turn_id"], "failed")
    row = journal.get(row["turn_id"])
    payload = {"turn_id": row["turn_id"], "expected_seq": row["last_seq"], "action_id": "review", "decision": "close", "note": "Checked recipient"}
    socket = await authenticate(remote.url)
    try:
        for _ in range(2):
            result = await rpc(socket, "execution.review", payload)
            assert not result.get("error") and result["payload"]["status"] == "needs_review"
        refused = await rpc(socket, "execution.retry", payload)
        assert refused["error"]
        assert not remote.calls
    finally:
        await socket.close()


@pytest.mark.asyncio
async def test_remote_unknown_error_preserves_identity_and_blocks_resend(remote):
    runtime = remote.server.service.runtime
    effects = []
    class Write(Tool):
        name = "remote_uncertain_write"
        description = "fixture"
        parameters = {"type": "object", "properties": {}}
        async def execute(self, **kwargs):
            effects.append("write")
            raise TimeoutError("receipt lost")
    runtime.tools.register(Write())
    async def model(**kwargs):
        return LLMResponse(content="", tool_calls=[ToolCall(id="write", name=Write.name, arguments={})])
    runtime.provider.chat = model
    socket = await authenticate(remote.url)
    try:
        payload = {"session_key": "desktop:remote-unknown", "content": "write"}
        first = await rpc(socket, "chat.send", payload)
        assert first["error"]["code"] == "execution_needs_review"
        assert first["error"]["details"]["status"] == "needs_review"
        await socket.close()
        socket = await authenticate(remote.url)
        again = await rpc(socket, "chat.send", {**payload, "execution_request_id": "fresh"})
        assert again["error"]["code"] == "execution_needs_review"
        assert again["error"]["details"] == first["error"]["details"]
        assert effects == ["write"]
    finally:
        await socket.close()
