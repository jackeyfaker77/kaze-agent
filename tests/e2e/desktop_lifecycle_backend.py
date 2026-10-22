"""Isolated stdio backend for the desktop lifecycle probe; never loads user config."""
import asyncio
import os
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
    crash_mode = next((arg.split("=", 1)[1] for arg in sys.argv[2:] if arg.startswith("--crash-write=")), None)
    recover_write = "--recover-write" in sys.argv[2:] or crash_mode is not None
    if recover_write:
        insert = runtime.session_manager._store.insert_message
        failed = False

        def fail_after_commit(*args, **kwargs):
            nonlocal failed
            row = insert(*args, **kwargs)
            if not failed:
                failed = True
                raise OSError("injected failure after SQLite commit")
            return row

        runtime.session_manager._store.insert_message = fail_after_commit
        if crash_mode == "before_append":
            async def fail_append(session, messages):
                raise OSError("injected failure before SQLite write")
            runtime.session_manager.append_messages = fail_append
        elif crash_mode == "before_cleanup":
            runtime.session_manager._store.insert_message = insert
            def fail_cleanup(key):
                raise OSError("injected failure before journal cleanup")
            server.service._chat_journal.remove = fail_cleanup
        if crash_mode:
            handle = server.service.handle
            async def crash_request(request, **kwargs):
                if request.get("method") == "probe.crash":
                    os._exit(73)  # No finally, aclose, pending write flush or orderly shutdown.
                return await handle(request, **kwargs)
            server.service.handle = crash_request

    async def provider(**kwargs):
        content = kwargs["messages"][-1]["content"]
        if recover_write and "hold-recovery" in str(content):
            await kwargs["on_content_delta"]({
                "content_delta": "retained partial", "thinking_delta": "retained reasoning",
            })
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
