import asyncio
import json

import httpx
import pytest

from agent.config_models import Config
from agent.provider import LLMProvider
from bootstrap import tools as tool_module
from bootstrap.tools import build_core_runtime
from bootstrap.remote_host import RemoteHostServer
from core.net.http import SharedHttpResources
from desktop_bridge.session_service import DesktopBridgeService
from tests.backend.bootstrap.test_remote_host import authenticate, rpc, TOKEN


@pytest.mark.parametrize('remote_mode', [False, True])
@pytest.mark.parametrize('append_failure', [False, True])
async def test_real_chat_commits_memory_intent_and_host_drains_once(tmp_path, monkeypatch, remote_mode, append_failure):
    provider = LLMProvider(api_key='test', max_retries=0)
    wire = []
    def respond(request):
        body = json.loads(request.content)
        wire.append(body)
        if body.get('stream'):
            chunks = [{'id':'a','object':'chat.completion.chunk','created':0,'model':'test',
                'choices':[{'index':0,'delta':{'role':'assistant','content':'reply'},'finish_reason':None}]},
                {'id':'a','object':'chat.completion.chunk','created':0,'model':'test',
                 'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]}]
            return httpx.Response(200, text=''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n',
                                  headers={'content-type':'text/event-stream'})
        text = '[]' if body['messages'][0]['content'].startswith('判断用户消息') else '{}'
        return httpx.Response(200, json={'id':'a','object':'chat.completion','created':0,'model':'test',
            'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':text}}]})
    await provider._client._client.aclose()
    provider._client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(tool_module, 'build_providers', lambda *args: (provider, None, None))
    config = Config(provider='openai', model='test', api_key='test')
    config.memory.enabled = True
    http = SharedHttpResources()
    runtime = build_core_runtime(config, tmp_path, http)
    if append_failure:
        append = runtime.session_manager.append_messages
        attempts = 0
        async def fail_first_append(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise asyncio.CancelledError('injected interruption before append')
            return await append(*args, **kwargs)
        monkeypatch.setattr(runtime.session_manager, 'append_messages', fail_first_append)
    service = DesktopBridgeService(runtime)
    server = None
    sockets = []
    try:
        jobs = runtime.memory_runtime.engine.memory_tasks
        payload = {'session_key':'desktop:durable','content':'hello','execution_request_id':'durable-one'}
        if remote_mode:
            server = RemoteHostServer(runtime, TOKEN, port=0)
            server_task = asyncio.create_task(server.run())
            await server.ready.wait()
            sockets = [await authenticate(f'ws://127.0.0.1:{server.port}') for _ in range(2)]
            reply = await rpc(sockets[0], 'chat.send', payload)
            assert reply.get('error') is None, reply
            assert bool(reply['payload'].get('cancelled')) == append_failure
        else:
            reply = await service.handle({'method':'chat.send','payload':payload})
            assert reply.error is None, reply
            assert bool(reply.payload.get('cancelled')) == append_failure
        # Pending persistence may complete outside the originating context.
        await (server.service if server else service).recover_pending_chat_writes()
        jobs.start()
        markdown_jobs = runtime.memory_runtime.markdown.maintenance.durable
        markdown_jobs.start()
        async with asyncio.timeout(10):
            while not jobs.list_tasks()['tasks'] or jobs.list_tasks()['tasks'][0]['state'] != 'done':
                await asyncio.sleep(.02)
            while not markdown_jobs.list_tasks() or markdown_jobs.list_tasks()[0]['state'] != 'done':
                await asyncio.sleep(.02)
        assert len(markdown_jobs.list_tasks()) == 1
        assert markdown_jobs.list_tasks()[0]['source_turn'] == jobs.list_tasks()['tasks'][0]['source_turn_id']
        assert len(wire) == 3
        before = list(jobs.db.iterdump())
        if remote_mode:
            results = await asyncio.gather(*(rpc(s, 'memory.tasks.list') for s in sockets))
            assert results[0]['payload'] == results[1]['payload']
            assert results[0]['payload']['tasks'][0]['state'] == 'done'
            assert results[0]['payload']['markdown_tasks'][0]['state'] == 'done'
        else:
            result = await service.handle({'method':'memory.tasks.list','payload':{}})
            assert result.payload['tasks'][0]['state'] == 'done'
            assert result.payload['markdown_tasks'][0]['state'] == 'done'
        assert list(jobs.db.iterdump()) == before and len(wire) == 3
        session = runtime.session_manager.get_or_create('desktop:durable')
        assert [m['content'] for m in session.messages] == ['hello','reply']
    finally:
        for socket in sockets:
            await socket.close()
        if server is not None:
            server_task.cancel()
            await asyncio.gather(server_task, return_exceptions=True)
        await service.aclose()
        await runtime.stop()
        await runtime.memory_runtime.aclose()
        runtime.session_manager._store.close()
        await provider._client.close()
        await http.aclose()
