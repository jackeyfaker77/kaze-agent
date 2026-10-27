"""Read-only proactive recovery planning. No model, tool or transport is invoked here."""
from __future__ import annotations

from dataclasses import asdict
import json
from hashlib import sha256
from uuid import uuid4

from .continuation import read_events
from .contracts import ExecutionConflict, ExecutionIdentity
from .model_context import digest
from .journal import encode
from .proactive import observe, proactive_scope


REASONS = {
    'boundary_missing': '这段历史没有完整的主动恢复边界，不能推测重跑。',
    'not_interrupted': '执行尚未中断，或已经完成。',
    'source_closed': '原执行或关联队列已结束处置。',
    'continuation_exists': '该边界已有恢复执行，请查看关联记录。',
    'environment_changed': '用户活动、主动配置、来源或打扰规则已变化。',
    'host_unavailable': '原主动运行时不可用，或有执行正在处理。',
    'unknown_model': '模型请求结果未完整提交，不能再次调用。',
    'unknown_effect': '存在未知工具或发送结果，不能继续。',
    'queue_unsettled': '队列正在处理、失败或待核对，不能继续。',
    'boundary_settled': '该提醒已完成；后续算法缺少独立安全边界。',
    'history_invalid': '恢复记录缺失或不一致。',
    'gate_contract_missing': '原插件门控持有激活状态，尚无对应的持久收尾协议。',
}


def delivery_job(queue, key):
    try:
        return queue.get(key) if queue is not None else None
    except KeyError:
        return None


def delivery_evidence(queue, job):
    if not job:
        return [], False, False
    parts = [dict(p) for p in queue.db.execute('SELECT part,state,result FROM parts WHERE job=? ORDER BY part', (job['id'],))]
    manifests = [json.loads(row[0]) for row in queue.db.execute('SELECT payload FROM part_manifests WHERE job=? ORDER BY scope', (job['id'],))]
    # wake.event produces one text delivery. Its Telegram renderer may split it
    # into several physical sends; a first-part receipt is NOT whole-job proof.
    payload = job['payload']
    text_only = bool(payload.get('message') and not any(payload.get(k) for k in ('image', 'images', 'file')))
    expected = set(manifests[0]) if text_only and len(manifests) == 1 else {'0:text'} if text_only and not manifests else set()
    actual = {part['part']: part['state'] for part in parts}
    known = bool(expected and set(actual).issubset(expected) and all(v in {'pending', 'done'} for v in actual.values()))
    complete = job['state'] == 'done' or bool(known and set(actual) == expected and all(v == 'done' for v in actual.values()))
    return parts, complete, known


def record_event_boundary(kind, event, ctx, request):
    scope = proactive_scope()
    if scope is None:
        return None
    capture = getattr(scope, 'proactive_environment', None)
    boundary = {'version': 1, 'adapter': 'wake.event', 'boundary_id': uuid4().hex,
                'kind': kind, 'event': event, 'request': request,
                'wake_id': ctx.wake_id, 'now_utc': ctx.now_utc.isoformat(),
                'session_key': ctx.session_key,
                'gate_activation': bool(getattr(scope, 'proactive_gate_activation', False)),
                'environment': capture() if capture else None}
    boundary['delivery_id'] = 'proactive-recovery:' + boundary['boundary_id']
    observe('recovery.boundary', **boundary)
    return boundary


