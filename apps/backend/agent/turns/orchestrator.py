from __future__ import annotations

import inspect
import logging
import asyncio
import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from uuid import uuid4
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agent.turns.outbound import OutboundDispatch, OutboundDispatchError, OutboundPort
from agent.turns.result import TurnResult
from bus.event_bus import EventBus
from bus.events_lifecycle import ProactiveMessageCommitted

if TYPE_CHECKING:
    from agent.core.runtime_support import SessionLike
    from agent.looping.ports import SessionServices

logger = logging.getLogger("agent.turn_orchestrator")


@dataclass
class TurnOrchestratorDeps:
    session: SessionServices
    outbound: OutboundPort
    event_bus: EventBus | None = None
    delivery_scope: Callable[[str, str], AbstractAsyncContextManager[None]] | None = (
        None
    )


class TurnOrchestrator:
    def __init__(self, deps: TurnOrchestratorDeps) -> None:
        self._session = deps.session
        self._outbound = deps.outbound
        self._event_bus = deps.event_bus
        self._delivery_scope = deps.delivery_scope

    async def handle_proactive_turn(
        self, *, result: TurnResult, session_key: str, channel: str, chat_id: str,
    ) -> bool:
        from core.execution.proactive import observe, stage, proactive_scope
        observe("resolve", decision=result.decision, evidence=list(result.evidence),
                content=result.outbound.content if result.outbound else None,
                media=list(result.outbound.media or []) if result.outbound else [],
                channel=channel, chat_id=chat_id,
                trace=result.trace.extra if result.trace else {})
        with stage("deliver"):
            sent = await self._handle_proactive_turn(result=result, session_key=session_key,
                                                    channel=channel, chat_id=chat_id)
            observe("deliver", sent=sent, decision=result.decision)
            scope = proactive_scope()
            if scope is not None and result.decision == "reply" and not sent:
                scope.failed = True
            return sent

    async def _handle_proactive_turn(
        self,
        *,
        result: TurnResult,
        session_key: str,
        channel: str,
        chat_id: str,
    ) -> bool:
        # 1. proactive 先处理 skip：不发消息，只跑 skip 路径副作用。
        if result.decision == "skip":
            await self._run_side_effects(result)
            return False

        if result.outbound is None:
            raise ValueError("proactive reply result requires outbound")

        content = result.outbound.content
        media = list(result.outbound.media or [])
        session = self._session.session_manager.get_or_create(session_key)
        source_metadata = self._build_source_metadata(
            session=session,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
        )
        if result.evidence:
            source_metadata['delivery_id'] = 'proactive:' + hashlib.sha256(json.dumps([channel, chat_id, session_key, sorted(result.evidence)]).encode()).hexdigest()
        else:
            source_metadata["delivery_id"] = uuid4().hex
        from core.execution.proactive import observe
        observe("delivery.intent", delivery_id=source_metadata["delivery_id"],
                channel=channel, chat_id=chat_id, evidence=list(result.evidence))
        # Persist only the data needed to reconstruct conversation history, never callbacks.
        source_metadata['_delivery_projection'] = json.dumps({
            'evidence': result.evidence,
            'trace_extra': result.trace.extra if result.trace else {},
        }, ensure_ascii=False)
        # 2. 先执行发送前 side_effects，再真正 dispatch 到 outbound。
        await self._run_effects(result.side_effects)
        scope = (
            self._delivery_scope(channel, chat_id)
            if self._delivery_scope
            else nullcontext()
        )
        async with scope:
            if result.delivery_guard is not None and not await result.delivery_guard():
                return False
            try:
                sent = await self._dispatch_outbound(
                    channel=channel,
                    chat_id=chat_id,
                    content=content,
                    media=media,
                    metadata=source_metadata,
                )
            except OutboundDispatchError:
                await self._run_effects(result.failure_side_effects)
                raise

            # 3. 只有真实发送成功才提交可见历史及成功副作用。
            if sent:
                await self.commit_sent_turn(
                    result=result,
                    session_key=session_key,
                    channel=channel,
                    chat_id=chat_id,
                    metadata=source_metadata,
                )
            else:
                await self._run_effects(result.failure_side_effects)

        return sent

    async def commit_sent_turn(
        self,
        *,
        result: TurnResult,
        session_key: str,
        channel: str,
        chat_id: str,
        metadata: dict[str, str],
    ) -> None:
        """供主动及调度宿主在真实投递成功后提交历史和完成事件。"""
        commit = asyncio.create_task(
            self._commit_sent_turn(
                result=result,
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                metadata=metadata,
            )
        )
        try:
            await asyncio.shield(commit)
        except asyncio.CancelledError:
            await commit
            raise

    async def _commit_sent_turn(
        self,
        *,
        result: TurnResult,
        session_key: str,
        channel: str,
        chat_id: str,
        metadata: dict[str, str],
    ) -> None:
        if result.outbound is None:
            raise ValueError("已投递回合必须包含正文")
        session = self._session.session_manager.get_or_create(session_key)
        delivery_id = metadata.get('delivery_id')
        existing = next((m for m in session.messages if delivery_id and m.get('delivery_id') == delivery_id), None)
        if delivery_id and (existing is None or not existing.get('id')):
            # An INSERT may have committed even when its acknowledgement was lost.
            store = getattr(self._session.session_manager, '_store', None)
            persisted = next((m for m in store.fetch_session_messages(session_key)
                if m.get('delivery_id') == delivery_id), None) if store is not None else None
            if persisted is not None:
                if existing is None:
                    session.messages.append(persisted)
                    existing = persisted
                else:
                    existing.update(persisted)
        newly_committed = existing is None or not existing.get('id')
        if existing is not None and not existing.get('id'):
            await self._session.session_manager.append_messages(session, [existing])
        elif existing is None:
            self._persist_proactive_session(
                session=session, content=result.outbound.content,
                media=list(result.outbound.media), result=result, metadata=metadata,
            )
            await self._session.session_manager.append_messages(session, session.messages[-1:])
        if newly_committed and self._session.presence:
            self._session.presence.record_proactive_sent(session_key)
        await self._run_effects(result.success_side_effects)
        if newly_committed and self._event_bus is not None:
            await self._event_bus.fanout(
                ProactiveMessageCommitted(
                    session_key=session_key,
                    channel=channel,
                    chat_id=chat_id,
                    assistant_response=result.outbound.content,
                    tools_used=("message_push",),
                )
            )

    async def dispatch_proactive_retry(
        self,
        *,
        result: TurnResult,
        session_key: str,
        channel: str,
        chat_id: str,
    ) -> bool:
        """Retries an already committed proactive message on another transport."""

        if result.decision != "reply" or result.outbound is None:
            raise ValueError("proactive retry requires reply outbound")
        return await self._dispatch_outbound(
            channel=channel,
            chat_id=chat_id,
            content=result.outbound.content,
            media=list(result.outbound.media or []),
            metadata={
                "source": "proactive_retry",
                "session_key_override": session_key,
            },
        )

    async def _run_side_effects(self, result: TurnResult) -> None:
        await self._run_effects(result.side_effects)

    async def _run_effects(self, effects: list[Any]) -> None:
        from core.execution.proactive import proactive_scope, check_proactive
        from core.execution.contracts import JournalWriteError, ExecutionNeedsReview
        for effect in effects:
            try:
                async def run():
                    maybe = effect.run()
                    return await maybe if inspect.isawaitable(maybe) else maybe
                scope = proactive_scope()
                if scope is not None:
                    await scope.effect("tool", {"name": type(effect).__name__, "arguments": {},
                                               "retry_policy": "unknown"}, run)
                else:
                    await run()
                check_proactive()
            except (JournalWriteError, ExecutionNeedsReview):
                raise
            except Exception as e:
                logger.warning("turn side effect failed: %s", e)

    async def _dispatch_outbound(
        self,
        *,
        channel: str,
        chat_id: str,
        content: str,
        media: list[str],
        metadata: dict[str, Any],
    ) -> bool:
        return await self._outbound.dispatch(
            OutboundDispatch(
                channel=channel,
                chat_id=chat_id,
                content=content,
                metadata=metadata,
                media=media,
            )
        )

    def _persist_proactive_session(
        self,
        *,
        session: SessionLike,
        content: str,
        media: list[str],
        result: TurnResult,
        metadata: dict[str, str],
    ) -> None:
        source_refs = []
        state_summary_tag = "none"
        if result.trace is not None and isinstance(result.trace.extra, dict):
            raw_refs = result.trace.extra.get("source_refs", [])
            if isinstance(raw_refs, list):
                source_refs = [ref for ref in raw_refs if isinstance(ref, dict)]
            state_summary_tag = str(result.trace.extra.get("state_summary_tag", "none"))
        session.add_message(
            "assistant",
            content,
            media=media if media else None,
            proactive=True,
            delivery_id=metadata.get("delivery_id", ""),
            tools_used=["message_push"],
            evidence_item_ids=[str(item_id) for item_id in result.evidence],
            source_refs=source_refs,
            state_summary_tag=state_summary_tag,
            metadata={k: v for k, v in metadata.items() if k != '_delivery_projection'},
        )

    def _build_source_metadata(
        self,
        *,
        session: SessionLike,
        session_key: str,
        channel: str,
        chat_id: str,
    ) -> dict[str, str]:
        return {
            "source": "proactive",
            "session_key_override": session_key,
            "transport_channel": channel,
            "transport_chat_id": chat_id,
        }
