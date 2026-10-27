"""Small memory classifiers must not spend their JSON budget on reasoning."""

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from agent.provider import LLMProvider
from core.execution.journal import ExecutionJournal
from memory2.post_response_worker import PostResponseMemoryWorker


def make_worker(provider, candidates=(), implicit=None):
    memorizer = SimpleNamespace(supersede_batch=Mock())
    retriever = SimpleNamespace(retrieve=AsyncMock(return_value=list(candidates)))
    worker = PostResponseMemoryWorker(
        memorizer=memorizer, retriever=retriever, light_provider=provider,
        light_model='deepseek-v4-flash', implicit_memory_handler=implicit,
    )
    return worker, memorizer, retriever


async def wire_provider(respond, journal=None):
    provider = LLMProvider(api_key='test', provider_name='deepseek', max_retries=0,
                           extra_body={'thinking': {'type': 'enabled'}, 'reasoning_effort': 'high'})
    if journal is not None:
        provider.execution_journal = journal
    wire = []

    def handle(request):
        payload = json.loads(request.content)
        wire.append(payload)
        text, finish = respond(payload, len(wire))
        return httpx.Response(200, json={
            'id': 'test', 'object': 'chat.completion', 'created': 0,
            'model': payload['model'], 'choices': [{'index': 0, 'finish_reason': finish,
                'message': {'role': 'assistant', 'content': text}}],
        })

    await provider._client._client.aclose()
    provider._client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    return provider, wire


async def test_classifiers_use_non_thinking_wire_and_replay_without_changing_dialogue(tmp_path):
    journal = ExecutionJournal(tmp_path)
    ids = [f'memu_{n:032x}' for n in range(5)]
    def respond(payload, index):
        # Reproduce the provider's length stop if auxiliary reasoning leaks in.
        if payload['max_tokens'] <= 256 and payload.get('thinking') != {'type': 'disabled'}:
            return '', 'length'
        return (json.dumps(['旧查询流程']) if index == 1 else json.dumps(ids), 'stop')

    provider, wire = await wire_provider(respond, journal)
    original = deepcopy(provider._extra_body)
    implicit = AsyncMock()
    worker, memorizer, _ = make_worker(provider,
        [{'id': item, 'summary': '旧查询流程', 'score': .99} for item in ids], implicit)
    try:
        await worker.run('不要再按旧查询流程做了', '好的', [], 'test')
        memorizer.supersede_batch.assert_called_once_with(ids)
        implicit.assert_awaited_once()
        assert sum(p['max_tokens'] for p in wire) + worker.TOKENS_EXTRACT_IMPLICIT <= worker.TOKEN_BUDGET_PER_RUN
        assert len(wire) == 2
        for payload in wire:
            assert payload['thinking'] == {'type': 'disabled'}
            assert 'reasoning_effort' not in payload
            assert 'tools' not in payload
        executions = journal.list_executions()['executions']
        assert len(executions) == 2
        snapshots = []
        for execution in executions:
            assert execution['status'] == 'completed'
            call_id = next(iter(execution['model_calls']))
            snapshot = journal.model_context(execution['turn_id'], call_id=call_id)
            sdk_request = dict(snapshot['request'])
            sdk_request.update(sdk_request.pop('extra_body', {}))
            assert sdk_request in wire
            snapshots.append((execution['turn_id'], call_id, snapshot))
        with journal._conn:
            journal._conn.execute('DROP TABLE model_context_cache')
        journal.close()
        journal = ExecutionJournal(tmp_path)
        provider.execution_journal = journal
        for _ in range(2):
            journal.rebuild_projection()
            for turn, call_id, snapshot in snapshots:
                assert journal.model_context(turn, call_id=call_id) == snapshot
        assert len(wire) == 2  # Projection/reopening cannot issue a new request.
        await provider.chat(messages=[{'role': 'user', 'content': 'normal chat'}],
                            tools=[], model='deepseek-v4-flash', max_tokens=1024)
        assert wire[-1]['thinking'] == {'type': 'enabled'}
        assert wire[-1]['reasoning_effort'] == 'high'
        assert provider._extra_body == original
    finally:
        await provider._client.close()
        journal.close()


@pytest.mark.parametrize('stage', ['topics', 'candidates'])
@pytest.mark.parametrize('failure', ['length', 'partial_json', 'wrong_shape'])
async def test_incomplete_classification_cannot_retire_memory_or_retry_model(tmp_path, stage, failure):
    def respond(payload, index):
        if stage == 'candidates' and index == 1:
            return '["旧查询流程"]', 'stop'
        text = '["old-id"]' if stage == 'candidates' else '["旧查询流程"]'
        if failure == 'partial_json':
            text = text[:-1]  # Previously json_repair could invent the closing bracket.
        elif failure == 'wrong_shape':
            text = '{"id":"old-id"}'
        return text, 'length' if failure == 'length' else 'stop'

    journal = ExecutionJournal(tmp_path)
    provider, wire = await wire_provider(respond, journal)
    implicit = AsyncMock()
    worker, memorizer, retriever = make_worker(provider,
        [{'id': 'old-id', 'summary': '旧查询流程', 'score': .99}], implicit)
    try:
        await worker.run('不要再按旧查询流程做了', '好的', [], 'test')
        memorizer.supersede_batch.assert_not_called()
        implicit.assert_awaited_once()
        assert len(wire) == (1 if stage == 'topics' else 2)
        if stage == 'topics':
            retriever.retrieve.assert_not_awaited()
        statuses = [e['status'] for e in journal.list_executions()['executions']]
        assert statuses.count('failed') == (1 if failure == 'length' else 0)
    finally:
        await provider._client.close()
        journal.close()


async def test_invalidations_stop_before_exceeding_total_budget():
    provider = SimpleNamespace(chat=AsyncMock(side_effect=[
        SimpleNamespace(content='["a","b","c","d"]'),
        *[SimpleNamespace(content='[]') for _ in range(3)],
    ]))
    implicit = AsyncMock()
    worker, memorizer, _ = make_worker(provider,
        [{'id': 'old-id', 'summary': 'old', 'score': .99}], implicit)
    await worker.run('废弃这些流程', '好的', [], 'test')
    assert provider.chat.await_count == 4
    assert sum(c.kwargs['max_tokens'] for c in provider.chat.await_args_list) <= worker.TOKEN_BUDGET_PER_RUN
    implicit.assert_not_awaited()
    memorizer.supersede_batch.assert_not_called()
