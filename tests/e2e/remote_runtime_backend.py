"""Isolated real Host for desktop transport acceptance; only the LLM is offline.

Optional --calendar-python runs the actual Calendar MCP in its own environment.
Never reads or modifies the user's workspace or configuration.
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/backend"))
from agent.config_models import Config
from agent.provider import LLMResponse, ToolCall
from bootstrap import tools as tool_module
from bootstrap.app import AppRuntime, RuntimeFeatures
from bootstrap.runtime_host import RuntimeHost
from bootstrap.remote_host import RemoteHostServer

workspace = Path(sys.argv[1]).resolve()
workspace.mkdir(parents=True, exist_ok=True)
port = int(sys.argv[2]) if len(sys.argv) > 2 else 0
token = "offline-remote-acceptance-" + "a" * 32
calendar_python = sys.argv[3] if len(sys.argv) > 3 else ""
if calendar_python:
    settings = workspace / "calendar-test.json"
    settings.write_text(json.dumps({"database_path": str(workspace / "agenda.sqlite3")}), encoding="utf-8")
    (workspace / "mcp_servers.json").write_text(json.dumps({"servers": {"calendar": {
        "command": [calendar_python, str(ROOT / "extensions/kaze-calendar/run_mcp.py")],
        "env": {"KAZE_CALENDAR_CONFIG": str(settings)}}}}), encoding="utf-8")


async def model(**kwargs):
    messages = kwargs["messages"]
    if messages[-1]["role"] == "tool":
        return LLMResponse(content=str(messages[-1]["content"]))
    content = str(messages[-1]["content"])
    with (workspace / "calls.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"content": content}) + "\n")
    if "create-remote-todo" in content:
        return LLMResponse(content=None, tool_calls=[ToolCall("create", "mcp_calendar__todo_create", {"title": "remote-shared-todo"})])
    if "list-remote-todos" in content:
        return LLMResponse(content=None, tool_calls=[ToolCall("list", "mcp_calendar__todo_list", {})])
    await kwargs["on_content_delta"]({"content_delta": "remote "})
    if "slow-input" in content:
        (workspace / "slow-started").write_text("started")
        await asyncio.sleep(2)
    return LLMResponse(content="remote reply")


async def run():
    tool_module.build_providers = lambda *args: (SimpleNamespace(chat=model), None, None)
    tool_module._resolve_plugin_dirs = lambda *args: []
    app = AppRuntime(Config(provider="openai", model="offline", api_key="offline", memory_optimizer_enabled=False),
                     workspace, features=RuntimeFeatures(False, False))
    async def attach(core):
        if calendar_python:
            await core.mcp_registry._connect_task
            assert core.tools.get_tool("mcp_calendar__todo_create") is not None
        server = RemoteHostServer(core, token, port=port)
        async def ready():
            await server.ready.wait()
            (workspace / "remote-ready.json").write_text(json.dumps({"port": server.port, "token": token}), encoding="utf-8")
        ready_task = asyncio.create_task(ready())
        try:
            await server.run()
        finally:
            ready_task.cancel()
            await asyncio.gather(ready_task, return_exceptions=True)
    await RuntimeHost(app).run(attach, handle_signals=True)


if __name__ == "__main__":
    asyncio.run(run())
