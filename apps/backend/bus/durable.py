"""Single-owner durable transport inbox/outbox. Never retry an uncertain effect."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace
from uuid import uuid4
from weakref import WeakValueDictionary

from core.execution.lease import EpochLease


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class DeliveryStopped(RuntimeError):
    def __init__(self, state: str):
        self.state = state
        super().__init__(f"投递状态：{state}；请核对持久发件记录")


class RetryDelivery(Exception):
    def __init__(self, delay: float = 1):
        self.delay = delay


class PermanentDelivery(Exception):
    pass


class TransportStore:
    def __init__(self, workspace: Path):
        self.directory = workspace / "private_runtime" / "transport"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lease = EpochLease(self.directory / "owner.lock")
        self.db = sqlite3.connect(self.directory / "queue.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                due REAL NOT NULL DEFAULT 0, result TEXT, error TEXT);
            CREATE INDEX IF NOT EXISTS jobs_due ON jobs(kind,state,due);
            CREATE TABLE IF NOT EXISTS parts (
                job TEXT NOT NULL, part TEXT NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL, result TEXT, PRIMARY KEY(job,part));
        """)
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state='needs_review',error='Interrupted physical send' WHERE id IN (SELECT job FROM parts WHERE state='sending')"
            )
            self.db.execute("UPDATE jobs SET state='pending' WHERE state='running'")
        self.closed = False

    def put(self, key: str, kind: str, payload: dict) -> dict:
        serialized = encode(payload)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO jobs(id,kind,payload) VALUES (?,?,?)",
                (key, kind, serialized),
            )
            row = self.get(key)
            if row["kind"] != kind or row["payload"] != payload:
                raise ValueError("Transport id reused with different content")
        return row

    def get(self, key: str) -> dict:
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return {
            **dict(row),
            "payload": json.loads(row["payload"]),
            "result": json.loads(row["result"]) if row["result"] else None,
        }

    def pending(self, kind: str | None = None) -> list[dict]:
        rows = self.db.execute(
            "SELECT id FROM jobs WHERE state='pending' AND due<=? AND (? IS NULL OR kind=?) ORDER BY rowid LIMIT 100",
            (time.time(), kind, kind),
        ).fetchall()
        return [self.get(r["id"]) for r in rows]

    def state(self, key, state, *, result=None, error=None, delay: float = 0):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state=?,result=?,error=?,due=? WHERE id=?",
                (
                    state,
                    encode(result) if result is not None else None,
                    error,
                    time.time() + delay,
                    key,
                ),
            )

    def attempt(self, key):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state='running',attempts=attempts+1 WHERE id=?", (key,)
            )

    def snapshot_file(self, value: str) -> str:
        source = Path(value)
        content = source.read_bytes()
        directory = self.directory / "attachments"
        directory.mkdir(exist_ok=True)
        path = directory / (hashlib.sha256(content).hexdigest() + source.suffix)
        if not path.exists():
            import os

            temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
            with temporary.open("wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        return str(path)

    def counts(self):
        return {
            f"{r[0]}:{r[1]}": r[2]
            for r in self.db.execute(
                "SELECT kind,state,count(*) FROM jobs GROUP BY kind,state"
            )
        }

    def close(self):
        if not self.closed:
            self.closed = True
            self.db.close()
            self.lease.close()


@dataclass
class DeliveryScope:
    queue: Outbox
    key: str
    sequence: int = 0


delivery_scope: ContextVar[DeliveryScope | None] = ContextVar(
    "transport_delivery", default=None
)


class Outbox:
    def __init__(self, store: TransportStore, chat_lane):
        self.store = store
        self.chat_lane = chat_lane
        self.handlers = {}
        self.accounts: dict[str, str] = {}
        self.locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self.active: set[str] = set()
        self.tasks: set[asyncio.Task] = set()

    def enqueue(self, kind: str, payload: dict, key: str | None = None) -> str:
        key = key or uuid4().hex
        account = self.accounts.get(payload.get("channel", ""))
        if account:
            payload = {**payload, "_transport_account": account}
        self.store.put(key, kind, payload)
        return key

    async def deliver(self, key: str):
        # Caller owns the target lane; recovery worker obtains it before this lock.
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            self.active.add(key)
            try:
                return await self._deliver(key)
            finally:
                self.active.discard(key)

    async def _deliver(self, key):
        while True:
            row = self.store.get(key)
            if row["state"] == "done":
                return row["result"]
            if row["state"] in {"needs_review", "failed"}:
                raise DeliveryStopped(row["state"])
            handler = self.handlers.get(row["kind"])
            if handler is None:
                raise DeliveryStopped("channel_unavailable")
            account = row["payload"].get("_transport_account")
            if account and account != self.accounts.get(row["payload"]["channel"]):
                self.store.state(key, "needs_review", error="Channel account changed")
                raise DeliveryStopped("needs_review")
            if row["due"] > time.time():
                await asyncio.sleep(row["due"] - time.time())
            self.store.attempt(key)
            token = delivery_scope.set(DeliveryScope(self, key))
            try:
                result = await handler(
                    {
                        k: v
                        for k, v in row["payload"].items()
                        if k != "_transport_account"
                    }
                )
                self.store.state(key, "done", result=result)
                return result
            except RetryDelivery as exc:
                attempts = self.store.get(key)["attempts"]
                state = "failed" if attempts >= 5 else "pending"
                self.store.state(
                    key,
                    state,
                    error="Retryable rejection",
                    delay=max(exc.delay, min(2 ** (attempts - 1), 60)),
                )
            except PermanentDelivery as exc:
                self.store.state(key, "failed", error=type(exc).__name__)
                raise DeliveryStopped("failed") from exc
            except BaseException as exc:
                uncertain = self.store.db.execute(
                    "SELECT 1 FROM parts WHERE job=? AND state='sending'", (key,)
                ).fetchone()
                attempts = self.store.get(key)["attempts"]
                state = (
                    "needs_review"
                    if uncertain
                    else (
                        "pending"
                        if isinstance(exc, asyncio.CancelledError) or attempts < 5
                        else "failed"
                    )
                )
                self.store.state(
                    key,
                    state,
                    error=type(exc).__name__,
                    delay=min(2 ** (attempts - 1), 60),
                )
                raise
            finally:
                delivery_scope.reset(token)

    async def part(self, name: str, payload: dict, operation):
        scope = delivery_scope.get()
        if scope is None or scope.queue is not self:
            raise RuntimeError("Physical send must belong to an outbox job")
        key = scope.key
        row = self.store.db.execute(
            "SELECT * FROM parts WHERE job=? AND part=?", (key, name)
        ).fetchone()
        if row:
            if row["payload"] != encode(payload):
                raise ValueError("Delivery part content changed")
            if row["state"] == "done":
                return SimpleNamespace(
                    message_id=json.loads(row["result"])["message_id"]
                )
            if row["state"] == "sending":
                raise DeliveryStopped("needs_review")
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO parts VALUES (?,?,?,'sending',NULL) ON CONFLICT(job,part) DO UPDATE SET state='sending'",
                (key, name, encode(payload)),
            )
        try:
            raw = await operation()
        except (RetryDelivery, PermanentDelivery):
            with self.store.db:
                self.store.db.execute(
                    "UPDATE parts SET state='pending' WHERE job=? AND part=?",
                    (key, name),
                )
            raise
        mid = getattr(raw, "message_id", None)
        if isinstance(raw, (str, int)):
            mid = str(raw)
        with self.store.db:
            self.store.db.execute(
                "UPDATE parts SET state='done',result=? WHERE job=? AND part=?",
                (
                    encode({"message_id": str(mid) if mid is not None else None}),
                    key,
                    name,
                ),
            )
        return raw

    async def run(self):
        targets: set[tuple[str, str]] = set()

        async def recover(row):
            try:
                p = row["payload"]
                await self.chat_lane.run_send(
                    p["channel"], p["chat_id"], lambda: self.deliver(row["id"])
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # The durable state exposes failure; never create a second notification.
                pass

        try:
            while True:
                for kind in tuple(self.handlers):
                    for row in self.store.pending(kind):
                        target = (row["payload"]["channel"], row["payload"]["chat_id"])
                        if (
                            row["id"] in self.active
                            or target in targets
                            or len(self.tasks) >= 32
                        ):
                            continue
                        self.active.add(row["id"])
                        targets.add(target)
                        task = asyncio.create_task(
                            recover(row), name="outbox:" + row["id"]
                        )
                        self.tasks.add(task)
                        task.add_done_callback(self.tasks.discard)
                        task.add_done_callback(
                            lambda _, key=row["id"]: self.active.discard(key)
                        )
                        task.add_done_callback(
                            lambda _, target=target: targets.discard(target)
                        )
                await asyncio.sleep(0.25)
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
