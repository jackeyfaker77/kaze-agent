"""Application heartbeat configuration."""
from dataclasses import dataclass

@dataclass
class ProactiveConfig:
    enabled: bool = False
    default_channel: str = "desktop"
    default_chat_id: str = ""
    session_key: str = ""
    interval_seconds: int = 1800
