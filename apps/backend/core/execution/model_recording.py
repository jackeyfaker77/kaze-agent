"""Production model boundary: durable facts -> pure projection -> transport.

Reconstruction is deliberately elsewhere and has no path back into this module.
Physical network retries retain one logical call and one immutable input prefix.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import asdict, dataclass
from functools import wraps
from uuid import uuid4

from .contracts import ExecutionNeedsReview, JournalWriteError
from .model_context import digest, materialize, observe_request
from .scope import ExecutionScope, active_execution, current_execution, current_model_tool


@dataclass
class ModelCall:
    scope: ExecutionScope | None
    call_id: str
    projection: dict | None = None
    attempts: int = 0
    returned: bool = False
    history_visible: bool = False


current_model_call: ContextVar[ModelCall | None] = ContextVar('current_model_call', default=None)
history_lane: ContextVar[bool] = ContextVar('model_history_lane', default=False)


async def history_chat(provider, **kwargs):
    """Interaction steps are distinct from routing, memory and other model work."""
    token = history_lane.set(True)
    try:
        return await provider.chat(**kwargs)
    finally:
        history_lane.reset(token)


def _check(scope):
    if scope is not None:
        if scope.broken or not scope.active:
            raise JournalWriteError('模型交互事实未完整保存，已停止后续操作')
        if scope.review_required:
            raise ExecutionNeedsReview('工具结果未知，不能继续请求模型或重跑工具')


def projected_request(request: dict, transport: str) -> dict:
    """The *only* request passed to either real transport is this projection."""
    call = current_model_call.get()
    scope = call.scope if call else active_execution()
    _check(scope)
    if call is not None and call.projection is not None:
        if call.projection['request_digest'] != digest(request):
            raise ValueError('A network retry cannot change the logical model request')
        if scope is not None:
            return scope.journal.model_context(scope.turn_id, call_id=call.call_id,
                through_seq=call.projection['through_seq'],
                expected_digest=call.projection['prefix_digest'])['request']
        return materialize(*_ephemeral(request, transport))
    if scope is not None:
        if call is None:
            raise JournalWriteError('Model transport is missing its logical invocation boundary')
        try:
            call.projection = scope.journal.prepare_model_request(scope.turn_id, call.call_id, request, transport,
                                                                  history_visible=call.history_visible)
        except Exception as exc:
            scope.broken = True
            raise JournalWriteError('模型上下文事实提交或投影失败，未调用模型') from exc
        return call.projection['request']
    # Unbound providers are used by library/unit-test callers. Runtime bootstrap
    # binds every provider to its journal, including background memory calls.
    components, manifest = _ephemeral(request, transport)
    projected = materialize(components, manifest)
    if call:
        call.projection = {'request_digest': digest(projected)}
    return projected


def _ephemeral(request, transport):
    components, manifest = observe_request(request, transport)
    return {c['id']: c for c in components}, manifest


def model_attempt():
    call = current_model_call.get()
    if call is not None:
        _check(call.scope)
        call.attempts += 1
        if call.scope is not None:
            call.scope.record('model.attempt.started', {'call_id': call.call_id, 'attempt': call.attempts})


def recorded_model_call(operation):
    @wraps(operation)
    async def invoke(self, *args, **kwargs):
        scope = active_execution()
        owner_token = None
        journal = getattr(self, 'execution_journal', None)
        if scope is None and journal is not None:
            key = uuid4().hex
            row, _ = journal.admit('model:' + key, 'model', key, {'origin': 'background_model_call'})
            journal.start(row['turn_id'])
            scope = ExecutionScope(journal, row['turn_id'])
            owner_token = current_execution.set(scope)
        _check(scope)
        call = ModelCall(scope, uuid4().hex, history_visible=history_lane.get())
        lane_token = history_lane.set(False)
        token = current_model_call.set(call)
        try:
            response = await operation(self, *args, **kwargs)
            if scope is not None:
                if call.projection is None:
                    scope.broken = True
                    raise JournalWriteError('模型请求绕过了上下文投影器')
                scope.record('model.response.recorded', {'call_id': call.call_id, 'response': asdict(response)})
                call.returned = True
                # Downstream consumers receive the committed semantic response.
                saved = scope.journal.model_response(scope.turn_id, call.call_id)
                from agent.provider import LLMResponse, ToolCall
                saved['tool_calls'] = [ToolCall(**c) for c in saved.get('tool_calls', [])]
                response = LLMResponse(**saved)
            if owner_token is not None:
                journal.finish(scope.turn_id, 'completed')
            return response
        except BaseException as exc:
            if scope is not None and not scope.broken:
                if call.projection is not None and not call.returned:
                    scope.record('model.response.recorded', {'call_id': call.call_id, 'error_type': type(exc).__name__})
                if owner_token is not None:
                    journal.finish(scope.turn_id, 'failed')
            raise
        finally:
            current_model_call.reset(token)
            history_lane.reset(lane_token)
            if owner_token is not None:
                scope.active = False
                current_execution.reset(owner_token)
    return invoke


def _tool_owner(scope, tool_call_id):
    calls = scope.journal.get(scope.turn_id).get('model_calls', {})
    completed = [c for c in calls.values() if c['status'] == 'returned' and not c.get('error_type')]
    return next((c for c in sorted(completed, key=lambda c: c['response_seq'], reverse=True)
                 if tool_call_id not in c.get('tool_results', {}) and tool_call_id in c.get('tool_call_ids', [])), None)


async def invoke_model_tool(tool_call_id, invoker, *args):
    scope = active_execution()
    owner = _tool_owner(scope, tool_call_id) if scope is not None else None
    token = current_model_tool.set((scope, owner['call_id'], tool_call_id) if owner else None)
    try:
        return await invoker(*args)
    finally:
        current_model_tool.reset(token)


def record_tool_context(messages: list[dict], *, tool_call_id: str):
    """Model-visible tool result, distinct from a dispatch/physical effect receipt.

Denied/synthetic results also use this boundary. Unknown effects still throw in
ExecutionScope.effect; they never acquire a manufactured successful result here.
"""
    scope = active_execution()
    if scope is None:
        return messages
    _check(scope)
    state = scope.journal.get(scope.turn_id)
    calls = state.get('model_calls', {})
    completed = [c for c in calls.values() if c['status'] == 'returned' and not c.get('error_type')]
    if not completed:
        # Legacy/custom providers do not have new model facts. Do not invent IDs.
        return messages
    owner = _tool_owner(scope, tool_call_id)
    if owner is None:
        raise ValueError('Tool result is not owned by the current model step')
    call_id = owner['call_id']
    key = uuid4().hex
    scope.record('interaction.tool_result', {'result_id': key, 'call_id': call_id,
        'tool_call_id': tool_call_id, 'messages': messages,
        'effect_ids': [e['effect_id'] for e in state['effects']
                       if e.get('model_call_id') == call_id and e.get('tool_call_id') == tool_call_id]})
    return scope.journal.tool_context(scope.turn_id, key)
