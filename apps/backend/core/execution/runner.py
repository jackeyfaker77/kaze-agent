"""Durable admission around the existing local ordinary-turn execution path."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from uuid import uuid4

from bus.events import InboundMessage, OutboundMessage
from .contracts import ExecutionUnavailable, JournalWriteError
from .journal import ExecutionJournal
from .scope import ExecutionScope, current_execution


async def run_recorded(journal: ExecutionJournal, item: InboundMessage, session_key: str, operation):
    # Explicit stable execution identity is distinct from the transport/RPC id.
    # Legacy callers that reuse RPC ids keep their existing semantics.
    request_key = item.metadata.get("execution_request_id")
    if request_key is not None and (not isinstance(request_key, str) or not request_key.strip() or len(request_key) > 200):
        raise ValueError("execution_request_id 必须是 1–200 字符的非空字符串")
    request_key = request_key or uuid4().hex
    metadata = {k: v for k, v in item.metadata.items() if k not in {"request_id", "execution_request_id"}}
    record, created = journal.admit(session_key, item.channel, request_key, {
        "content": item.content, "media": item.media, "sender": item.sender,
        "channel": item.channel, "chat_id": item.chat_id, "metadata": metadata,
    })
    if not created:
        if record["status"] == "completed" and record["result"] is not None:
            return OutboundMessage(**record["result"])
        raise ExecutionUnavailable(f"该请求已接纳，状态为 {record['status']}；请查询执行记录，不能自动重跑")
    turn_id = record["turn_id"]
    scope = ExecutionScope(journal, turn_id)
    token = current_execution.set(scope)
    try:
        journal.start(turn_id)
        result = await operation()
        if scope.broken:
            raise JournalWriteError("执行记录不完整，请核对执行状态")
        journal.finish(turn_id, "failed" if scope.failed else "completed", asdict(result))
        return result
    except BaseException as exc:
        # A broken journal must remain non-terminal until startup recovery can
        # inspect the committed prefix. Never paper over a failed durable boundary.
        if not scope.broken and journal.get(turn_id)["status"] in {"accepted", "running"}:
            journal.finish(turn_id, "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed")
        raise
    finally:
        scope.active = False
        current_execution.reset(token)
