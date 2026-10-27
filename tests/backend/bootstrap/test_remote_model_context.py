"""Both local bridge and authenticated remote reads consume Host-owned facts."""
import json

import httpx
import pytest

from agent.provider import LLMProvider
from tests.backend.bootstrap.test_remote_host import remote, rpc, authenticate  # noqa: F401


@pytest.mark.asyncio
async def test_remote_production_chat_and_local_context_share_prefix(remote, monkeypatch):
    journal = remote.server.service.sessions.execution_journal
    provider = LLMProvider(api_key='fixture', max_retries=0)
    provider.execution_journal = journal
    sent = []
    def respond(request):
        sent.append(json.loads(request.content))
        chunks = [
            {'id': 's', 'object': 'chat.completion.chunk', 'created': 0, 'model': 'offline',
             'choices': [{'index': 0, 'delta': {'content': 'projected remote reply'}, 'finish_reason': None}]},
            {'id': 's', 'object': 'chat.completion.chunk', 'created': 0, 'model': 'offline',
             'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
        ]
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
            content=''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n')
    await provider._client._client.aclose()
    provider._client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(remote.app.core.provider, 'chat', provider.chat)
    socket = await authenticate(remote.url)
    try:
        first = await rpc(socket, 'chat.send', {'session_key': 'desktop:projection', 'content': 'first input',
                                               'execution_request_id': 'context-request'})
        assert not first.get('error'), first
        assert first['payload']['reply'] == 'projected remote reply'
        execution = journal.find('desktop:projection', 'desktop', 'context-request')
        assert execution is not None
        listing = await rpc(socket, 'execution.context', {'turn_id': execution['turn_id']})
        call = listing['payload']['calls'][0]
        request = {'turn_id': execution['turn_id'], 'call_id': call['call_id'], 'through_seq': call['through_seq']}
        projected = (await rpc(socket, 'execution.context', request))['payload']
        assert projected['request'] == sent[0]
        with journal._conn:
            journal._conn.execute('DELETE FROM model_context_cache')
        session = remote.server.service.sessions.get_or_create('desktop:projection')
        session.messages[-1]['content'] = 'POISONED UI CACHE'
        second = await rpc(socket, 'chat.send', {'session_key': 'desktop:projection', 'content': 'second input'})
        assert not second.get('error'), second
        assert any(m.get('content') == 'projected remote reply' for m in sent[1]['messages'])
        assert 'POISONED UI CACHE' not in json.dumps(sent[1])
        snapshot = await rpc(socket, 'session.get', {'session_key': 'desktop:projection'})
        assert 'POISONED UI CACHE' not in json.dumps(snapshot)
        await socket.close()
        socket = await authenticate(remote.url)
        request['expected_digest'] = projected['prefix_digest']
        assert (await rpc(socket, 'execution.context', request))['payload'] == projected
        local = await remote.server.service.handle({'method': 'execution.context', 'payload': request})
        assert local.payload == projected
        assert len(sent) == 2
    finally:
        await socket.close()
        await provider._client.close()
