from __future__ import annotations

from typing import Any, cast

from agent.lifecycle.types import AfterReasoningCtx
from bus.event_bus import EventBus
from bus.events_lifecycle import ExternalImagePushed, ProactiveMessageCommitted
from session.manager import Session, SessionManager


class ExternalImageSyncService:
    """Persists externally pushed images into their ordinary session."""

    def __init__(
        self,
        *,
        session_manager: SessionManager,
        event_bus: EventBus,
    ) -> None:
        self._sessions = session_manager
        self._event_bus = event_bus
        self._pending_turn_images: dict[str, list[str]] = {}
        event_bus.on(ExternalImagePushed, self.handle_image_pushed)
        event_bus.on(AfterReasoningCtx, self.attach_turn_images)

    async def handle_image_pushed(
        self,
        event: ExternalImagePushed,
    ) -> ExternalImagePushed:
        """Queues turn-owned images or persists background deliveries immediately."""

        if event.already_persisted:
            self._validate_existing_message(event)
            return event
        if event.attach_to_turn:
            pending = self._pending_turn_images.setdefault(event.session_key, [])
            if event.image not in pending:
                pending.append(event.image)
            return event

        session = self._sessions.get_or_create(event.session_key)
        metadata = {"transport_channel": event.channel, "transport_chat_id": event.chat_id}
        session.add_message(
            "assistant",
            "",
            media=[event.image],
            proactive=True,
            tools_used=["message_push"],
            sender_role="assistant",
            metadata=metadata,
        )
        await self._sessions.append_messages(session, session.messages[-1:])
        await self._event_bus.fanout(
            ProactiveMessageCommitted(
                session_key=event.session_key,
                channel=event.channel,
            )
        )
        return event

    def attach_turn_images(self, ctx: AfterReasoningCtx) -> AfterReasoningCtx:
        """Attaches already-delivered images to persistence without resending them."""

        pending = self._pending_turn_images.pop(ctx.session_key, [])
        for image in pending:
            if image not in ctx.persisted_media:
                ctx.persisted_media.append(image)
        return ctx

    def _validate_existing_message(self, event: ExternalImagePushed) -> None:
        session = self._sessions.get_or_create(event.session_key)
        if self._last_message_contains(session, event):
            return
        raise RuntimeError("外部图片已发送，但预写入的共享会话消息不存在")

    @staticmethod
    def _last_message_contains(session: Session, event: ExternalImagePushed) -> bool:
        if not session.messages:
            return False
        message = session.messages[-1]
        metadata = message.get("metadata")
        source = cast(dict[str, Any], metadata) if isinstance(metadata, dict) else {}
        same_transport = (
            str(source.get("transport_channel") or "") == event.channel
            and str(source.get("transport_chat_id") or "") == event.chat_id
        )
        multi_transport_proactive = (
            message.get("proactive") is True
            and str(source.get("source") or "") == "proactive"
        )
        return (
            message.get("role") == "assistant"
            and event.image in list(message.get("media") or [])
            and (same_transport or multi_transport_proactive)
        )

