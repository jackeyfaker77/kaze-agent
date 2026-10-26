"""Isolated real backend for the execution workbench probe."""
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps/backend"))
from agent.config_models import Config
from agent.provider import LLMResponse, ToolCall
from agent.tools.base import Tool
from bootstrap.tools import build_core_runtime
from core.net.http import SharedHttpResources
from desktop_bridge.server import DesktopBridgeServer


async def main():
    workspace = Path(sys.argv[1])
    http = SharedHttpResources()
    runtime = build_core_runtime(Config(provider="openai", model="fixture", api_key="fixture", memory_optimizer_enabled=False), workspace, http)
    journal = runtime.session_manager.execution_journal
    class UncertainWrite(Tool):
        name = "ui_uncertain_write"
        description = "UI safety fixture"
        parameters = {"type": "object", "properties": {}}
        async def execute(self, **kwargs):
            with (workspace / "uncertain-effects.txt").open("a") as stream:
                stream.write("effect\n")
            raise TimeoutError("receipt lost")
    runtime.tools.register(UncertainWrite())
    class ConfirmedWrite(Tool):
        name = "ui_confirmed_write"
        description = "response-loss fixture"
        parameters = {"type": "object", "properties": {}}
        async def execute(self, **kwargs):
            with (workspace / "confirmed-effects.txt").open("a") as stream:
                stream.write("effect\n")
            return "write confirmed"
    runtime.tools.register(ConfirmedWrite())
    for key, effect in [("safe", False), ("external", True)]:
        if journal.find(f"desktop:{key}", "desktop", key):
            continue
        row, _ = journal.admit(f"desktop:{key}", "desktop", key, {
            "content": key, "media": [], "sender": "user", "channel": "desktop", "chat_id": f"desktop:{key}",
            "metadata": {"input_method": "text", "raise_on_error": True, "session_key_override": f"desktop:{key}"},
        })
        journal.start(row["turn_id"])
        if effect:
            journal.append(row["turn_id"], "tool.dispatched", {"effect_id": "unknown", "name": "external_write", "arguments": {"target": "fixture"}})
        else:
            for i in range(55):
                journal.append(row["turn_id"], "reply.delta", {"content": str(i)})
        journal.finish(row["turn_id"], "failed")
    async def model(**kwargs):
        with (workspace / "model-calls.txt").open("a") as stream:
            stream.write("call\n")
        if "UI uncertain" in str(kwargs["messages"]):
            return LLMResponse(content="", tool_calls=[ToolCall(id="uncertain", name="ui_uncertain_write", arguments={})])
        if "UI lost" in str(kwargs["messages"]):
            if kwargs["messages"][-1]["role"] == "tool":
                return LLMResponse(content="UI completed once")
            return LLMResponse(content="", tool_calls=[ToolCall(id="confirmed", name="ui_confirmed_write", arguments={})])
        return LLMResponse(content="UI retry verified")
    runtime.provider.chat = model
    server = DesktopBridgeServer(runtime)
    try:
        await server.serve_stdio()
    finally:
        await runtime.stop()
        await runtime.memory_runtime.aclose()
        runtime.session_manager._store.close()
        await http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
