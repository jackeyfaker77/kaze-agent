"""Remove unusable observations before a context snapshot reaches a decision."""

from datetime import datetime
from typing import Any

_METADATA = {
    "_source",
    "source_id",
    "kind",
    "context_type",
    "schema_version",
    "observed_at",
    "expires_at",
    "ttl_seconds",
    "errors",
}


def _timestamp(value: Any, *, aware: bool) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if (result.tzinfo is not None) == aware else None


def current_context_snapshot(snapshot: dict, *, now: datetime) -> dict:
    payload = dict(snapshot)
    reason = "unavailable" if payload.get("available") is False else ""
    observed_raw, expires_raw = payload.get("observed_at"), payload.get("expires_at")
    # Legacy replay clocks may be naive; compare only within the same clock domain.
    aware = now.tzinfo is not None
    observed = _timestamp(observed_raw, aware=aware)
    expires = _timestamp(expires_raw, aware=aware)
    if (observed_raw not in (None, "") and observed is None) or (
        expires_raw not in (None, "") and expires is None
    ):
        reason = "invalid_timestamp"
    elif expires is not None:
        if observed is not None and expires <= observed:
            reason = "invalid_lifetime"
        elif expires <= now:
            reason = "expired"
    if not reason:
        # Legacy context sources without expiry keep their existing contract.
        return payload
    result = {key: value for key, value in payload.items() if key in _METADATA}
    if observed_raw not in (None, "") and observed is None:
        result.pop("observed_at", None)
    if expires_raw not in (None, "") and expires is None:
        result.pop("expires_at", None)
    result.update(
        {
            "available": False,
            "presence": "unknown",
            "confidence": 0.0,
            "interruptibility": 0.0,
            "unavailable_reason": reason,
        }
    )
    return result
