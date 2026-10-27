"""One fail-closed authority for read-only plans and atomic continuation claims.

An operator decision never repairs an unknown result. Only complete committed
boundaries can authorize a new execution. Projection performs no external work.
"""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from uuid import uuid4

from .contracts import ExecutionConflict, ExecutionIdentity
from .model_context import digest, project_interaction_history, project_model_context, validate_tool_pairs
from .projection import project_event, ExecutionProjectionError


REASONS = {
    'not_interrupted': '仅失败或中断的执行可以生成续跑资格。',
    'source_closed': '原执行已结束处置或已有重试。',
    'unsupported_source': 'v1 仅支持桌面对话；渠道和主动执行仍由原队列管理。',
    'legacy_boundary': '缺少受控续跑边界事实，不能推测旧执行可安全继续。',
    'unknown_effect': '工具或发送结果未知；请另开明确的新一轮检查外部现状，不能续跑旧边界。',
    'incomplete_model': '模型调用没有完整已提交结果，不能重发历史请求。',
    'incomplete_tools': '模型工具调用与结果尚未完整配对。',
    'not_safe_boundary': '历史没有停在用户或完整工具结果边界。',
    'workspace_changed': '工作区身份或执行目录与原边界不一致。',
    'tools_changed': '历史工具已不可用，或工具目录在预览后发生变化。',
    'background_unsettled': '仍有后台、子任务或其他执行未结束。',
    'queue_unsettled': '关联收发任务未完成或结果未知，请先查看收发队列。',
    'session_changed': '原执行之后已有新的会话交互或会话已删除。',
    'continuation_exists': '该边界已创建续跑，请查看关联新执行。',
    'history_invalid': '事件前缀不完整或相互矛盾，拒绝续跑。',
    'safety_observation_unavailable': '无法取得完整的运行状态，请检查服务后重新生成计划。',
}


def read_events(journal, turn_id):
    return [{**dict(r), 'payload': json.loads(r['payload'])} for r in journal._conn.execute(
        'SELECT * FROM events WHERE turn_id=? ORDER BY seq', (turn_id,))]


def interaction_segments(journal, turn_id, visited=None):
    visited = set(visited or ())
    if turn_id in visited or len(visited) >= 64:
        raise ValueError('Invalid continuation ancestry')
    visited.add(turn_id)
    events = read_events(journal, turn_id)
    lineage = next((e['payload'] for e in events if e['kind'] == 'continuation.started'), None)
    segments = []
    if lineage:
        prefix = read_events(journal, lineage['source_turn_id'])
        if not prefix or prefix[-1]['seq'] != lineage['source_seq'] or digest(prefix) != lineage['source_digest']:
            raise ValueError('Invalid continuation source prefix')
        segments = interaction_segments(journal, lineage['source_turn_id'], visited)
    return segments + [project_interaction_history(events, journal.root_id)]


def resolve_boundary(journal, turn_id, visited=None):
    """Revalidate each immutable ancestor, not cached UI or execution views."""
    visited = set(visited or ())
    if turn_id in visited or len(visited) >= 64:
        raise ValueError('Invalid continuation ancestry')
    visited.add(turn_id)
    events = read_events(journal, turn_id)
    state = None
    for event in events:
        state = project_event(state, event, journal.root_id)
    if state is None:
        raise ValueError('Missing execution')
    for event in events:
        if event['kind'] == 'model.request.prepared':
            project_model_context(events, root_id=journal.root_id, turn_id=turn_id,
                                  call_id=event['payload']['call_id'], through_seq=event['seq'])
    boundary = next((e['payload'] for e in events if e['kind'] == 'continuation.boundary'), None)
    lineage = state.get('continuation')
    inherited_effects = []
    if lineage:
        parent, prefix, parent_boundary, history, inherited_effects = resolve_boundary(journal, lineage['source_turn_id'], visited)
        if (parent['last_seq'] != lineage['source_seq'] or digest(prefix) != lineage['source_digest']
                or digest(history) != lineage['history_digest']):
            raise ValueError('Source continuation prefix changed')
        # A claim committed immediately before process death is already a durable
        # opening boundary. Reconstruct it from its verified source, never run it
        # automatically and never spend the original claim twice.
        if boundary is None:
            boundary = parent_boundary
    else:
        if boundary is None:
            return state, events, None, [], []
        history = boundary['history']
    semantic = project_interaction_history(events, journal.root_id)
    state['_semantic_diagnostics'] = semantic['diagnostics']
    return state, events, boundary, history + semantic['messages'], inherited_effects + list(state['effects'].values())


