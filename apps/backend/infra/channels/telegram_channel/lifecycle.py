"""TelegramChannel 初始化与运行时生命周期。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from datetime import timedelta

from telegram import BotCommand, Update
from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from agent.looping.interrupt import InterruptController
from bus.event_bus import EventBus
from bus.events_lifecycle import (
    StreamDeltaReady,
    ToolCallCompleted,
    ToolCallStarted,
    TurnStarted,
)
from bus.queue import MessageBus
from core.channels import ChannelHub
from infra.channels.base import AttachmentStore, MessageDeduper, SessionIdentityIndex
from infra.channels.contract import ChannelContext
from infra.channels.telegram_utils import (
    TelegramLiveEditQueue,
    TelegramLiveTextMessage,
    TelegramOutboundLimiter,
    TelegramStreamMessage,
)
from session.manager import SessionManager

from .commands import _CommandMixin
from .formatting import _CHANNEL, _SEEN_MSG_MAXSIZE, _ToolLiveLine
from .inbound import _InboundMixin
from .media import _MediaMixin
from .outbound import _OutboundMixin
from .streaming import _StreamingMixin
from .durable import _DurableMixin, PersistentUpdateQueue

logger = logging.getLogger("infra.channels.telegram_channel")


class TelegramChannel(
    _DurableMixin,
    _InboundMixin,
    _CommandMixin,
    _MediaMixin,
    _StreamingMixin,
    _OutboundMixin,
):
    """连接 Telegram Bot、消息总线与 lifecycle 事件。"""

    def __init__(
        self,
        token: str,
        bus: MessageBus,
        session_manager: SessionManager,
        allow_from: list[str] | None = None,
        bot_commands: list[tuple[str, str]] | None = None,
        event_bus: EventBus | None = None,
        interrupt_controller: InterruptController | None = None,
        channel_name: str = _CHANNEL,
        channel_hub: "ChannelHub | None" = None,
    ) -> None:
        self._bus = bus
        self._session_manager = session_manager
        self._interrupt_controller = interrupt_controller
        self._channel = channel_name
        self.name = channel_name
        self._allow_from: set[str] = set(allow_from) if allow_from else set()
        self._message_deduper = MessageDeduper(_SEEN_MSG_MAXSIZE)
        ws = getattr(session_manager, "workspace", None)
        self._attachments = AttachmentStore(Path(ws) / "uploads" if ws else None)
        self._channel_hub = channel_hub
        if self._channel_hub is None and ws:
            self._channel_hub = ChannelHub.from_workspace(
                Path(ws),
                session_manager=session_manager,
            )
        self._identity_index = SessionIdentityIndex(
            session_manager,
            channel=channel_name,
            metadata_key="username",
            normalizer=lambda value: value.lower(),
        )
        builder = Application.builder().token(token)
        self._durable_updates = None
        self._durable_task = None
        if getattr(bus, 'transport', None) is not None:
            self._durable_updates = PersistentUpdateQueue(bus.transport, channel_name+':'+token.split(':', 1)[0])
            bus.outbox.accounts[channel_name] = token.split(':', 1)[0]
            builder = builder.update_queue(self._durable_updates)
        self._app = builder.build()
        self._bot_commands = bot_commands or []
        self._app.add_handler(CommandHandler("stop", self._on_stop_command))
        self._app.add_handler(
            MessageHandler(filters.COMMAND, self._on_command)
        )
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        )
        self._app.add_handler(
            MessageHandler(filters.PHOTO & ~filters.COMMAND, self._on_photo)
        )
        self._app.add_handler(
            MessageHandler(filters.Document.ALL & ~filters.COMMAND, self._on_document)
        )
        if self._durable_updates is not None:
            from telegram.ext import TypeHandler
            self._app.handlers.clear()
            self._app.add_handler(TypeHandler(Update, self._on_durable_update))
        self._event_bus = event_bus
        self._outbound_bound = False
        self._events_bound = False
        self._lifecycle_lock = asyncio.Lock()
        self._ready = False
        self.user_map = self._identity_index.mapping
        self._polling_conflict_task: asyncio.Task[None] | None = None
        self._telegram_outbound_limiter = TelegramOutboundLimiter()
        self._active_streams: dict[str, TelegramStreamMessage] = {}
        self._live_edit_queue = TelegramLiveEditQueue(limiter=self._telegram_outbound_limiter)
        self._live_messages: dict[str, TelegramLiveTextMessage] = {}
        self._reply_buffers: dict[str, str] = {}
        self._thinking_buffers: dict[str, str] = {}
        self._thinking_live_next_at: dict[str, float] = {}
        self._live_last_lengths: dict[str, int] = {}
        self._tool_lines: dict[str, list[_ToolLiveLine]] = {}
        self._live_tasks: set[asyncio.Task[None]] = set()
        self._live_tasks_by_session: dict[str, set[asyncio.Task[None]]] = {}

    @property
    def bot(self):
        return self._app.bot

    @property
    def is_running(self) -> bool:
        return bool(self._app.running and self._app.updater and self._app.updater.running
                    and (self._durable_updates is None or (not self._durable_updates.failed and self._durable_task and not self._durable_task.done())))

    async def start(self, ctx: ChannelContext | None = None) -> None:
        async with self._lifecycle_lock:
            if self._ready:
                return
            try:
                await self._start_runtime(ctx)
                self._ready = True
            except BaseException:
                # Roll back partial initialization before the Host may retry.
                # A cleanup failure propagates instead of starting a second consumer.
                try:
                    await self._stop_runtime()
                except Exception as cleanup:
                    raise RuntimeError('Telegram startup cleanup failed; automatic retry stopped') from cleanup
                raise

    @staticmethod
    def startup_retry_delay(error: Exception, attempt: int) -> float | None:
        if isinstance(error, RetryAfter):
            value = error.retry_after
            return max(5.0, value.total_seconds() if isinstance(value, timedelta) else float(value))
        # BadRequest inherits NetworkError in PTB, but retrying bad parameters is unsafe.
        if isinstance(error, (NetworkError, TimeoutError)) and not isinstance(error, BadRequest):
            return float(min(5 * 2 ** min(attempt, 4), 60))
        return None

    async def _start_runtime(self, ctx: ChannelContext | None) -> None:
        if ctx is not None:
            self._bus = ctx.bus
            self._event_bus = ctx.event_bus
            self._interrupt_controller = ctx.interrupt_controller
        self._rebuild_user_map()
        await self._app.initialize()
        await self._app.start()
        await self._register_bot_commands()
        updater = self._app.updater
        if updater is None:
            raise RuntimeError("Telegram updater 未初始化")
        await updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            error_callback=self._on_polling_error,
            bootstrap_retries=0,
        )
        # Failed startup must not expose send handlers to the durable outbox.
        self._bind_runtime()
        if self._durable_updates is not None:
            self._bus.outbox.handlers['telegram:'+self._channel] = self._deliver_command
            self._durable_task = asyncio.create_task(self._durable_updates.recover(self._app.bot), name='telegram_inbox')
        if ctx is not None:
            ctx.push_tool.register_channel(
                self.name,
                text=self.send,
                stream_text=self.send_stream,
                file=self.send_file,
                image=self.send_image,
                target_resolver=self._resolve_chat_id,
                durable_parts=self._durable_updates is not None,
            )
        logger.info(f"TelegramChannel 已启动  已知用户: {len(self.user_map)}")

    def _bind_runtime(self) -> None:
        if not self._outbound_bound:
            if self._durable_updates is not None:
                self._bus.subscribe_outbound(self._channel, self._on_response, durable_parts=True)
            else:
                self._bus.subscribe_outbound(self._channel, self._on_response)
            self._outbound_bound = True
        if self._event_bus is not None and not self._events_bound and self._durable_updates is None:
            self._event_bus.on(TurnStarted, self._on_turn_started)
            self._event_bus.on(StreamDeltaReady, self._on_stream_delta)
            self._event_bus.on(ToolCallStarted, self._on_tool_call_started)
            self._event_bus.on(ToolCallCompleted, self._on_tool_call_completed)
            self._events_bound = True

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_runtime()

    async def _stop_runtime(self) -> None:
        self._ready = False
        if self._durable_task is not None:
            self._durable_task.cancel()
            await asyncio.gather(self._durable_task, return_exceptions=True)
            self._durable_task = None
        if self._polling_conflict_task and not self._polling_conflict_task.done():
            await self._polling_conflict_task
        if self._live_tasks:
            _ = await asyncio.gather(*self._live_tasks, return_exceptions=True)
        updater = self._app.updater
        if updater and updater.running:
            await updater.stop()
        if getattr(self._app, 'running', True):
            await self._app.stop()
        await self._app.shutdown()
        # Application.shutdown is a no-op when getMe failed during initialize.
        # Bot.shutdown still closes the partially initialized HTTP clients.
        if hasattr(self._app.bot, 'shutdown'):
            await self._app.bot.shutdown()
        logger.info("TelegramChannel 已停止")

    # ── 私有方法 ──────────────────────────────────────────────────

    def _rebuild_user_map(self) -> None:
        """扫描已有 session 文件，从 metadata 重建 username → chat_id 索引。"""
        self._identity_index.rebuild()
        logger.debug(f"[telegram] user_map 重建完成: {self.user_map}")

    def _is_allowed(self, user) -> bool:
        """检查用户是否在白名单中，白名单为空则允许所有人"""
        if not self._allow_from:
            return True
        return str(user.id) in self._allow_from or (
            user.username
            and user.username.lower() in {u.lower() for u in self._allow_from}
        )

    async def _register_bot_commands(self) -> None:
        commands = [
            BotCommand(command, description)
            for command, description in [
                *self._bot_commands,
                ("stop", "中断当前回复"),
            ]
        ]
        await self._app.bot.set_my_commands(commands)

    async def _remember_username(self, chat_id: str, username: str | None) -> None:
        if username:
            await self._identity_index.remember(username, chat_id)
