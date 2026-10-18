from datetime import UTC, date, datetime, time
from hashlib import sha256
import json
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


def utc_now():
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("A timezone-aware timestamp is required")
    return value.astimezone(UTC).isoformat()


def timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(
            "Use an ISO 8601 timestamp with an explicit UTC offset"
        ) from None
    if result.tzinfo is None:
        raise ValueError("Use an ISO 8601 timestamp with an explicit UTC offset")
    return result.astimezone(UTC)


def calendar_time(value: date | datetime, zone: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        result = value if value.tzinfo else value.replace(tzinfo=zone)
    else:
        result = datetime.combine(value, time.min, zone)
    return result.astimezone(UTC)


def digest(value: object) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:32]


def text(value: object, name: str, maximum: int, *, required=False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (required and not value.strip())
    ):
        raise ValueError(
            f"{name} must be {'nonblank ' if required else ''}text, at most {maximum} characters"
        )
    return value.strip()


def subscription_url(value: str) -> str:
    value = text(value, "url", 4096, required=True)
    if value.lower().startswith("webcal://"):
        value = "https://" + value[9:]
    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme in {"http", "https"}
            and parts.hostname
            and not parts.username
            and not parts.password
            and not parts.fragment
        )
        _ = parts.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            "Use an HTTP(S) ICS subscription URL without embedded credentials or fragment"
        )
    return value
