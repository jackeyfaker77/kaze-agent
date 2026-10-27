from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from infra.channels.contract import Channel, ChannelContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChannelFailure:
    """Describes a channel that could not be constructed or started."""

    channel: str
    phase: str
    error_type: str
    message: str


class ChannelHost:
    def __init__(
        self,
        ctx_factory: Callable[[Channel], ChannelContext],
    ) -> None:
        self._ctx_factory = ctx_factory
        self._channels: list[Channel] = []
        self._failures: list[ChannelFailure] = []
        self._started: set[str] = set()
        self._recovery_tasks: dict[str, asyncio.Task[None]] = {}
        self._start_lock = asyncio.Lock()
        self._stopping = False

    def add(self, channel: Channel) -> None:
        self._channels.append(channel)

    def record_failure(self, channel: str, *, phase: str, error: BaseException) -> None:
        """Records a channel failure while allowing independent channels to proceed."""

        self._failures = [f for f in self._failures if (f.channel, f.phase) != (channel, phase)]
        self._failures.append(
            ChannelFailure(
                channel=str(channel),
                phase=str(phase),
                error_type=type(error).__name__,
                message=str(error),
            )
        )

    async def start_all(self) -> None:
        async with self._start_lock:
            for channel in self._channels:
                if self._stopping:
                    return
                if channel.name in self._started or channel.name in self._recovery_tasks:
                    continue
                error = await self._start_once(channel)
                if not self._stopping and error is not None and callable(getattr(channel, 'startup_retry_delay', None)):
                    self._recovery_tasks[channel.name] = asyncio.create_task(
                        self._recover_start(channel, error), name=f'channel_start:{channel.name}')

    async def _start_once(self, channel: Channel) -> Exception | None:
        try:
            await channel.start(self._ctx_factory(channel))
        except Exception as exc:
            self.record_failure(channel.name, phase='start', error=exc)
            # Provider exceptions can contain credentials; status/logs need only the type.
            logger.warning('渠道启动失败 %s: %s', channel.name, type(exc).__name__)
            return exc
        self._started.add(channel.name)
        self._failures = [f for f in self._failures if (f.channel, f.phase) != (channel.name, 'start')]
        logger.info('渠道已启动: %s', channel.name)
        return None

    async def _recover_start(self, channel: Channel, error: Exception) -> None:
        retry_delay = getattr(channel, 'startup_retry_delay')
        attempt = 0
        try:
            while not self._stopping:
                delay = retry_delay(error, attempt)
                if delay is None:
                    return
                logger.info('渠道等待恢复 %s: %.1fs', channel.name, delay)
                await asyncio.sleep(delay)
                if self._stopping:
                    return
                next_error = await self._start_once(channel)
                if next_error is None:
                    return
                error = next_error
                attempt += 1
        finally:
            self._recovery_tasks.pop(channel.name, None)

    async def stop_all(self) -> None:
        self._stopping = True
        tasks = list(self._recovery_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._recovery_tasks.clear()
        async with self._start_lock:
            self._started.clear()
            for channel in reversed(self._channels):
                try:
                    await channel.stop()
                except Exception as e:
                    logger.warning("渠道停止失败 %s: %s", channel.name, type(e).__name__)

    def health(self) -> dict[str, bool]:
        return {channel.name: channel.name in self._started and bool(getattr(channel, "is_running", True))
                for channel in self._channels}

    @property
    def channels(self) -> list[Channel]:
        return list(self._channels)

    @property
    def failures(self) -> list[ChannelFailure]:
        """Returns a snapshot of channel construction/start failures."""

        return list(self._failures)
