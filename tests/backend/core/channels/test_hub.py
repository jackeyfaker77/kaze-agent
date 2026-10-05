import json
import pytest
from bus.events import InboundMessage, OutboundMessage
from core.channels import ChannelHub
from session.manager import SessionManager


@pytest.mark.parametrize("allow,sender,alias,expected", [({}, "owner", "", False),
    ({"telegram": ["owner"]}, "owner", "", True),
    ({"telegram": ["owner"]}, "stranger", "", False),
    ({"telegram": ["alice"]}, "123", "alice", True),
    ({"telegram": ["*"]}, "123", "", True)])
def test_global_allowlist_is_fail_closed(tmp_path, allow, sender, alias, expected):
    (tmp_path / "channels.json").write_text(json.dumps({"allow_from": allow}), encoding="utf-8")
    hub = ChannelHub.from_workspace(tmp_path, session_manager=SessionManager(tmp_path))
    assert hub.is_sender_allowed(channel="telegram", chat_id="42", sender_id=sender, sender_alias=alias) is expected


def test_routing_keeps_transport_sessions_independent(tmp_path):
    hub = ChannelHub(SessionManager(tmp_path))
    first = hub.route_inbound(InboundMessage(channel="telegram", sender="owner", chat_id="42", content="hi"))
    second = hub.route_inbound(InboundMessage(channel="qq", sender="owner", chat_id="42", content="hi"))
    assert first.session_key == "telegram:42"
    assert second.session_key == "qq:42"
    assert hub.resolve_runtime_session_key("telegram", "42") == first.session_key
    with pytest.raises(ValueError, match="sender_id"):
        hub.route_inbound(InboundMessage(channel="telegram", sender="", chat_id="42", content="hi"))


@pytest.mark.parametrize("override", [False, True])
def test_delivery_updates_only_the_target_persisted_session(tmp_path, override):
    sessions = SessionManager(tmp_path)
    target = "desktop:chosen" if override else "telegram:42"
    for key in [target, "other"]:
        session = sessions.get_or_create(key)
        session.add_message("assistant", "reply")
        sessions.save(session)
    updated = ChannelHub(sessions).mark_delivery(OutboundMessage(channel="telegram", chat_id="42", content="reply",
        metadata={"session_key_override": target} if override else {}),
        default_channel="telegram", delivery_status="sent", external_message_id="message-1")
    assert updated["delivery_status"] == "sent"
    sessions.invalidate(target)
    assert sessions.get_or_create(target).messages[-1]["external_message_id"] == "message-1"
    assert not sessions.get_or_create("other").messages[-1].get("external_message_id")
