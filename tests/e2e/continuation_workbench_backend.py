"""Isolated production bridge and SDK, with only external model I/O replaced."""
import asyncio
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(root), str(root / 'apps/backend')]
from agent.config_models import Config
from bootstrap.tools import build_core_runtime
from core.net.http import SharedHttpResources
from desktop_bridge.server import DesktopBridgeServer
from tests.backend.test_continuation import Write, seed, continuation_provider


async def main():
    workspace = Path(sys.argv[1])
    http = SharedHttpResources()
    runtime = build_core_runtime(Config(provider='openai', model='fake', api_key='fake'), workspace, http)
    server = DesktopBridgeServer(runtime)
    tool = Write()
    runtime.tools.register(tool)
    await seed(server.service, tool)
    provider, wire = await continuation_provider(server.service)
    async def count(request):
        with (workspace / 'new-model.txt').open('a') as stream:
            stream.write('call\n')
    provider._client._client.event_hooks['request'].append(count)
    try:
        await server.serve_stdio()
        assert len(wire) == 1 and tool.calls == ['one']
    finally:
        await provider._client.close()
        await runtime.stop()
        await runtime.memory_runtime.aclose()
        runtime.session_manager._store.close()
        await http.aclose()


asyncio.run(main())
