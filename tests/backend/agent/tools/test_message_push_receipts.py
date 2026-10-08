from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.tools.message_push import DeliveryReceipt, MessagePushTool
from bus.event_bus import EventBus
from conversation.push_sync import ExternalImageSyncService
from session.manager import SessionManager


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,payload,label", [
    ("text", {"message": "hello"}, "文本"),
    ("file", {"file": "review.txt"}, "文件"),
    ("image", {"image": "review.png"}, "图片"),
])
@pytest.mark.parametrize("raises", [False, True])
async def test_failed_operations_report_failure_and_do_not_sync_images(tmp_path, kind, payload, label, raises):
    sessions, bus = SessionManager(tmp_path), EventBus()
    ExternalImageSyncService(session_manager=sessions, event_bus=bus)
    sender = AsyncMock(return_value=DeliveryReceipt(
        False, "telegram", "42", "not delivered", "invalid-id", "offline",
    ))
    if raises:
        sender.side_effect = RuntimeError("offline")
    push = MessagePushTool(bus)
    push.register_channel("telegram", **{kind: sender})
    try:
        receipt = await push.send(channel="telegram", chat_id="42", **payload)
        assert receipt.ok is False and receipt.error == "offline"
        assert receipt.message_id is None and receipt.delivery_ref is None
        assert f"{label}发送失败" in receipt.text and "已发送" not in receipt.text
        assert await push.execute(channel="telegram", chat_id="42", **payload) == receipt.text
        assert sessions.get_or_create("telegram:42").messages == []
    finally:
        await bus.aclose()
        sessions._store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_mixed_delivery_keeps_each_result_and_only_syncs_successful_images(tmp_path, raises):
    sessions, bus = SessionManager(tmp_path), EventBus()
    ExternalImageSyncService(session_manager=sessions, event_bus=bus)
    push = MessagePushTool(bus)
    file_sender = AsyncMock(return_value=DeliveryReceipt(False, "telegram", "42", "failed", error="offline"))
    if raises:
        file_sender.side_effect = RuntimeError("offline")
    image_sender = AsyncMock(return_value=None)
    push.register_channel("telegram", text=AsyncMock(return_value=SimpleNamespace(message_id=100)),
                          file=file_sender, image=image_sender)
    try:
        receipt = await push.send(channel="telegram", chat_id="42", message="hello", file="review.txt", image="review.png")
        assert receipt.ok is False and receipt.message_id == "100"
        assert receipt.text == "文本已发送；文件发送失败：offline；图片已发送"
        image_sender.assert_awaited_once()
        messages = sessions.get_or_create("telegram:42").messages
        assert len(messages) == 1 and messages[0]["media"] == ["review.png"]
    finally:
        await bus.aclose()
        sessions._store.close()