def boundary_facts(journal, turn_id):
    state = journal._public_state(turn_id)
    events = read_events(journal, turn_id)
    boundaries = [e for e in events if e['kind'] == 'proactive.observed'
                  and e['payload']['stage'] == 'recovery.boundary']
    if not boundaries:
        if state.get('continuation') and state['source'] == 'proactive':
            _, _, boundary, _, response = boundary_facts(journal, state['continuation']['source_turn_id'])
            if boundary:
                return state, events, {**boundary, 'response': response}, [], response
        return state, events, None, [], None
    event = boundaries[-1]
    boundary = event['payload']['detail']
    suffix = [e for e in events if e['seq'] > event['seq']]
    # A recovered attempt carries the committed response as an explicit reference
    # and frozen value, never as a new model response from the provider.
    response = boundary.get('response')
    calls = [e for e in suffix if e['kind'] == 'proactive.model.requested']
    if calls:
        returned = next((e['payload'] for e in suffix if e['kind'] == 'proactive.model.returned'
                         and e['payload']['call_id'] == calls[-1]['payload']['call_id']), None)
        if returned and not returned.get('error_type'):
            response = returned.get('response')
            if returned.get('model_call_id'):
                response = next((e['payload'].get('response') for e in suffix
                    if e['kind'] == 'model.response.recorded'
                    and e['payload']['call_id'] == returned['model_call_id']), None)
        elif returned is None and len(calls) == 1:
            prepared = [e for e in suffix if e['kind'] == 'model.request.prepared']
            responses = [e for e in suffix if e['kind'] == 'model.response.recorded' and e['payload'].get('response') is not None]
            if (len(prepared) == len(responses) == 1
                    and prepared[0]['payload']['call_id'] == responses[0]['payload']['call_id']
                    and calls[0]['payload']['input_digest'] == sha256(encode(boundary['request']).encode()).hexdigest()):
                response = responses[0]['payload']['response']
    return state, events, boundary, suffix, response


def plan_recovery(journal, queue, turn_id, observation):
    state, events, boundary, suffix, response = boundary_facts(journal, turn_id)
    reasons = []
    if state['source'] != 'proactive' or not boundary or boundary.get('version') != 1 or boundary.get('adapter') != 'wake.event':
        reasons.append('boundary_missing')
    if state['status'] not in {'failed', 'interrupted', 'needs_review'}:
        reasons.append('not_interrupted')
    if state['review'] or state['retry_turn_id']:
        reasons.append('source_closed')
    if boundary and boundary.get('gate_activation'):
        reasons.append('gate_contract_missing')
    if state.get('continuation_turn_id'):
        reasons.append('continuation_exists')
    if not observation or observation.get('busy'):
        reasons.append('host_unavailable')
    if observation and observation['environment']['session_key'] != state['session_key']:
        reasons.append('host_unavailable')
    if boundary and (not boundary.get('environment') or boundary['environment'] != (observation or {}).get('environment')):
        reasons.append('environment_changed')
    if boundary:
        event = boundary['event']
        ack = {'source_id': str(event.get('_reservoir_ack_source_id') or ''),
               'source_event_id': str(event.get('_reservoir_source_event_id') or '')}
        if ack in (observation or {}).get('unknown_acknowledgements', []):
            reasons.append('unknown_effect')
    models = state.get('proactive', {}).get('models', {})
    bounded_calls = [e['payload']['call_id'] for e in suffix if e['kind'] == 'proactive.model.requested']
    if any((c['status'] != 'returned' and not (response is not None and bounded_calls == [key]))
           or c.get('outcome', {}).get('error_type') for key, c in models.items()):
        reasons.append('unknown_model')
    if any(c['status'] != 'returned' or c.get('error_type') for c in state.get('model_calls', {}).values()):
        reasons.append('unknown_model')
    calls = [e for e in suffix if e['kind'] == 'proactive.model.requested']
    if len(calls) > 1 or (calls and response is None):
        reasons.append('unknown_model')
    settled = [e for e in suffix if e['kind'] == 'proactive.observed' and e['payload']['stage'] == 'recovery.settled']
    settle_only = state['input'].get('recovery_action') == 'settle_only'
    if settled:
        trailing = [e for e in suffix if e['seq'] > settled[-1]['seq']]
        settle_only = all(e['kind'] in {'run.recovered', 'run.ended', 'run.error', 'proactive.stage.completed'} for e in trailing)
        if not settle_only:
            reasons.append('boundary_settled')
    job = delivery_job(queue, boundary['delivery_id']) if boundary else None
    parts, receipts_only, known_parts = delivery_evidence(queue, job)
    if (receipts_only or settle_only) and 'environment_changed' in reasons:
        reasons.remove('environment_changed')
    if job and (job['review_closed'] or job['state'] in {'needs_review', 'failed'} or job['id'] in queue.active):
        reasons.append('source_closed' if job['review_closed'] else 'queue_unsettled')
    if any(p['state'] == 'sending' for p in parts):
        reasons.append('unknown_effect')
    if job and any(json.loads(r[0]).get('absent_parts') for r in queue.db.execute(
            'SELECT request FROM transport_reviews WHERE job=?', (job['id'],))):
        # An operator statement is not a physical rejection receipt. Legacy
        # queue reclassification cannot grant continuation of an unknown send.
        reasons.append('unknown_effect')
    # Queue receipts are the only authority for sends. A missing execution
    # delivery.returned can be repaired by the exact persisted part receipt.
    for effect in state['effects']:
        if effect['certainty'] == 'confirmed' or effect.get('retry_safe'):
            continue
        finalization = (observation or {}).get('finalization') or {}
        if (boundary and effect['kind'] == 'tool' and effect.get('name') == 'AsyncEffect'
                and effect.get('arguments', {}).get('recovery_id') == boundary['delivery_id']
                and finalization.get('delivery_id') == boundary['delivery_id']
                and all(ack['state'] == 'pending' for ack in finalization.get('pending', []))):
            continue
        if (effect['kind'] == 'delivery' and job and known_parts
                and effect.get('channel') == job['payload'].get('channel')
                and effect.get('chat_id') == job['payload'].get('chat_id')
                and effect.get('arguments') == [job['payload'].get('chat_id'), job['payload'].get('message')]):
            continue
        reasons.append('unknown_effect')
    reasons = list(dict.fromkeys(reasons))
    plan = {'version': 1, 'kind': 'proactive', 'turn_id': turn_id, 'session_key': state['session_key'],
            'source_seq': state['last_seq'], 'source_digest': digest(events),
            'allowed': not reasons, 'reasons': reasons, 'explanations': [REASONS[r] for r in reasons],
            'continuation_turn_id': state.get('continuation_turn_id'),
            'completed_model_calls': sum(c['status'] == 'returned' and not c.get('error_type') for c in models.values()),
            'completed_tools': sum(e['kind'] == 'tool' and e['certainty'] == 'confirmed' for e in state['effects']),
            'completed_deliveries': sum(p['state'] == 'done' for p in parts),
            'action': 'settle_only' if settle_only else 'receipts_only' if receipts_only else 'reuse_decision' if response is not None else 'judge',
            'observation': observation, 'queue': job, 'parts': parts}
    plan['plan_digest'] = digest(plan)
    return plan


