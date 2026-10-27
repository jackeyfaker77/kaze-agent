"""Local and remote desktops use the same Host-owned proactive recovery service."""
from __future__ import annotations

import asyncio

from core.execution.proactive_recovery import boundary_facts, claim_recovery, plan_recovery, delivery_job, delivery_evidence
from core.execution.proactive import observe
from core.execution.scope import ExecutionScope, current_execution
from core.execution.contracts import ExecutionConflict, ExecutionUnavailable


def inspect_host(service, turn_id):
    loop = getattr(service.runtime, 'proactive_loop', None)
    if loop is None or not service.runtime.config.proactive.enabled or not hasattr(getattr(loop, '_recovery_runtime', None), 'resume_event'):
        return None
    journal = service.sessions.execution_journal
    busy = bool(service._requests or service._pending_chat_writes or loop._reload_lock.locked()
                or loop._busy() or journal._conn.execute(
                    "SELECT 1 FROM executions WHERE status IN ('accepted','running') LIMIT 1").fetchone())
    boundary = boundary_facts(journal, turn_id)[2]
    return {'environment': loop.recovery_environment(), 'busy': busy,
            'finalization': loop._recovery_runtime._state.event_finalization(boundary['delivery_id']) if boundary else None,
            'unknown_acknowledgements': loop._recovery_runtime._state.unknown_acknowledgements()}


def recovery_plan(service, turn_id):
    journal = service.sessions.execution_journal
    with journal._lock:
        return plan_recovery(journal, service.runtime.bus.transport, turn_id, inspect_host(service, turn_id))


async def continue_execution(service, payload, request_id, chat_admission):
    if payload.get('confirmed') is not True:
        raise ValueError('请先查看恢复计划，再明确确认恢复主动提醒')
    journal, queue = service.sessions.execution_journal, service.runtime.bus.transport
    source_id = str(payload.get('turn_id') or '')
    async with chat_admission():
        record, created = claim_recovery(journal, queue, source_id, payload.get('plan_digest'),
                                         payload.get('action_id'), lambda: inspect_host(service, source_id))
        if not created:
            return {'execution': record, 'created': False}
        loop = service.runtime.proactive_loop
        key = record['session_key']
        scope = ExecutionScope(journal, record['turn_id'])
        scope.proactive = True
        service._requests[key] = request_id
        service._chat_tasks[key] = asyncio.current_task()
        token = current_execution.set(scope)
        try:
            journal.start(scope.turn_id)
            if record['input'].get('recovery_action') == 'settle_only':
                journal.finish(scope.turn_id, 'completed', {'recovered_from': source_id, 'local_settlement': True})
                return {'execution': journal.get(scope.turn_id), 'created': True}
            _, _, boundary, _, response = boundary_facts(journal, source_id)
            if boundary is None:
                raise ExecutionConflict('缺少主动恢复边界')
            # Capture the inherited value before any await, including crashes
            # after claim. The next plan can trace the source without re-calling.
            boundary = {**boundary, 'response': response}
            observe('recovery.boundary', **boundary)
            job = delivery_job(queue, boundary['delivery_id'])
            scope.proactive_receipts_only = delivery_evidence(queue, job)[1]
            scope.proactive_authorized_owners = {scope.turn_id, source_id}
            ancestor = journal.get(source_id)
            while ancestor.get('continuation'):
                ancestor = journal.get(ancestor['continuation']['source_turn_id'])
                scope.proactive_authorized_owners.add(ancestor['turn_id'])
            async with loop._reload_lock:
                loop._tick_last_user_at = loop._presence.get_last_user_at(key) if loop._presence else None
                async def guard():
                    if scope.proactive_receipts_only:
                        return True  # Outbox refuses every part without a receipt.
                    if loop.recovery_environment() != boundary['environment'] or not loop.can_send() or not service.runtime.config.proactive.enabled:
                        return False
                    job = delivery_job(queue, boundary['delivery_id'])
                    if job and job['review_closed']:
                        return False
                    valid = await loop._recovery_runtime.validate_recovery(boundary)
                    job = delivery_job(queue, boundary['delivery_id'])
                    return (valid and loop.recovery_environment() == boundary['environment']
                            and loop.can_send() and service.runtime.config.proactive.enabled
                            and not (job and job['review_closed']))
                scope.proactive_host_guard = guard
                if not await guard():
                    observe('recovery.cancelled', reason='eligibility_changed')
                    journal.finish(scope.turn_id, 'completed', {'cancelled': True, 'reason': 'eligibility_changed'})
                else:
                    from agent.core.proactive_turn.gates import ProactiveGateChain, ProactiveGateContext, ProactiveGateCompletion
                    from core.common.timekit import utcnow
                    manager = getattr(service.runtime, 'plugin_manager', None)
                    gates = ProactiveGateChain(manager.proactive_gates if manager else [])
                    gate = gates.evaluate(ProactiveGateContext(tick_id=scope.turn_id, session_key=key, now_utc=utcnow(),
                        target_transports=((loop._cfg.default_channel, loop._cfg.default_chat_id),)))
                    if gate.blocked and not scope.proactive_receipts_only:
                        observe('recovery.cancelled', reason=gate.reason)
                        journal.finish(scope.turn_id, 'completed', {'cancelled': True, 'reason': gate.reason})
                    else:
                        loop.delivered = False
                        await loop._recovery_runtime.resume_event(boundary, response)
                        if gate.activation and not scope.broken and not scope.review_required:
                            gates.finalize(ProactiveGateCompletion(activation=gate.activation, session_key=key,
                                occurred_at=utcnow(), outcome='delivered' if loop.delivered else 'closed', reason='recovery'))
                        journal.finish(scope.turn_id, 'failed' if scope.failed else 'completed', {'recovered_from': source_id})
            return {'execution': journal.get(scope.turn_id), 'created': True}
        except BaseException as exc:
            if not scope.broken and journal.get(scope.turn_id)['status'] in {'accepted', 'running'}:
                journal.finish(scope.turn_id, 'interrupted' if isinstance(exc, asyncio.CancelledError) else 'failed')
            if isinstance(exc, Exception):
                raise ExecutionUnavailable('主动恢复已接纳但未完成，请查看关联记录', journal.get(scope.turn_id)) from exc
            raise
        finally:
            scope.active = False
            current_execution.reset(token)
            service._requests.pop(key, None)
            service._chat_tasks.pop(key, None)
