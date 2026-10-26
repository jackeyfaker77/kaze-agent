"""Exercise the real local bridge/AgentLoop against an isolated fake provider."""
import asyncio
import json
import os
from pathlib import Path
import sys

from agent.config_models import Config
from agent.provider import LLMResponse
from bootstrap.tools import build_core_runtime
from core.net.http import SharedHttpResources
from desktop_bridge.session_service import DesktopBridgeService


async def main():
    workspace, mode = Path(sys.argv[1]), sys.argv[2]
    http = SharedHttpResources()
    runtime = build_core_runtime(Config(provider="openai", model="fake", api_key="fake"), workspace, http)
    bridge = DesktopBridgeService(runtime)
    payload = {"session_key": "desktop:actual-crash", "content": "original input", "media": ["original.txt"],
               "execution_request_id": "actual-request"}
    async def provider(**kwargs):
        if mode == "recover":
            raise AssertionError("A recovered request must not call the provider again")
        if mode == "partial":
            await kwargs["on_content_delta"]({"content_delta": "partial answer", "thinking_delta": "partial reasoning"})
            os._exit(73)
        return LLMResponse(content="complete answer")
    runtime.provider.chat = provider
    try:
        if mode == "recover":
            await bridge.recover_pending_chat_writes()
            before = await bridge.handle({"method": "execution.get", "payload": payload})
            assert before.error is None
            retry = await bridge.handle({"method": "chat.send", "payload": payload})
            row = before.payload
            if row["status"] == "completed":
                assert retry.error is None and retry.payload["reply"] == "complete answer"
                assert len(retry.payload["session"]["messages"]) == 2
            else:
                assert retry.error is not None and retry.error.code == "execution_unavailable"
                assert retry.error.details["turn_id"] == row["turn_id"]
            events = runtime.session_manager.execution_journal.events(row["turn_id"])["events"]
            print(json.dumps({"status": row["status"], "input": row["input"],
                "deltas": [e["payload"] for e in events if e["kind"] == "reply.delta"]}))
        else:
            result = await bridge.handle({"method": "chat.send", "payload": payload})
            assert result.error is None
            os._exit(73)
    finally:
        await bridge.aclose()
        await runtime.stop()
        await runtime.memory_runtime.aclose()
        runtime.session_manager._store.close()
        await http.aclose()


asyncio.run(main())