def claim_recovery(journal, queue, turn_id, plan_digest, action_id, inspect):
    if not isinstance(action_id, str) or not action_id.strip() or len(action_id) > 200:
        raise ValueError('Invalid recovery action identity')
    with journal._transaction():
        old = journal._conn.execute('SELECT * FROM continuation_claims WHERE source_turn_id=?', (turn_id,)).fetchone()
        if old:
            if old['plan_digest'] != plan_digest:
                raise ExecutionConflict('恢复计划已失效，请查看关联执行')
            return journal._public_state(old['target_turn_id']), False
        plan = plan_recovery(journal, queue, turn_id, inspect())
        if not plan['allowed'] or plan['plan_digest'] != plan_digest:
            raise ExecutionConflict('恢复条件已变化，请重新查看计划：' + '；'.join(plan['explanations']))
        identity = asdict(ExecutionIdentity(journal.root_id, journal.epoch, plan['session_key'], 'proactive',
                                           'recover:' + turn_id, uuid4().hex, uuid4().hex))
        journal._event(identity, 'request.accepted', {'identity': identity, 'input': {
            'proactive_recovery_of': turn_id, 'recovery_action': plan['action']}})
        journal._event(identity, 'continuation.started', {'version': 1, 'source_turn_id': turn_id,
            'source_seq': plan['source_seq'], 'source_digest': plan['source_digest'],
            'history_digest': plan['source_digest'], 'plan_digest': plan_digest, 'action_id': action_id})
        return journal._public_state(identity['turn_id']), True
