"""Durable admission around the existing local ordinary-turn execution path."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from uuid import uuid4

from bus.events import InboundMessage, OutboundMessage
from .contracts import ExecutionUnavailable, ExecutionNeedsReview, JournalWriteError
from .journal import ExecutionJournal
from .scope import ExecutionScope, current_execution


def spawn_stop_notice(item, session_key, turn_id, detail):
    return OutboundMessage(channel=item.channel, chat_id=item.chat_id,
        content=detail,
        metadata={**item.metadata, 'session_key_override': session_key,
                  'execution_notice': turn_id, 'spawn_completion_notice': True})


async def run_spawn_recorded(journal, item, session_key, operation):
    """Consume a claimed durable result once; replay only a committed reply.

    An admitted-only attempt can be reclaimed. Once started, crash recovery never
    infers that plugins/model/tools were harmless or repeats them automatically.
    """
    record, created = journal.admit_spawn_consumer(
        item.event.job_id, item.metadata['_spawn_claim'], session_key)
    if not created:
        if record['status'] == 'completed' and record['result'] is not None:
            item.metadata['_spawn_handled'] = True
            item.metadata['_spawn_reply_needs_dispatch'] = True
            return OutboundMessage(**record['result'])
        if record['status'] in {'accepted', 'running'}:
            raise ExecutionUnavailable('子任务结果正在被父执行处理', record)
        item.metadata['_spawn_handled'] = True
        item.metadata['_spawn_reply_needs_dispatch'] = True
        closed = (journal.get(record['turn_id']).get('review') or {}).get('decision') == 'close'
        return spawn_stop_notice(item, session_key, record['turn_id'],
            '后台结果的后续处理已关闭，不再继续执行。' if closed else
            f"后台结果已接纳，但后续执行未完成。请核对执行 {record['turn_id']}，不会自动重跑。")
    scope = ExecutionScope(journal, record['turn_id'])
    token = current_execution.set(scope)
    from bus.queue import inbound_job, spawn_delivery_account
    delivery_token = inbound_job.set('spawn:' + item.event.job_id)
    account_token = spawn_delivery_account.set((item.channel, item.metadata.get('_spawn_account')))
    try:
        journal.start(scope.turn_id)
        if record['spawn_cancelled']:
            item.metadata['_spawn_reply_needs_dispatch'] = True
            result = spawn_stop_notice(item, session_key, scope.turn_id, '后台任务的后续处理已取消，已完成结果保留在执行记录中。')
        else:
            result = await operation()
        if item.metadata.get('_spawn_account'):
            result.metadata['_spawn_account'] = item.metadata['_spawn_account']
        if scope.broken:
            raise JournalWriteError('父执行记录不完整，请核对执行状态')
        if scope.review_required or journal._unresolved(scope.turn_id):
            raise ExecutionNeedsReview('父执行存在未知外部结果，请核对执行记录')
        journal.finish(scope.turn_id, 'failed' if scope.failed else 'completed', asdict(result))
        item.metadata['_spawn_handled'] = True
        return result
    except BaseException as exc:
        if not scope.broken and journal.get(scope.turn_id)['status'] in {'accepted', 'running'}:
            journal.finish(scope.turn_id, 'interrupted' if isinstance(exc, asyncio.CancelledError) else 'failed')
        raise
    finally:
        scope.active = False
        current_execution.reset(token)
        inbound_job.reset(delivery_token)
        spawn_delivery_account.reset(account_token)


async def run_recorded(journal: ExecutionJournal, item: InboundMessage, session_key: str, operation):
    # Explicit stable execution identity is distinct from the transport/RPC id.
    # Legacy callers that reuse RPC ids keep their existing semantics.
    request_key = item.metadata.get("execution_request_id")
    if request_key is not None and (not isinstance(request_key, str) or not request_key.strip() or len(request_key) > 200):
        raise ValueError("execution_request_id 必须是 1–200 字符的非空字符串")
    request_key = request_key or uuid4().hex
    retry = item.metadata.get("_execution_retry")
    metadata = {k: v for k, v in item.metadata.items() if k not in {"request_id", "execution_request_id", "_execution_retry"}}
    original_input = {
        "content": item.content, "media": item.media, "sender": item.sender,
        "channel": item.channel, "chat_id": item.chat_id, "metadata": metadata,
    }
    if retry is not None:
        original = journal.get(retry["turn_id"])
        if original["session_key"] != session_key or original["input"] != original_input:
            raise ExecutionUnavailable("重试输入与原执行不一致")
        record, created = journal.admit_retry(**retry)
    else:
        record, created = journal.admit(session_key, item.channel, request_key, original_input)
    if not created:
        if record["status"] == "completed" and record["result"] is not None:
            return OutboundMessage(**record["result"])
        error = ExecutionNeedsReview if record["status"] == "needs_review" else ExecutionUnavailable
        raise error(f"该请求已接纳，状态为 {record['status']}；请在工作台查看执行记录并处置", record)
    turn_id = record["turn_id"]
    scope = ExecutionScope(journal, turn_id)
    token = current_execution.set(scope)
    try:
        journal.start(turn_id)
        result = await operation()
        if scope.broken:
            raise JournalWriteError("执行记录不完整，请核对执行状态")
        if scope.review_required or journal._unresolved(turn_id):
            raise ExecutionNeedsReview("外部操作结果未知，请在工作台核对执行记录")
        journal.finish(turn_id, "failed" if scope.failed else "completed", asdict(result))
        return result
    except BaseException as exc:
        # A broken journal must remain non-terminal until startup recovery can
        # inspect the committed prefix. Never paper over a failed durable boundary.
        if not scope.broken and journal.get(turn_id)["status"] in {"accepted", "running"}:
            journal.finish(turn_id, "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed")
        if not scope.broken and isinstance(exc, Exception):
            final = journal.get(turn_id)
            if final["status"] == "needs_review":
                raise ExecutionNeedsReview("外部操作结果未知，请在工作台核对执行记录后处置；请勿直接重发", final) from exc
            if any(not effect["retry_safe"] for effect in final["effects"]):
                raise ExecutionUnavailable("本次执行已有外部操作，不能整轮重发；请在工作台核对执行记录并处置", final) from exc
        raise
    finally:
        scope.active = False
        current_execution.reset(token)
