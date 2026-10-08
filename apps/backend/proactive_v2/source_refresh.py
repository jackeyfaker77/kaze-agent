"""旧 JSON 源声明的刷新任务，与主动候选判断分开运行。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from proactive_v2.legacy_mcp_sources import _load_sources
from proactive_v2.mcp_sources import SharedMcpGateway

logger = logging.getLogger(__name__)


class SourceRefresher:
    def __init__(
        self,
        workspace: Path,
        gateway: SharedMcpGateway,
        *,
        active_routes: set[tuple[str, str]],
        interval_seconds: int,
    ) -> None:
        self._workspace, self._gateway = workspace, gateway
        self._active_routes = active_routes
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None

    async def refresh_once(self) -> None:
        calls = {}
        for source in _load_sources(self._workspace):
            route = (
                source["server"],
                str(source.get("get_tool") or source.get("fetch_tool") or ""),
            )
            poll = str(source.get("poll_tool") or "").strip()
            if (
                route not in self._active_routes
                or not poll
                or source["channel"] not in {"", "content"}
            ):
                continue
            args = dict(source.get("poll_args", {}))
            calls[(source["server"], poll, json.dumps(args, sort_keys=True))] = args
        results = await asyncio.gather(
            *(
                self._gateway.call(server, poll, args, timeout=180)
                for (server, poll, _), args in calls.items()
            ),
            return_exceptions=True,
        )
        for route, result in zip(calls, results):
            if isinstance(result, BaseException):
                logger.warning(
                    "[proactive.source.refresh] 刷新失败 %s.%s: %s",
                    route[0],
                    route[1],
                    result,
                )

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name="proactive_source_refresh"
            )

    async def _run(self) -> None:
        while True:
            try:
                await self.refresh_once()
            except Exception:
                logger.exception("[proactive.source.refresh] 刷新声明失败")
            await asyncio.sleep(self._interval)

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
