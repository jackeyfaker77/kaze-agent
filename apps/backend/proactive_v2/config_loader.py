"""Loads ordinary session heartbeat settings, tolerating retired config keys."""
from proactive_v2.config import ProactiveConfig

class ProactiveConfigError(ValueError):
    pass

def load_proactive_config(value: dict) -> ProactiveConfig:
    target = value.get("target") or {}
    try:
        interval = int(value.get("interval_seconds", 1800))
    except (ValueError, TypeError) as exc:
        raise ProactiveConfigError("interval_seconds 必须是整数") from exc
    if interval < 60:
        raise ProactiveConfigError("interval_seconds 不能小于 60")
    return ProactiveConfig(
        enabled=bool(value.get("enabled", False)),
        default_channel=str(target.get("channel", value.get("default_channel", "desktop"))),
        default_chat_id=str(target.get("chat_id", value.get("default_chat_id", ""))),
        session_key=str(value.get("session_key", "")),
        interval_seconds=interval,
    )
