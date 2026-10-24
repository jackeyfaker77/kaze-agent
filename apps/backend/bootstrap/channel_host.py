from __future__ import annotations

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

    def add(self, channel: Channel) -> None:
        self._channels.append(channel)

    def record_failure(self, channel: str, *, phase: str, error: BaseException) -> None:
        """Records a channel failure while allowing independent channels to proceed."""

        self._failures.append(
            ChannelFailure(
                channel=str(channel),
                phase=str(phase),
                error_type=type(error).__name__,
                message=str(error),
            )
        )

    async def start_all(self) -> None:
        for channel in self._channels:
            try:
                await channel.start(self._ctx_factory(channel))
                self._started.add(channel.name)
                logger.info("渠道已启动: %s", channel.name)
            except Exception as e:
                self.record_failure(channel.name, phase="start", error=e)
                logger.error("渠道启动失败 %s: %s", channel.name, e)

    async def stop_all(self) -> None:
        self._started.clear()
        for channel in reversed(self._channels):
            try:
                await channel.stop()
            except Exception as e:
                logger.warning("渠道停止失败 %s: %s", channel.name, e)

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
