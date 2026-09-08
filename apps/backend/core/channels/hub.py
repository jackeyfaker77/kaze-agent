"""Channel routing uses transport session keys and a global sender allow-list."""
from pathlib import Path
import json
from bus.events import InboundMessage, OutboundMessage

class ChannelHub:
    def __init__(self, session_manager, allow_from=None):
        self.sessions = session_manager
        self.allow_from = allow_from or {}

    @classmethod
    def from_workspace(cls, workspace: Path, *, session_manager):
        path = workspace / 'channels.json'
        config = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        return cls(session_manager, config.get('allow_from', {}))

    def route_inbound(self, message: InboundMessage) -> InboundMessage:
        if not message.sender.strip():
            raise ValueError('sender_id is required')
        return message

    def is_sender_allowed(self, *, channel, chat_id, sender_id, sender_alias=''):
        allowed = self.allow_from.get(channel, [])
        return '*' in allowed or sender_id in allowed or bool(sender_alias and sender_alias in allowed)

    def has_binding(self, channel, chat_id):
        return bool(self.allow_from.get(channel))

    def resolve_runtime_session_key(self, channel, chat_id):
        return f'{channel}:{chat_id}'

    def mark_delivery(self, message: OutboundMessage, *, default_channel, delivery_status, external_message_id=''):
        key = str(message.metadata.get('session_key_override') or f'{default_channel}:{message.chat_id}')
        return self.sessions.mark_latest_assistant_delivery(
            key, delivery_status=delivery_status, external_message_id=external_message_id)
