"""Hard-exit probes through real desktop/spawn boundaries in an isolated workspace."""
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from agent.config_models import Config
from agent.provider import LLMResponse, ToolCall
from agent.subagent import SubAgent
from agent.tools.base import Tool
from bootstrap.tools import build_core_runtime
from core.net.http import SharedHttpResources
from desktop_bridge.session_service import DesktopBridgeService


async def main():
    workspace, scenario, phase = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    http = SharedHttpResources()
    runtime = build_core_runtime(Config(provider="openai", model="fixture", api_key="fixture",
                                        memory_optimizer_enabled=False), workspace, http)
    bridge = DesktopBridgeService(runtime)
    journal = runtime.session_manager.execution_journal
    release = asyncio.Event()
    calls = {"model": 0, "tool": 0}
    payload = {"session_key": "desktop:process-recovery", "content": "write once",
               "execution_request_id": "original-request"}

    class Write(Tool):
        name = "recovery_write"
        description = "Isolated filesystem effect with a process-death boundary"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            calls["tool"] += 1
            assert phase == "crash", "Recovery must not invoke a tool"
            with (workspace / "effects.txt").open("a", encoding="utf-8") as stream:
                stream.write("one\n")
                stream.flush()
                os.fsync(stream.fileno())
            if scenario == "spawn_effect":
                os._exit(73)  # The dispatch is durable; the return is absent.
            return "write confirmed"

    async def child_model(**kwargs):
        calls["model"] += 1
        assert phase == "crash", "Recovery must not resume a child model"
        await release.wait()
        return LLMResponse(content="", tool_calls=[ToolCall(id="write", name=Write.name, arguments={})])

    async def model(**kwargs):
        calls["model"] += 1
        assert phase == "crash", "Recovery must not invoke the parent model"
        if calls["model"] == 1:
            name = "spawn" if scenario == "spawn_effect" else Write.name
            arguments = {"task": "isolated write", "profile": "general", "run_in_background": True} if name == "spawn" else {}
            return LLMResponse(content="", tool_calls=[ToolCall(id="call", name=name, arguments=arguments)])
        return LLMResponse(content="durable reply")

    runtime.provider.chat = model
    runtime.tools.register(Write())
    manager = runtime.tools._tools["spawn"]._manager
    manager._build_subagent = lambda **kwargs: SubAgent(
        provider=SimpleNamespace(chat=child_model), model="fixture", tools=[Write()])

    def lose_completed_reply(event):
        if scenario == "reply_completed" and event["method"] == "chat.done":
            os._exit(73)  # run.ended is committed, but no RPC response is delivered.

    try:
        if phase == "crash":
            bridge.add_event_listener(lose_completed_reply)
            result = await bridge.handle({"method": "chat.send", "payload": payload})
            assert result.error is None
            assert journal.find(payload["session_key"], "desktop", "original-request")["status"] == "completed"
            release.set()  # Ensure the background child outlives a completed parent.
            await asyncio.sleep(15)
            raise AssertionError("Expected hard exit was not reached")

        await bridge.recover_pending_chat_writes()
        rows = journal.list_executions()["executions"]
        event_count = journal._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        messages = bridge._snapshot(payload["session_key"])["messages"]
        for _ in range(2):
            journal.rebuild_projection()
            # Replaying the parent must read the saved reply, including when its
            # background child is still waiting for human review after recovery.
            replay = await bridge.handle({"method": "chat.send", "payload": payload})
            assert replay.error is None and replay.payload["reply"] == "durable reply"
            assert journal.list_executions()["executions"] == rows
            assert bridge._snapshot(payload["session_key"])["messages"] == messages
        assert calls == {"model": 0, "tool": 0}
        assert (workspace / "effects.txt").read_text(encoding="utf-8") == "one\n"
        assert journal._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == event_count
        print(json.dumps({"executions": rows, "messages": messages, "event_count": event_count,
                          "effect_count": 1, "calls": calls}, ensure_ascii=False))
    finally:
        await bridge.aclose()
        await runtime.stop()
        await runtime.memory_runtime.aclose()
        runtime.session_manager._store.close()
        await http.aclose()


asyncio.run(main())
