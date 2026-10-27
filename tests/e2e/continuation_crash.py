"""Real process termination at committed source/claim/continuation boundaries."""
import asyncio
import json
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(root), str(root / 'apps/backend')]
from agent.config_models import Config
from bootstrap.tools import build_core_runtime
from core.execution.delivery import record_delivery
from core.execution.scope import ExecutionScope, active_execution
from core.net.http import SharedHttpResources
from desktop_bridge.session_service import DesktopBridgeService
from desktop_bridge import continuation_service as continuation
from tests.backend import test_continuation as helpers


workspace, scenario, phase = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
workspace.mkdir(parents=True, exist_ok=True)


def count(kind):
    with (workspace / (kind + '.txt')).open('a') as stream:
        stream.write('call\n')


def crash():
    os._exit(73)


async def main():
    http = SharedHttpResources()
    runtime = build_core_runtime(Config(provider='openai', model='fake', api_key='fake'), workspace, http)
    service = DesktopBridgeService(runtime)
    journal = service.sessions.execution_journal
    class CountedTool(helpers.Write):
        async def execute(self, value, **kwargs):
            count('tool')
            if scenario == 'during_tool':
                crash()
            async def send():
                count('send')
                if scenario == 'unknown_send':
                    crash()
                return 'receipt', None
            await record_delivery(journal, 'desktop:continuation', {'channel': 'test', 'chat_id': 'self', 'content': 'once'}, send)
            return await super().execute(value, **kwargs)
    tool = CountedTool()
    runtime.tools.register(tool)
    provider = None
    try:
        if phase == 'recover':
            before = list(journal._conn.iterdump())
            rows = journal._conn.execute('SELECT turn_id,status FROM executions ORDER BY rowid').fetchall()
            plans = [continuation.recovery_plan(service, row['turn_id']) for row in rows]
            assert plans == [continuation.recovery_plan(service, row['turn_id']) for row in rows]
            assert before == list(journal._conn.iterdump())
            journal.rebuild_projection()
            assert plans == [continuation.recovery_plan(service, row['turn_id']) for row in rows]
            print(json.dumps({'plans': [{k: p[k] for k in ('turn_id', 'allowed', 'reasons', 'plan_digest')} for p in plans],
                'states': [r['status'] for r in rows], 'claims': journal._conn.execute('SELECT COUNT(*) FROM continuation_claims').fetchone()[0],
                'counts': {k: len((workspace / (k + '.txt')).read_text().splitlines()) if (workspace / (k + '.txt')).exists() else 0 for k in ('model', 'tool', 'send')}}))
            return
        original_provider = helpers.provider_with_wire
        async def counted_provider(*args, **kwargs):
            result, wire = await original_provider(*args, **kwargs)
            async def on_request(request):
                count('model')
                if scenario == 'during_new_model' and journal._conn.execute('SELECT 1 FROM continuation_claims').fetchone():
                    crash()
            result._client._client.event_hooks['request'].append(on_request)
            return result, wire
        helpers.provider_with_wire = counted_provider
        original_record = ExecutionScope.record
        def record(scope, kind, payload):
            original_record(scope, kind, payload)
            is_child = bool(journal._state(scope.turn_id).get('continuation'))
            if not is_child and {'source_boundary': 'continuation.boundary', 'source_response': 'model.response.recorded',
                'tool_dispatch': 'tool.dispatched', 'physical_result': 'tool.returned', 'semantic_result': 'interaction.tool_result'}.get(scenario) == kind:
                crash()
            if is_child and {'new_boundary': 'continuation.boundary', 'new_response': 'model.response.recorded'}.get(scenario) == kind:
                crash()
        ExecutionScope.record = record
        source, _ = await helpers.seed(service, tool)
        (workspace / 'source.txt').write_text(source)
        if scenario == 'source_terminal':
            crash()
        plan = continuation.recovery_plan(service, source)
        assert plan['allowed'], plan
        original_claim = continuation.claim_continuation
        def claim(*args, **kwargs):
            result = original_claim(*args, **kwargs)
            if scenario == 'after_claim':
                crash()
            return result
        continuation.claim_continuation = claim
        original_event = journal._event
        def event(row, kind, payload):
            original_event(row, kind, payload)
            if scenario == 'claim_transaction' and kind == 'continuation.started':
                crash()
        journal._event = event
        original_append = service.sessions.append_messages
        async def append(*args, **kwargs):
            await original_append(*args, **kwargs)
            if scenario == 'new_view' and active_execution() is not None:
                crash()
        service.sessions.append_messages = append
        provider, _ = await helpers.continuation_provider(service)
        result = await helpers.call(service, 'execution.continue', turn_id=source, plan_digest=plan['plan_digest'],
                                    action_id='confirm', confirmed=True)
        assert result['execution']['status'] == 'completed'
        if scenario == 'new_terminal':
            crash()
        raise AssertionError('Failpoint not reached')
    finally:
        (workspace / 'finally.txt').write_text('ran')
        if provider:
            await provider._client.close()
        await service.aclose()
        await runtime.stop()
        await runtime.memory_runtime.aclose()
        runtime.session_manager._store.close()
        await http.aclose()


asyncio.run(main())
