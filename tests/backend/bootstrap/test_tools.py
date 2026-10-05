import json
import pytest
from agent.config_models import Config
from bootstrap.tools import build_core_runtime
from core.net.http import SharedHttpResources


@pytest.mark.asyncio
async def test_core_runtime_wires_plain_sessions_and_global_services(tmp_path):
    http = SharedHttpResources()
    runtime = build_core_runtime(Config(provider="openai", model="test", api_key="fake"), tmp_path, http)
    try:
        assert runtime.loop.session_manager is runtime.session_manager
        assert runtime.tools.get_tool("message_push") is runtime.push_tool
        assert runtime.screen_observation is not None
        session = runtime.session_manager.get_or_create("desktop:test")
        session.add_message("user", "hello")
        await runtime.session_manager.save_async(session)
        runtime.session_manager.invalidate("desktop:test")
        assert runtime.session_manager.get_or_create("desktop:test").messages[0]["content"] == "hello"
    finally:
        await runtime.stop()
        await runtime.memory_runtime.aclose()
        runtime.session_manager._store.close()
        await http.aclose()
