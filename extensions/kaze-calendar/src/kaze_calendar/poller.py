import asyncio
from datetime import timedelta
import logging

import httpx

from .models import timestamp
from .parsing import MAX_BYTES, occurrences
from .store import Store

logger = logging.getLogger(__name__)


class Poller:
    def __init__(self, store: Store, *, transport=None):
        self.store = store
        self.transport = transport
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task = None

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def wake(self):
        self._wake.set()

    async def aclose(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self):
        while True:
            self._wake.clear()
            try:
                result = await self.poll()
                if result["remaining"]:
                    self._wake.set()
            except Exception:
                logger.exception("Calendar background refresh failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=30)
            except TimeoutError:
                pass

    async def poll(self, source="", force=False):
        async with self._lock:
            now = self.store.clock()
            rows = [
                row
                for row in self.store.calendars(source)
                if row["enabled"]
                and (
                    force
                    or not row["last_checked_at"]
                    or now - timestamp(row["last_checked_at"])
                    >= timedelta(seconds=self.store.settings.poll_interval_seconds)
                )
            ]
            rows.sort(key=lambda row: row["last_checked_at"] or "")
            async with httpx.AsyncClient(
                timeout=self.store.settings.request_timeout_seconds,
                follow_redirects=True,
                max_redirects=5,
                transport=self.transport,
                headers={
                    "User-Agent": "kaze-calendar/0.1.0",
                    "Accept": "text/calendar",
                },
            ) as client:
                results = await asyncio.gather(
                    *(self._fetch(client, row) for row in rows[:4])
                )
            return {
                "ok": all(row["ok"] for row in results),
                "results": results,
                "remaining": max(0, len(rows) - 4),
            }

    async def _fetch(self, client: httpx.AsyncClient, row: dict) -> dict:
        headers = {}
        if row["etag"]:
            headers["If-None-Match"] = row["etag"]
        if row["last_modified"]:
            headers["If-Modified-Since"] = row["last_modified"]
        try:
            async with client.stream("GET", row["url"], headers=headers) as response:
                if response.status_code == 304:
                    if not row["body"]:
                        raise ValueError("304 response without cached calendar")
                    body = bytes(row["body"])
                    etag = response.headers.get("etag", row["etag"])
                    modified = response.headers.get(
                        "last-modified", row["last_modified"]
                    )
                else:
                    response.raise_for_status()
                    parts, size = [], 0
                    async for part in response.aiter_bytes():
                        size += len(part)
                        if size > MAX_BYTES:
                            raise ValueError("ICS exceeds 2 MiB")
                        parts.append(part)
                    body = b"".join(parts)
                    etag = response.headers.get("etag", "")
                    modified = response.headers.get("last-modified", "")
            now = self.store.clock()
            # Parse before committing both cache and HTTP validators. A malformed feed
            # cannot replace the last usable calendar or advance the conditional cursor.
            await asyncio.to_thread(
                occurrences,
                body,
                row["id"],
                row["name"],
                row["timezone"],
                now - timedelta(days=2),
                now + timedelta(days=32),
                row["reminder_minutes"],
                self.store.settings.all_day_reminder_hour,
            )
            saved = self.store.save_sync(
                row, body=body, etag=etag, last_modified=modified
            )
            return {
                "calendar_id": row["id"],
                "ok": saved,
                "status": "refreshed" if saved else "configuration_changed",
            }
        except Exception as exc:
            # Isolate each source, including unexpected third-party parser
            # failures. Cancellation (BaseException) must still propagate.
            if isinstance(exc, httpx.HTTPStatusError):
                error = f"http_status:{exc.response.status_code}"
            elif isinstance(exc, httpx.HTTPError):
                error = f"network:{type(exc).__name__}"
            else:
                error = f"invalid_calendar:{type(exc).__name__}"
            # Never include HTTP exception text: private ICS URLs often contain tokens.
            self.store.save_sync(row, error=error)
            return {"calendar_id": row["id"], "ok": False, "error": error}