def plan_continuation(journal, turn_id, observation):
    """Caller supplies Host-owned observations, never UI assertions."""
    reasons = []
    try:
        state, events, boundary, history, effects = resolve_boundary(journal, turn_id)
    except (ValueError, KeyError, TypeError, ExecutionProjectionError):
        return {'version': 1, 'turn_id': turn_id, 'allowed': False, 'reasons': ['history_invalid'],
                'explanations': [REASONS['history_invalid']], 'plan_digest': ''}
    claimed = journal._conn.execute('SELECT target_turn_id FROM continuation_claims WHERE source_turn_id=?', (turn_id,)).fetchone()
    if claimed:
        reasons.append('continuation_exists')
    if state['status'] not in {'interrupted', 'failed', 'needs_review'}:
        reasons.append('not_interrupted')
    if state['review'] or state['retry_turn_id']:
        reasons.append('source_closed')
    if state['source'] != 'desktop' or state['input'].get('channel') != 'desktop':
        reasons.append('unsupported_source')
    if boundary is None:
        reasons.append('legacy_boundary')
    else:
        if boundary['workspace'] != observation['workspace'] or boundary['cwd'] != observation['cwd']:
            reasons.append('workspace_changed')
        if any(c['status'] != 'returned' or c.get('error_type') for c in state.get('model_calls', {}).values()):
            reasons.append('incomplete_model')
        if state['_semantic_diagnostics']:
            reasons.append('incomplete_tools')
        if any(e['certainty'] != 'confirmed' for e in effects):
            reasons.append('unknown_effect')
        required = {e['name'] for e in effects if e['kind'] == 'tool'}
        if any('model_arguments' not in e for e in effects if e['kind'] == 'tool'):
            reasons.append('legacy_boundary')
        if not required.issubset(observation['tools']):
            reasons.append('tools_changed')
        # Background tools need a tool-specific durable settlement contract;
        # a returned job id is not proof that its work finished.
        if required.intersection({'spawn', 'spawn_manage'}):
            reasons.append('background_unsettled')
        if not history or history[-1].get('role') not in {'user', 'tool'}:
            reasons.append('not_safe_boundary')
        try:
            validate_tool_pairs({'messages': history}, 'chat.completions.v1')
        except ValueError:
            reasons.append('incomplete_tools')
    reasons.extend(observation.get('blockers', []))
    reasons = list(dict.fromkeys(reasons))
    plan = {'version': 1, 'turn_id': turn_id, 'session_key': state['session_key'],
            'source_seq': state['last_seq'], 'source_digest': digest(events),
            'history_digest': digest(history), 'observation': observation,
            'allowed': not reasons, 'reasons': reasons, 'explanations': [REASONS[r] for r in reasons],
            'continuation_turn_id': claimed[0] if claimed else None,
            'completed_model_calls': sum(c['status'] == 'returned' and not c.get('error_type') for c in state.get('model_calls', {}).values()),
            'completed_tools': sum(e['kind'] == 'tool' and e['certainty'] == 'confirmed' for e in effects),
            'completed_deliveries': sum(e['kind'] == 'delivery' and e['certainty'] == 'confirmed' for e in effects)}
    plan['plan_digest'] = digest(plan)
    return plan


def claim_continuation(journal, turn_id, plan_digest, action_id, inspect):
    if not isinstance(action_id, str) or not action_id.strip() or len(action_id) > 200:
        raise ValueError('Invalid continuation action identity')
    with journal._transaction():
        old = journal._conn.execute('SELECT * FROM continuation_claims WHERE source_turn_id=?', (turn_id,)).fetchone()
        if old:
            if old['plan_digest'] != plan_digest:
                raise ExecutionConflict('续跑计划已失效，请查看已接纳的新执行')
            return journal._state(old['target_turn_id']), False
        plan = plan_continuation(journal, turn_id, inspect())
        if not plan['allowed'] or plan['plan_digest'] != plan_digest:
            raise ExecutionConflict('续跑条件已变化，请重新生成恢复计划：' + '；'.join(plan['explanations']))
        source = resolve_boundary(journal, turn_id)[0]
        identity = asdict(ExecutionIdentity(journal.root_id, journal.epoch, source['session_key'], 'desktop',
                                           'continue:' + turn_id, uuid4().hex, uuid4().hex))
        journal._event(identity, 'request.accepted', {'identity': identity,
            'input': {'channel': 'desktop', 'continuation_of': turn_id}})
        journal._event(identity, 'continuation.started', {'version': 1, 'source_turn_id': turn_id,
            'source_seq': plan['source_seq'], 'source_digest': plan['source_digest'],
            'history_digest': plan['history_digest'], 'plan_digest': plan_digest, 'action_id': action_id})
        return journal._state(identity['turn_id']), True


def record_boundary(scope, messages):
    if scope.journal.get(scope.turn_id).get('continuation_boundary'):
        return
    from os import getcwd
    lineage = scope.journal.get(scope.turn_id).get('continuation')
    payload = {'version': 1, 'workspace': str(scope.journal.directory.parent.parent.resolve()),
               'cwd': str(Path(getcwd()).resolve()),
               'history': [] if lineage else [m for m in messages if m.get('role') not in {'system', 'developer'}]}
    scope.record('continuation.boundary', payload)
