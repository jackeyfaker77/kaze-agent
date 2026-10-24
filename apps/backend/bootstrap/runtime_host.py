"""Own the backend lifetime; desktop stdio is an optional attachment."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
import logging
import signal

from bootstrap.app import AppRuntime
from bootstrap.host_state import WorkspaceOwner
from bootstrap.tools import CoreRuntime

logger = logging.getLogger(__name__)
Attachment = Callable[[CoreRuntime], Awaitable[None]]


class RuntimeHost:
    def __init__(self, runtime: AppRuntime, *, mode: str = "host"):
        self.runtime = runtime
        self.mode = mode
        self._stop = asyncio.Event()
        self._state = "starting"

    def request_stop(self) -> None:
        self._stop.set()

    @contextmanager
    def _signals(self, enabled: bool):
        previous = {}
        loop = asyncio.get_running_loop()
        try:
            if enabled:
                for signum in (signal.SIGINT, signal.SIGTERM):
                    previous[signum] = signal.signal(signum, lambda *_: loop.call_soon_threadsafe(self.request_stop))
            yield
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)

    def _publish(self, owner: WorkspaceOwner, **extra) -> None:
        owner.publish(self._state, **self.runtime.health(), **extra)

    async def _control(self, owner: WorkspaceOwner) -> None:
        while not self._stop.is_set():
            if owner.stop_requested():
                self._stop.set()
                break
            self._publish(owner)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1)
            except TimeoutError:
                pass

    async def run(self, attachment: Attachment | None = None, *, handle_signals: bool = False) -> None:
        try:
            owner = WorkspaceOwner(self.runtime.workspace, self.mode)
        except BaseException:
            await self.runtime.shutdown()
            raise
        tasks: list[asyncio.Task] = []
        failed = False
        try:
            with self._signals(handle_signals):
                self._publish(owner)
                control = asyncio.create_task(self._control(owner), name="host_control")
                startup = asyncio.create_task(self.runtime.start(), name="host_startup")
                tasks.extend((control, startup))
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if control in done:
                    await control
                    return
                await startup
                self._state = "running"
                self._publish(owner)
                logger.info("Runtime Host ready mode=%s owner=%s", self.mode, owner.owner_id)
                watched = [control, *self.runtime.background_tasks]
                client = None
                if attachment is not None:
                    if self.runtime.core is None:
                        raise RuntimeError("Runtime Host core is unavailable")
                    async def serve_attachment():
                        assert self.runtime.core is not None
                        await attachment(self.runtime.core)
                    client = asyncio.create_task(serve_attachment(), name="desktop_attachment")
                    tasks.append(client)
                    watched.append(client)
                done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
                if control in done:
                    await control
                    return
                for task in self.runtime.background_tasks:
                    if task in done:
                        # Normal return is also a failure for a resident worker.
                        if not task.cancelled():
                            await task
                        raise RuntimeError(f"Runtime worker stopped unexpectedly: {task.get_name()}")
                if client is not None:
                    await client  # EOF ends only the existing local desktop mode.
        except BaseException:
            failed = True
            raise
        finally:
            try:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self._state = "stopping"
                try:
                    self._publish(owner)
                finally:
                    await self.runtime.shutdown()
            except BaseException:
                failed = True
                raise
            finally:
                try:
                    owner.publish("failed" if failed else "stopped", healthy=False)
                finally:
                    owner.close()
