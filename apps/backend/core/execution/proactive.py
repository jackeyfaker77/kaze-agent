"""Durable proactive attempts. Replay reads facts; it never resumes callbacks."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from hashlib import sha256
from uuid import uuid4

from .contracts import ExecutionNeedsReview, ExecutionUnavailable, JournalWriteError
from .journal import encode
from .scope import ExecutionScope, active_execution, current_execution, fact


def proactive_scope():
    scope = active_execution()
    return scope if scope is not None and getattr(scope, "proactive", False) else None


def check_proactive():
    scope = proactive_scope()
    if scope is not None:
        if scope.broken or not scope.active:
            raise JournalWriteError("主动执行事实未完整保存，已停止后续操作")
        if scope.review_required:
            raise ExecutionNeedsReview("主动执行存在未知外部结果，请在工作台核对")


def observe(stage: str, **detail):
    check_proactive()
    scope = proactive_scope()
    if scope is not None:
        scope.record("proactive.observed", {"stage": stage, "detail": detail})


def candidate_identities(kind: str, candidates: list[dict]) -> list[str]:
    identities = []
    for item in candidates:
        source = item.get('ack_server') or item.get('_source') or item.get('source') or ''
        key = item.get('event_id') or item.get('id') or item.get('item_id')
        # Context can legitimately change under one source ID. Use its observed value.
        if kind == 'context' or not key:
            key = sha256(encode(item).encode()).hexdigest()
        identities.append(encode([kind, source, key]))
    return identities


def available_candidates(kind: str, candidates: list[dict]) -> list[dict]:
    check_proactive()
    scope = proactive_scope()
    if scope is None or not candidates:
        return candidates
    row = scope.journal.get(scope.turn_id)
    flags = scope.journal.available_proactive_candidates(row['session_key'], candidate_identities(kind, candidates))
    observe('candidates.filtered', kind=kind, excluded=sum(not flag for flag in flags))
    return [item for item, available in zip(candidates, flags) if available]


def claim_candidates(kind: str, candidates: list[dict]) -> bool:
    check_proactive()
    scope = proactive_scope()
    if scope is None or not candidates:
        return True
    identities = candidate_identities(kind, candidates)
    try:
        claimed = scope.journal.claim_proactive(scope.turn_id, identities)
    except Exception as exc:
        scope.broken = True
        raise JournalWriteError('主动候选接纳失败，已停止模型调用') from exc
    observe('candidates', claimed=claimed, identities=identities)
    return claimed


def defer_before_delivery():
    """A new user message can defer an unsent reminder, without consuming it."""
    check_proactive()
    scope = proactive_scope()
    if scope is None:
        return
    state = scope.journal.get(scope.turn_id)
    proactive = state.get('proactive', {})
    # Conservatively keep claims if this attempt has already handed off any send.
    if any(e['kind'] == 'delivery' for e in state['effects']) or any(
        o['stage'] in {'queue.enqueued', 'queue.linked'} for o in proactive.get('observations', [])
    ):
        return
    candidates = sorted(set(proactive.get('candidates', [])) - set(proactive.get('released_candidates', [])))
    if candidates:
        scope.record('proactive.candidates.released', {
            'candidates': candidates, 'reason': 'user_activity_before_delivery',
        })


@contextmanager
def stage(name: str):
    check_proactive()
    scope = proactive_scope()
    key = uuid4().hex
    if scope is not None:
        scope.record("proactive.stage.started", {"stage_id": key, "name": name})
    yield
    check_proactive()
    if scope is not None:
        scope.record("proactive.stage.completed", {"stage_id": key})


class RecordedProactiveProvider:
    """Record each logical model request before invoking the existing provider."""
    def __init__(self, provider):
        self.provider = provider

    def __getattr__(self, name):
        return getattr(self.provider, name)

    async def chat(self, **kwargs):
        check_proactive()
        scope = proactive_scope()
        if scope is None:
            return await self.provider.chat(**kwargs)
        key = uuid4().hex
        # This digest is an audit reference, not a resumable model-context snapshot.
        scope.record("proactive.model.requested", {
            "call_id": key, "model": kwargs.get("model"),
            "input_digest": sha256(encode({k: v for k, v in kwargs.items() if k != 'on_content_delta'}).encode()).hexdigest(),
        })
        before = set(scope.journal.get(scope.turn_id).get('model_calls', {}))
        try:
            response = await self.provider.chat(**kwargs)
        except BaseException as exc:
            scope.record("proactive.model.returned", {"call_id": key, "error_type": type(exc).__name__})
            raise
        recorded = set(scope.journal.get(scope.turn_id).get('model_calls', {})) - before
        outcome = {'model_call_id': next(iter(recorded))} if len(recorded) == 1 else {'response': fact(response)}
        scope.record("proactive.model.returned", {"call_id": key, **outcome})
        return response


async def run_proactive_recorded(journal, session_key, request_key, payload, operation):
    record, created = journal.admit(session_key, "proactive", request_key, payload)
    if not created:
        if record["status"] == "completed":
            return (record["result"] or {}).get("base_score")
        raise ExecutionUnavailable("主动执行已接纳；请查看记录，不自动重跑模型或发送", record)
    scope = ExecutionScope(journal, record["turn_id"])
    scope.proactive = True
    token = current_execution.set(scope)
    try:
        journal.start(scope.turn_id)
        score = await operation()
        check_proactive()
        journal.finish(scope.turn_id, "failed" if scope.failed else "completed", {"base_score": score})
        return score
    except BaseException as exc:
        if not scope.broken and journal.get(scope.turn_id)["status"] in {"accepted", "running"}:
            scope.error(exc)
            journal.finish(scope.turn_id, "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed")
        raise
    finally:
        scope.active = False
        current_execution.reset(token)


def frame_summary(frame):
    """Small semantic read model shared by the default and wake lifecycles."""
    state = frame.slots.get("wake:run_state") or frame.slots.get("run:state")
    if state is None:
        return {}
    ctx = state.ctx
    gateway = getattr(state, "gateway", None)
    return {
        "tick_id": getattr(ctx, "wake_id", None) or getattr(ctx, "tick_id", None),
        "route": getattr(state, "route", ""),
        "decision": getattr(ctx, "terminal_action", None),
        "skip_reason": getattr(ctx, "skip_reason", ""),
        "cited_ids": list(getattr(ctx, "cited_item_ids", [])),
        "candidates": [
            {"id": item.get("id") or item.get("event_id"),
             "source": item.get("ack_server") or item.get("_source"), "kind": kind}
            for kind, items in (
                ("alert", getattr(state, "alerts", None) or getattr(gateway, "alerts", [])),
                ("content", getattr(state, "contents", None) or getattr(gateway, "content_meta", [])),
            ) for item in items
        ],
    }
