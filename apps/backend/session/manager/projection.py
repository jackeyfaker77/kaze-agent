"""Session conversation thread 投影。"""

from __future__ import annotations

from typing import Any

from .models import Session

class _ProjectionMixin:
    def mark_latest_assistant_delivery(
        self,
        session_key: str,
        *,
        thread_id: str = "",
        delivery_status: str,
        external_message_id: str = "",
    ) -> dict[str, Any] | None:
        updated = self._store.update_latest_assistant_delivery(
            session_key,
            thread_id=thread_id,
            delivery_status=delivery_status,
            external_message_id=external_message_id,
        )
        if updated is None:
            return None
        session = self._cache.get(session_key)
        if session is None:
            return updated
        updated_id = str(updated.get("id") or "").strip()
        for message in reversed(session.messages):
            if str(message.get("id") or "").strip() != updated_id:
                continue
            if "delivery_status" in updated:
                message["delivery_status"] = updated["delivery_status"]
            if updated.get("external_message_id"):
                message["external_message_id"] = updated["external_message_id"]
            break
        return updated
