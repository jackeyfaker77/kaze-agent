"""Isolated real-process crashes, using the production provider with fake wire IO."""
import asyncio
import json
import os
from pathlib import Path
import sys

import httpx

from agent.provider import LLMProvider
from agent.tool_runtime import append_tool_result
from core.execution.journal import ExecutionJournal
from core.execution.model_recording import history_chat
from core.execution.scope import ExecutionScope, current_execution

workspace, scenario, phase = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
workspace.mkdir(parents=True, exist_ok=True)


def count(name):
    path = workspace / (name + '.txt')
    value = int(path.read_text()) if path.exists() else 0
    path.write_text(str(value + 1))


def crash():
    os._exit(73)


async def main():
    journal = ExecutionJournal(workspace)
    if phase == 'recover':
        execution = journal.find('desktop:crash', 'desktop', 'stable')
        assert execution is not None
        journal.rebuild_projection()
        calls = journal.model_context(execution['turn_id'])['calls']
        projections = [journal.model_context(execution['turn_id'], call_id=c['call_id']) for c in calls]
        for _ in range(3):
            for original in projections:
                assert journal.model_context(execution['turn_id'], call_id=original['call_id'],
                    through_seq=original['through_seq'], expected_digest=original['prefix_digest']) == original
            journal.interaction_history(execution['turn_id'])
        counts = {name: int((workspace / (name + '.txt')).read_text()) if (workspace / (name + '.txt')).exists() else 0
                  for name in ('model', 'tool', 'send')}
        print(json.dumps({'counts': counts, 'status': execution['status'], 'contexts': projections,
                          'effects': execution['effects']}))
        journal.close()
        return
    row, _ = journal.admit('desktop:crash', 'desktop', 'stable', {'content': 'input'})
    journal.start(row['turn_id'])
    scope = ExecutionScope(journal, row['turn_id'])
    current_execution.set(scope)
    original_event = journal._event
    def event(record, kind, payload):
        if scenario == 'before_request_commit' and kind == 'model.request.prepared':
            crash()
        original_event(record, kind, payload)
    journal._event = event
    original_prepare = journal.prepare_model_request
    def prepare(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        if scenario == 'before_network':
            crash()
        return result
    journal.prepare_model_request = prepare
    original_response = journal.model_response
    def response(*args, **kwargs):
        result = original_response(*args, **kwargs)
        if scenario == 'after_response':
            crash()
        return result
    journal.model_response = response
    original_tool = journal.tool_context
    def tool_context(*args, **kwargs):
        result = original_tool(*args, **kwargs)
        if scenario == 'after_tool_result':
            crash()
        return result
    journal.tool_context = tool_context
    provider = LLMProvider(api_key='fixture', max_retries=0)
    def wire(request):
        count('model')
        if scenario == 'during_model':
            crash()
        return httpx.Response(200, json={'id': 'test', 'object': 'chat.completion', 'created': 0, 'model': 'm',
            'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': 'step',
                'tool_calls': [{'id': 'tc', 'type': 'function', 'function': {'name': 'write', 'arguments': '{}'}}]}}]})
    await provider._client._client.aclose()
    provider._client._client = httpx.AsyncClient(transport=httpx.MockTransport(wire))
    await history_chat(provider, messages=[{'role': 'user', 'content': 'input'}], tools=[], model='m', max_tokens=50)
    async def tool():
        count('tool')
        if scenario == 'unknown_tool':
            crash()
        return 'saved'
    result = await scope.effect('tool', {'name': 'write'}, tool)
    append_tool_result([], tool_call_id='tc', content=result)
    async def send():
        count('send')
        if scenario == 'unknown_send':
            crash()
        return 'receipt'
    await scope.effect('delivery', {}, send)
    journal.finish(scope.turn_id, 'completed')
    await provider._client.close()
    crash()


asyncio.run(main())
