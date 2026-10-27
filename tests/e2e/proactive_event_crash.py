"""Hard-exit probe using the real Kaze kernel, journal, outbox and conversation store."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from agent.config_models import Config
from agent.provider import LLMResponse, ToolCall
from agent.tools.base import Tool
from bootstrap.app import AppRuntime, RuntimeFeatures
from bootstrap.proactive import build_proactive_loop
import bootstrap.tools as tool_module
from core.execution.contracts import ExecutionUnavailable
from proactive_v2.config_loader import load_proactive_config

workspace, scenario, phase = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
workspace.mkdir(parents=True, exist_ok=True)
key = 'telegram:42'


def count(kind):
    with (workspace / 'calls.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'kind': kind}) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def crash(point):
    if phase == 'crash' and scenario == point:
        os._exit(73)


async def model(**kwargs):
    count('model')
    crash('model_unknown')
    response = LLMResponse(None, tool_calls=[ToolCall('decision', 'send_event', {'message': 'durable proactive reminder'})])
    if scenario == 'canonical_response':
        from core.execution.scope import active_execution, fact
        scope = active_execution()
        assert scope is not None
        projected = scope.journal.prepare_model_request(scope.turn_id, 'canonical', kwargs, 'chat.completions.v1')
        assert projected['request']['messages'] == kwargs['messages']
        scope.record('model.attempt.started', {'call_id': 'canonical', 'attempt': 1})
        scope.record('model.response.recorded', {'call_id': 'canonical', 'response': fact(response)})
        crash('canonical_response')
    return response


async def sender(*args):
    count('send')
    crash('physical_unknown')
    return SimpleNamespace(message_id='receipt-1')


class Source(Tool):
    name = 'fixture_source'
    description = 'Offline alert fixture'
    parameters = {'type': 'object', 'properties': {'consumer': {'type': 'string'}, 'event_ids': {'type': 'array'}}}

    def __init__(self, name):
        self.name = name

    async def execute(self, **kwargs):
        if self.name.endswith('fetch'):
            return json.dumps([{'event_id': 'once', 'kind': 'alert', 'title': 'remind once'}])
        count('ack')
        crash('ack_unknown')
        return '{"ok":true}'


async def main():
    provider = SimpleNamespace(chat=model)
    tool_module.build_providers = lambda *args: (provider, None, None)
    tool_module._resolve_plugin_dirs = lambda *args: []
    cfg = Config(provider='openai', model='offline', api_key='offline', memory_optimizer_enabled=False)
    cfg.proactive = load_proactive_config({'enabled': False, 'lifecycle': 'wake', 'session_key': key,
        'default_channel': 'telegram', 'default_chat_id': '42', 'anyaction_enabled': False,
        'message_dedupe_enabled': False})
    (workspace / 'proactive_sources.json').write_text(json.dumps({'sources': [{'server': 'fixture',
        'channel': 'alert', 'get_tool': 'fetch', 'ack_tool': 'ack'}]}), encoding='utf-8')
    app = AppRuntime(cfg, workspace, features=RuntimeFeatures(enable_message_channels=False))
    await app.start()
    core = app.core
    assert core is not None
    core.plugin_manager = None
    core.config.proactive.enabled = True
    for name in ('fetch', 'ack'):
        core.tools.register(Source('mcp_fixture__' + name), source_type='mcp', source_name='fixture')
    core.push_tool.register_channel('telegram', text=sender)
    if phase == 'crash':
        core.presence.record_user_message(key, datetime.now(timezone.utc) - timedelta(days=1))
    loop = build_proactive_loop(core)
    assert loop is not None
    await loop._start_current_snapshot()
    adapter = loop._recovery_runtime
    consume = adapter._state.consume_and_queue_ack
    def finish_local(**kwargs):
        consume(**kwargs)
        crash('after_finalization')
    adapter._state.consume_and_queue_ack = finish_local
    acknowledge = adapter._state.mark_acknowledged
    def ack_receipt(*args):
        acknowledge(*args)
        crash('after_ack')
    adapter._state.mark_acknowledged = ack_receipt
    journal = core.session_manager.execution_journal
    queue = core.bus.transport
    outbox = core.bus.outbox
    assert queue is not None and outbox is not None
    # Keep the production outbox worker from racing the exact injection point.
    for task in app.background_tasks:
        if task.get_name() == 'transport_outbox':
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    append = journal.append
    def append_fact(turn, kind, payload):
        if kind == 'proactive.model.requested':
            crash('before_model')
        append(turn, kind, payload)
        if kind == 'proactive.model.returned':
            crash('after_model')
        if kind == 'proactive.observed' and payload.get('stage') == 'resolve':
            crash('after_decision')
        if kind == 'proactive.observed' and payload.get('stage') == 'recovery.settled':
            crash('after_settled')
    journal.append = append_fact
    put = queue.put
    def put_intent(*args, **kwargs):
        result = put(*args, **kwargs)
        crash('after_enqueue')
        return result
    queue.put = put_intent
    part = outbox.part
    async def send_part(*args, **kwargs):
        result = await part(*args, **kwargs)
        crash('after_receipt')
        return result
    outbox.part = send_part
    state = queue.state
    def set_state(job, status, **kwargs):
        state(job, status, **kwargs)
        if status == 'done':
            crash('after_queue_done')
    queue.state = set_state
    finish = journal.finish
    def finish_turn(turn, status, result=None):
        finish(turn, status, result)
        if status == 'completed':
            crash('after_completed')
    journal.finish = finish_turn
    try:
        if phase == 'crash':
            await loop._tick_bound('fixed-trigger')
            raise AssertionError('Crash point was not reached')
        before_calls = (workspace / 'calls.jsonl').read_text() if (workspace / 'calls.jsonl').exists() else ''
        for _ in range(3):
            journal.rebuild_projection()
            journal.recover_abandoned()
            try:
                await loop._tick_bound('fixed-trigger')
            except ExecutionUnavailable:
                pass
        assert ((workspace / 'calls.jsonl').read_text() if (workspace / 'calls.jsonl').exists() else '') == before_calls
        # A new scheduled tick must not re-evaluate an unreviewed interrupted attempt.
        original = journal.find(key, 'proactive', 'fixed-trigger')
        if original['status'] != 'completed' and not journal.get(original['turn_id']).get('continuation_turn_id'):
            await loop._tick_bound()
            assert ((workspace / 'calls.jsonl').read_text() if (workspace / 'calls.jsonl').exists() else '') == before_calls
        # Replaying the log / restarting the worker does not grant permission
        # to resume proactive sends. Explicit Host confirmation does.
        recovery = None
        if phase in {'controlled', 'claim_crash'}:
            from desktop_bridge.session_service import DesktopBridgeService
            if phase == 'claim_crash':
                import desktop_bridge.proactive_recovery_service as recovery_service
                claim = recovery_service.claim_recovery
                def claim_and_crash(*args):
                    claim(*args)
                    os._exit(73)
                recovery_service.claim_recovery = claim_and_crash
            core.proactive_loop = loop
            service = DesktopBridgeService(core)
            turn_id = original['turn_id']
            plan = await service._dispatch('execution.plan', {'turn_id': turn_id}, 'plan')
            while plan.get('continuation_turn_id') and journal.get(plan['continuation_turn_id'])['status'] != 'completed':
                turn_id = plan['continuation_turn_id']
                plan = await service._dispatch('execution.plan', {'turn_id': turn_id}, 'plan')
            if plan['allowed']:
                payload = {'turn_id': turn_id, 'confirmed': True, 'plan_digest': plan['plan_digest'], 'action_id': 'confirm'}
                results = await asyncio.gather(service._dispatch('execution.continue', payload, 'one'),
                                               service._dispatch('execution.continue', {**payload, 'action_id': 'two'}, 'two'))
                assert {r['created'] for r in results} == {True, False}
                assert len({r['execution']['turn_id'] for r in results}) == 1
                recovery = results[0]['execution']['status']
            elif plan.get('continuation_turn_id'):
                recovery = journal.get(plan['continuation_turn_id'])['status']
            else:
                recovery = plan['reasons']
        core.session_manager._store.rebuild_projection()
        calls = [json.loads(line)['kind'] for line in (workspace / 'calls.jsonl').read_text().splitlines()] if (workspace / 'calls.jsonl').exists() else []
        messages = core.session_manager._store.fetch_session_messages(key)
        jobs = [dict(r) for r in queue.db.execute('SELECT id,state,attempts FROM jobs ORDER BY id')]
        links = [list(r) for r in queue.db.execute('SELECT * FROM transport_execution_links ORDER BY job,turn_id')]
        print(json.dumps({'status': original['status'], 'model': calls.count('model'), 'send': calls.count('send'), 'ack': calls.count('ack'),
            'messages': [(m['id'], m['content']) for m in messages], 'jobs': jobs, 'links': links,
            'replay_calls': 0, 'recovery': recovery, 'events': journal._conn.execute('SELECT count(*) FROM events').fetchone()[0]}))
    finally:
        await loop._stop_active_kernel()
        loop.close()
        await app.shutdown()


asyncio.run(main())
