"""Isolated stdio backend for the desktop lifecycle probe; never loads user config."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps/backend"))
from agent.config_models import Config
from agent.provider import LLMResponse
from bootstrap.tools import build_core_runtime
from core.net.http import SharedHttpResources
from desktop_bridge.server import DesktopBridgeServer


async def main():
    http = SharedHttpResources()
    runtime = build_core_runtime(
        Config(provider="openai", model="fixture", api_key="fixture"),
        Path(sys.argv[1]), http,
    )
    server = DesktopBridgeServer(runtime)

    async def provider(**kwargs):
        content = kwargs["messages"][-1]["content"]
        await server.service.publish_event({
            "id": "probe", "type": "event", "method": "probe.provider",
            "payload": {"content": content},
        })
        if "hold-" in str(content):
            await asyncio.Event().wait()
        return LLMResponse(content="verified reply")

    runtime.provider.chat = provider
    try:
        await server.serve_stdio()
    finally:
        await runtime.stop()
        await runtime.memory_runtime.aclose()
        runtime.session_manager._store.close()
        await http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
