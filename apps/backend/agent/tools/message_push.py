"""
统一消息推送工具，agent 通过 channel + chat_id 向任意已注册渠道发送消息、文件或图片。
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from agent.tools.base import Tool
from bus.event_bus import EventBus
from bus.events_lifecycle import ExternalImagePushed

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryReceipt:
    """一次推送的结构化回执。

    - `text`：与改造前完全一致的人类可读文本，`execute()` 原样返回（模型侧不变）
    - `message_id`：平台消息号；渠道拿不到时为 None（即"没有平台级回执"）
    - `ok`：本次请求涉及的所有操作是否都成功
    """

    ok: bool
    channel: str
    chat_id: str
    text: str
    message_id: str | None = None
    error: str | None = None

    @property
    def delivery_ref(self) -> str | None:
        """可用于 feed 账本 delivery_ref 的字符串；无平台回执时为 None。"""
        return f"{self.channel}:{self.message_id}" if self.message_id else None

    @property
    def has_platform_receipt(self) -> bool:
        return self.message_id is not None


def _extract_receipt(raw: object) -> tuple[str | None, str | None]:
    """从渠道 sender 的返回值里提取 (message_id, error)。

    兼容三种返回：
    - None：已发送，但渠道不提供回执（当前 all channels 都是这种）
    - DeliveryReceipt：渠道自己给出结构
    - 任何带 `message_id` 属性的对象（例如 Telegram 的 Message）
    """
    if raw is None:
        return None, None
    if isinstance(raw, DeliveryReceipt):
        return raw.message_id, raw.error if raw.ok else (raw.error or raw.text or "发送失败")
    mid = getattr(raw, "message_id", None)
    if mid is None and isinstance(raw, (int, str)):
        mid = raw
    return (str(mid) if mid is not None else None), None


class MessagePushTool(Tool):
    name = "message_push"
    description = (
        "向指定渠道的用户主动发送消息、文件或图片。"
        "需要提供当前会话对应的渠道名和目标 chat_id。"
        "渠道名必须使用渠道原名：telegram、qq（NapCat QQ）或 qqbot（官方 QQBot）；"
        "官方 QQBot 不能写成 qq。QQBot 私聊 chat_id 格式为 c2c:<user_openid>。"
        "message/file/image 三者至少提供一个。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "channel": {
                "type": "string",
                "description": (
                    "目标渠道原名：telegram、qq（NapCat QQ）或 qqbot（官方 QQBot）。"
                    "官方 QQBot 必须填写 qqbot，不能填写 qq。"
                ),
            },
            "chat_id": {
                "type": "string",
                "description": "目标会话 ID；官方 QQBot 私聊使用 c2c:<user_openid>",
            },
            "message": {
                "type": "string",
                "description": "要发送的文本内容（可与 file/image 同时提供）",
            },
            "file": {
                "type": "string",
                "description": "要发送的文件本地路径，例如 /tmp/report.pdf",
            },
            "image": {
                "type": "string",
                "description": "要发送的图片本地路径或 URL",
            },
        },
        "required": ["channel", "chat_id"],
    }

    def __init__(self, event_bus: EventBus | None = None) -> None:
        # channel -> {type: sender_fn}
        self._senders: dict[str, dict[str, Callable[..., Awaitable[Any]]]] = {}
        self._target_resolvers: dict[str, Callable[[str], str]] = {}
        self._event_bus = event_bus

    def register_channel(
        self,
        channel: str,
        text: Callable[[str, str], Awaitable[Any]] | None = None,
        stream_text: Callable[[str, str], Awaitable[Any]] | None = None,
        file: Callable[[str, str, str | None], Awaitable[Any]] | None = None,
        image: Callable[[str, str], Awaitable[Any]] | None = None,
        target_resolver: Callable[[str], str] | None = None,
        committed_text: Callable[[str, str], Awaitable[Any]] | None = None,
        pending_text: Callable[[str, str], Awaitable[Any]] | None = None,
    ) -> None:
        """注册渠道的各类 sender。
        - text(chat_id, message)
        - stream_text(chat_id, message)
        - file(chat_id, file_path, name=None)
        - image(chat_id, image_path_or_url)
        - target_resolver(chat_id) -> canonical chat_id

        sender 可以返回 None（= 已发送但无回执），也可以返回 DeliveryReceipt，
        或任何带 `message_id` 属性的对象 —— 后者会变成回执里的平台消息号。
        """
        self._senders[channel] = {}
        if committed_text:
            self._senders[channel]["committed_text"] = committed_text
        if pending_text:
            self._senders[channel]["pending_text"] = pending_text
        if text:
            self._senders[channel]["text"] = text
        if stream_text:
            self._senders[channel]["stream_text"] = stream_text
        if file:
            self._senders[channel]["file"] = file
        if image:
            self._senders[channel]["image"] = image
        if target_resolver is not None:
            self._target_resolvers[channel] = target_resolver
        else:
            self._target_resolvers.pop(channel, None)
        logger.debug(
            f"message_push: 注册渠道 {channel!r}  支持: {list(self._senders[channel])}"
        )

    async def send(self, **kwargs: Any) -> DeliveryReceipt:
        """执行一次推送并返回结构化回执。

        宿主代码（scheduler / proactive）应当调用本方法并按 `ok` 判定成败；
        `execute()` 只是它的文本渲染版本，行为与改造前一致。
        """
        channel: str = str(kwargs["channel"])
        requested_chat_id = str(kwargs["chat_id"])
        message: str | None = kwargs.get("message")
        file: str | None = kwargs.get("file")
        image: str | None = kwargs.get("image")
        session_key = str(kwargs.get("session_key") or "").strip()

        if not message and not file and not image:
            return DeliveryReceipt(
                ok=False,
                channel=channel,
                chat_id=requested_chat_id,
                text="错误：message、file、image 至少提供一个",
                error="缺少内容参数",
            )

        try:
            resolver = self._target_resolvers.get(channel)
            chat_id = resolver(requested_chat_id) if resolver is not None else requested_chat_id
        except Exception as e:
            logger.error(f"[message_push] 目标解析失败 {channel}:{requested_chat_id}: {e}")
            return DeliveryReceipt(
                ok=False,
                channel=channel,
                chat_id=requested_chat_id,
                text=f"发送失败：{e}",
                error=str(e),
            )

        session_key = session_key or (chat_id if channel == "desktop" else f"{channel}:{chat_id}")
        senders = self._senders.get(channel)
        if senders is None:
            return DeliveryReceipt(
                ok=False,
                channel=channel,
                chat_id=requested_chat_id,
                text=f"渠道 {channel!r} 未注册，可用渠道：{list(self._senders) or ['（无）']}",
                error="渠道未注册",
            )

        results: list[str] = []
        errors: list[str] = []
        message_id: str | None = None
        image_sent = False
        try:
            if message and ("text" in senders or "stream_text" in senders):
                sender_name = "stream_text" if "stream_text" in senders else "text"
                if kwargs.get("already_persisted") and "committed_text" in senders:
                    sender_name = "committed_text"
                if kwargs.get("commit_after_delivery") and "pending_text" in senders:
                    sender_name = "pending_text"
                raw = await senders[sender_name](chat_id, message)
                mid, err = _extract_receipt(raw)
                message_id = message_id or mid
                if err:
                    errors.append(str(err))
                preview = message[:60] + "..." if len(message) > 60 else message
                logger.info(f"[message_push] {channel}:{chat_id} ← text: {preview!r}")
                results.append("文本已发送")
            elif message:
                errors.append("渠道没有文本 sender")

            if file:
                if "file" not in senders:
                    results.append(f"渠道 {channel!r} 不支持发送文件")
                    errors.append("渠道不支持发送文件")
                else:
                    import os

                    name = os.path.basename(file)
                    raw = await senders["file"](chat_id, file, name)
                    mid, err = _extract_receipt(raw)
                    message_id = message_id or mid
                    if err:
                        errors.append(str(err))
                    logger.info(f"[message_push] {channel}:{chat_id} ← file: {file!r}")
                    results.append(f"文件 {name!r} 已发送")

            if image:
                if "image" not in senders:
                    results.append(f"渠道 {channel!r} 不支持发送图片")
                    errors.append("渠道不支持发送图片")
                else:
                    raw = await senders["image"](chat_id, image)
                    mid, err = _extract_receipt(raw)
                    message_id = message_id or mid
                    if err:
                        errors.append(str(err))
                    logger.info(
                        f"[message_push] {channel}:{chat_id} ← image: {image!r}"
                    )
                    results.append("图片已发送")
                    image_sent = True

        except Exception as e:
            logger.error(f"[message_push] 发送失败 {channel}:{chat_id}: {e}")
            return DeliveryReceipt(
                ok=False,
                channel=channel,
                chat_id=chat_id,
                text=f"发送失败：{e}",
                error=str(e),
            )

        if (
            image_sent
            and image
            and channel != "desktop"
            and session_key
            and self._event_bus is not None
        ):
            _ = await self._event_bus.emit(
                ExternalImagePushed(
                    session_key=session_key,
                    channel=channel,
                    chat_id=chat_id,
                    image=image,
                    attach_to_turn=_is_truthy(kwargs.get("defer_push_session_sync")),
                    already_persisted=_is_truthy(
                        kwargs.get("push_message_already_persisted")
                    ),
                )
            )

        if not results:
            return DeliveryReceipt(
                ok=False,
                channel=channel,
                chat_id=chat_id,
                text=f"渠道 {channel!r} 没有可用的 sender",
                error="没有可用的 sender",
            )

        return DeliveryReceipt(
            ok=not errors,
            channel=channel,
            chat_id=chat_id,
            text="；".join(results),
            message_id=message_id,
            error="；".join(errors) if errors else None,
        )

    async def execute(self, **kwargs: Any) -> str:
        """兼容旧调用方：返回与改造前完全一致的文本（模型侧输出不变）。"""
        return (await self.send(**kwargs)).text


def _is_truthy(value: object) -> bool:
    return value is True or str(value or "").strip().lower() in {"1", "true", "yes"}
