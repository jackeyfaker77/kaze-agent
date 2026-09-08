from __future__ import annotations

import inspect
import logging
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


class TurnOrchestrator:
    def __init__(self, deps: TurnOrchestratorDeps) -> None:
        self._session = deps.session
        self._outbound = deps.outbound
        self._event_bus = deps.event_bus

    async def handle_proactive_turn(
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
        # 2. reply 路径只写 proactive session；后处理只归 passive commit 管。
        self._persist_proactive_session(
            session=session,
            content=content,
            media=media,
            result=result,
            metadata=source_metadata,
        )
        await self._session.session_manager.append_messages(
            session, session.messages[-1:]
        )

        # 3. 先执行发送前 side_effects，再真正 dispatch 到 outbound。
        await self._run_effects(result.side_effects)
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

        # 4. 根据是否真正发送成功，分别执行 success / failure side_effects。
        if sent:
            if self._session.presence:
                self._session.presence.record_proactive_sent(session_key)
            await self._run_effects(result.success_side_effects)
            if self._event_bus is not None:
                await self._event_bus.fanout(
                    ProactiveMessageCommitted(
                        session_key=session_key,
                        channel=channel,
                        
                        chat_id=chat_id,
                        assistant_response=content,
                        tools_used=("message_push",),
                    )
                )
        else:
            await self._run_effects(result.failure_side_effects)

        return sent

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
        for effect in effects:
            try:
                maybe = effect.run()
                if inspect.isawaitable(maybe):
                    await maybe
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
            tools_used=["message_push"],
            evidence_item_ids=[str(item_id) for item_id in result.evidence],
            source_refs=source_refs,
            state_summary_tag=state_summary_tag,
            metadata=metadata,
        )

    def _build_source_metadata(
        self,
        *,
        session: SessionLike,
        session_key: str,
        channel: str,
        chat_id: str,
    ) -> dict[str, str]:
        return {"source": "proactive", "session_key_override": session_key,
                "transport_channel": channel, "transport_chat_id": chat_id}
