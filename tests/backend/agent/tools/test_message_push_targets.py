from __future__ import annotations

import pytest

from agent.tools.message_push import MessagePushTool




@pytest.mark.asyncio
async def test_message_push_sends_to_registered_channel() -> None:
    sent: list[tuple[str, str]] = []
    tool = MessagePushTool()

    async def send(chat_id: str, message: str) -> None:
        sent.append((chat_id, message))

    tool.register_channel("telegram", text=send)
    result = await tool.execute(
        channel="telegram",
        chat_id="123",
        message="hello",
        role_id="mira",
    )

    assert result == "文本已发送"
    assert sent == [("123", "hello")]


@pytest.mark.asyncio
async def test_message_push_resolves_alias_before_sending() -> None:
    sent: list[tuple[str, str]] = []
    tool = MessagePushTool()

    async def send(chat_id: str, message: str) -> None:
        sent.append((chat_id, message))

    tool.register_channel(
        "telegram",
        text=send,
        target_resolver=lambda chat_id: (
            "7602298892" if chat_id.lstrip("@").lower() == "windy" else chat_id
        ),
    )
    result = await tool.execute(
        channel="telegram",
        chat_id="@Windy",
        message="hello",
        role_id="mira",
    )

    assert result == "文本已发送"
    assert sent == [("7602298892", "hello")]


def test_message_push_schema_distinguishes_official_qqbot_from_napcat_qq() -> None:
    tool = MessagePushTool()

    assert "qqbot" in tool.description
    assert "qq（NapCat QQ）" in tool.description
    assert "不能写成 qq" in tool.description
    assert "c2c:<user_openid>" in tool.parameters["properties"]["chat_id"]["description"]


@pytest.mark.asyncio
async def test_message_push_does_not_treat_qq_as_qqbot():
    sent = []
    tool = MessagePushTool()
    async def send(chat_id, message):
        sent.append((chat_id, message))
    tool.register_channel("qqbot", text=send)
    result = await tool.execute(channel="qq", chat_id="c2c:user-1", message="hello")
    assert "未注册" in result
    assert sent == []
    result = await tool.execute(channel="qqbot", chat_id="c2c:user-1", message="hello")
    assert result == "文本已发送"
    assert sent == [("c2c:user-1", "hello")]


@pytest.mark.asyncio
async def test_message_push_sends_text_when_channel_only_registers_stream_sender() -> None:
    sent: list[tuple[str, str]] = []
    tool = MessagePushTool()

    async def send_stream(chat_id: str, message: str) -> None:
        sent.append((chat_id, message))

    tool.register_channel("qqbot", stream_text=send_stream)

    result = await tool.execute(
        channel="qqbot",
        chat_id="c2c:user-1",
        message="主动推送",
    )

    assert result == "文本已发送"
    assert sent == [("c2c:user-1", "主动推送")]




@pytest.mark.asyncio
async def test_message_push_reports_target_resolution_failure_without_sending():
    tool = MessagePushTool()
    sent = []
    async def send(chat_id, message):
        sent.append((chat_id, message))
    def resolve(chat_id):
        raise ValueError("unknown recipient")
    tool.register_channel("telegram", text=send, target_resolver=resolve)
    assert "unknown recipient" in await tool.execute(channel="telegram", chat_id="@missing", message="hello")
    assert sent == []
