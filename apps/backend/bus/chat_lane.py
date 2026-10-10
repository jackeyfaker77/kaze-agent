"""按传输目标协调被动回复与非被动发送。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TypeVar

_T = TypeVar("_T")


@dataclass
class _ChatState:
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    users: int = 0
    passive_turns: int = 0
    passive_sends: int = 0
    next_ticket: int = 0
    serving_ticket: int = 0
    cancelled_tickets: set[int] = field(default_factory=set)
    sending: bool = False


@dataclass
class _Scope:
    key: tuple[str, str]
    active: bool = True


class ChatLane:
    """同一目标被动优先、非被动 FIFO；不同目标分别协调。"""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], _ChatState] = {}
        self._passive: ContextVar[_Scope | None] = ContextVar(
            "chat_lane_passive", default=None
        )
        self._sender: ContextVar[_Scope | None] = ContextVar(
            "chat_lane_sender", default=None
        )

    def _acquire(
        self, channel: str, chat_id: str
    ) -> tuple[tuple[str, str], _ChatState]:
        key = (str(channel), str(chat_id))
        state = self._states.setdefault(key, _ChatState())
        state.users += 1
        return key, state

    def _release(self, key: tuple[str, str], state: _ChatState) -> None:
        state.users -= 1
        if not (
            state.users
            or state.passive_turns
            or state.passive_sends
            or state.sending
            or state.next_ticket != state.serving_ticket
            or state.cancelled_tickets
        ):
            self._states.pop(key, None)

    @staticmethod
    def _skip_cancelled(state: _ChatState) -> None:
        while state.serving_ticket in state.cancelled_tickets:
            state.cancelled_tickets.remove(state.serving_ticket)
            state.serving_ticket += 1

    async def _change_pending(
        self, channel: str, chat_id: str, *, sends: bool, delta: int
    ) -> None:
        key, state = self._acquire(channel, chat_id)
        try:
            async with state.condition:
                pending = (
                    state.passive_sends if sends else state.passive_turns
                ) + delta
                if pending < 0:
                    raise RuntimeError(f"ChatLane 确认次数超过准入次数: {key}")
                if sends:
                    state.passive_sends = pending
                else:
                    state.passive_turns = pending
                state.condition.notify_all()
        finally:
            self._release(key, state)

    async def mark_passive_pending(self, channel: str, chat_id: str) -> None:
        await self._change_pending(channel, chat_id, sends=False, delta=1)

    async def mark_passive_done(self, channel: str, chat_id: str) -> None:
        await self._change_pending(channel, chat_id, sends=False, delta=-1)

    async def mark_passive_send_pending(self, channel: str, chat_id: str) -> None:
        await self._change_pending(channel, chat_id, sends=True, delta=1)

    @asynccontextmanager
    async def passive_turn(
        self, channel: str, chat_id: str, *, pending: bool = True
    ) -> AsyncIterator[None]:
        """覆盖直接回合及桌面完成通知，并允许本轮工具向自身目标发送。"""
        key = (str(channel), str(chat_id))
        current = self._passive.get()
        if current is not None and current.active and current.key == key:
            yield
            return
        if pending:
            await self.mark_passive_pending(*key)
        scope = _Scope(key)
        token = self._passive.set(scope)
        try:
            yield
        finally:
            scope.active = False
            self._passive.reset(token)
            if pending:
                await self.mark_passive_done(*key)

    async def run_send(
        self, channel: str, chat_id: str, send: Callable[[], Awaitable[_T]]
    ) -> _T:
        """宿主已持有发送窗口时复用它，否则按当前回合归属准入。"""
        key = (str(channel), str(chat_id))
        sender = self._sender.get()
        if sender is not None and sender.active and sender.key == key:
            return await send()
        passive = self._passive.get()
        if passive is not None and passive.active:
            return await self.run_passive(channel, chat_id, send, queued=False)
        return await self.run_non_passive(channel, chat_id, send)

    async def run_passive(
        self,
        channel: str,
        chat_id: str,
        send: Callable[[], Awaitable[_T]],
        *,
        queued: bool = True,
    ) -> _T:
        """等待在途发送结束；失败或取消也收束已排队的被动回复。"""
        key, state = self._acquire(channel, chat_id)
        sending = False
        scope = _Scope(key)
        token = None
        try:
            async with state.condition:
                while state.sending:
                    await state.condition.wait()
                state.sending = sending = True
            token = self._sender.set(scope)
            return await send()
        finally:
            scope.active = False
            if token is not None:
                self._sender.reset(token)
            async with state.condition:
                if queued and state.passive_sends > 0:
                    state.passive_sends -= 1
                if sending:
                    state.sending = False
                state.condition.notify_all()
            self._release(key, state)

    async def run_non_passive(
        self, channel: str, chat_id: str, send: Callable[[], Awaitable[_T]]
    ) -> _T:
        async with self.non_passive_send(channel, chat_id):
            return await send()

    @asynccontextmanager
    async def non_passive_send(self, channel: str, chat_id: str) -> AsyncIterator[None]:
        """等被动回合及回复全部结束，按 FIFO 持有发送及历史提交窗口。"""
        key, state = self._acquire(channel, chat_id)
        ticket = -1
        sending = False
        scope = _Scope(key)
        token = None
        try:
            async with state.condition:
                ticket = state.next_ticket
                state.next_ticket += 1
                self._skip_cancelled(state)
                while (
                    state.sending
                    or state.passive_turns
                    or state.passive_sends
                    or ticket != state.serving_ticket
                ):
                    await state.condition.wait()
                    self._skip_cancelled(state)
                state.sending = sending = True
            token = self._sender.set(scope)
            yield
        finally:
            scope.active = False
            if token is not None:
                self._sender.reset(token)
            async with state.condition:
                if ticket >= 0:
                    if sending:
                        state.serving_ticket += 1
                        state.sending = False
                    else:
                        state.cancelled_tickets.add(ticket)
                    self._skip_cancelled(state)
                state.condition.notify_all()
            self._release(key, state)
