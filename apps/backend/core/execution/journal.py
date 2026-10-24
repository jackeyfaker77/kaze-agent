"""Append-only execution facts and transactionally maintained status index.

Conversation projections share this fact space; model context is not yet replayed here.
All writes commit synchronously before their caller crosses an effect boundary.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any
from uuid import uuid4

from .contracts import ACTIVE, CONTRACT_VERSION, TERMINAL, ExecutionConflict, ExecutionIdentity
from .lease import EpochLease
from .schema import ensure_event_schema


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ExecutionJournal:
    def __init__(self, workspace: Path):
        self.directory = workspace / "private_runtime" / "execution"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.epoch = uuid4().hex
        self._lease = EpochLease(self.directory / f"{self.epoch}.lock")
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(self.directory / "events.sqlite3", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            ensure_event_schema(self._conn)
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS executions (
                    turn_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE,
                    epoch TEXT NOT NULL, session_key TEXT NOT NULL, source TEXT NOT NULL,
                    request_key TEXT NOT NULL, input TEXT NOT NULL, status TEXT NOT NULL,
                    result TEXT, UNIQUE(source, session_key, request_key));
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_execution ON executions(session_key)
                    WHERE status IN ('accepted', 'running');
            """)
            with self._transaction():
                self._conn.execute("INSERT OR IGNORE INTO identity VALUES (1, ?, ?)", (uuid4().hex, CONTRACT_VERSION))
                identity = self._conn.execute("SELECT * FROM identity WHERE id=1").fetchone()
                if identity["version"] != CONTRACT_VERSION:
                    raise RuntimeError("Unsupported execution journal version")
                self.root_id = identity["root_id"]
            self.recover_abandoned()
        except BaseException:
            self._conn.close()
            self._lease.close()
            self._closed = True
            raise

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def _event(self, row, kind: str, payload: dict):
        self._conn.execute(
            "INSERT INTO events(turn_id,run_id,epoch,version,kind,payload,created_at) VALUES (?,?,?,?,?,?,?)",
            (row["turn_id"], row["run_id"], row["epoch"], CONTRACT_VERSION, kind, encode(payload), datetime.now(timezone.utc).isoformat()),
        )

    def admit(self, session_key: str, source: str, request_key: str, payload: dict) -> tuple[dict, bool]:
        if not all(isinstance(s, str) and s.strip() for s in (session_key, source, request_key)):
            raise ValueError("Execution identity must not be empty")
        serialized = encode(payload)
        with self._transaction():
            old = self._conn.execute("SELECT * FROM executions WHERE source=? AND session_key=? AND request_key=?",
                                     (source, session_key, request_key)).fetchone()
            if old is not None:
                if old["input"] != serialized:
                    raise ExecutionConflict("执行请求标识已用于不同内容")
                return self._decode(old), False
            identity = ExecutionIdentity(self.root_id, self.epoch, session_key, source, request_key, uuid4().hex, uuid4().hex)
            row = asdict(identity)
            try:
                self._conn.execute("INSERT INTO executions VALUES (?,?,?,?,?,?,?,?,NULL)",
                    (identity.turn_id, identity.run_id, self.epoch, session_key, source, request_key, serialized, "accepted"))
            except sqlite3.IntegrityError as exc:
                raise ExecutionConflict("此会话已有接纳的执行，请查询其状态") from exc
            self._event(row, "request.accepted", {"identity": row, "input": payload})
            return {**row, "input": payload, "status": "accepted", "result": None}, True

    def _row(self, turn_id: str):
        row = self._conn.execute("SELECT * FROM executions WHERE turn_id=?", (turn_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown execution")
        return row

    def start(self, turn_id: str):
        with self._transaction():
            row = self._row(turn_id)
            if row["status"] != "accepted" or row["epoch"] != self.epoch:
                raise ExecutionConflict("Execution cannot start")
            self._event(row, "run.started", {})
            self._conn.execute("UPDATE executions SET status='running' WHERE turn_id=?", (turn_id,))

    def append(self, turn_id: str, kind: str, payload: dict):
        if kind not in {"tool.dispatched", "tool.returned", "delivery.dispatched", "delivery.returned", "reply.delta", "messages.committed", "run.error"}:
            raise ValueError("Unknown execution event")
        with self._transaction():
            row = self._row(turn_id)
            if row["status"] != "running" or row["epoch"] != self.epoch:
                raise ExecutionConflict("Execution is not running in this epoch")
            self._event(row, kind, payload)

    def _unresolved(self, turn_id: str) -> bool:
        pending: set[str] = set()
        for row in self._conn.execute("SELECT kind,payload FROM events WHERE turn_id=? ORDER BY seq", (turn_id,)):
            payload = json.loads(row["payload"])
            if row["kind"].endswith(".dispatched"):
                pending.add(payload["effect_id"])
            elif row["kind"].endswith(".returned") and payload.get("certainty") == "confirmed":
                pending.discard(payload["effect_id"])
        return bool(pending)

    def finish(self, turn_id: str, status: str, result: dict | None = None):
        if status not in TERMINAL:
            raise ValueError("Not a terminal status")
        with self._transaction():
            row = self._row(turn_id)
            if row["status"] not in ACTIVE or row["epoch"] != self.epoch:
                raise ExecutionConflict("Execution already ended or belongs to another epoch")
            status = "needs_review" if self._unresolved(turn_id) else status
            self._event(row, "run.ended", {"status": status, "result": result})
            self._conn.execute("UPDATE executions SET status=?,result=? WHERE turn_id=?",
                               (status, encode(result) if result is not None else None, turn_id))

    def recover_abandoned(self):
        with self._lock:
            epochs = [r[0] for r in self._conn.execute("SELECT DISTINCT epoch FROM executions WHERE status IN ('accepted','running') AND epoch<>?", (self.epoch,))]
        for epoch in epochs:
            try:
                lease = EpochLease(self.directory / f"{epoch}.lock")
            except (BlockingIOError, PermissionError):
                continue  # The original runtime still owns this epoch.
            try:
                with self._transaction():
                    for row in self._conn.execute("SELECT * FROM executions WHERE epoch=? AND status IN ('accepted','running')", (epoch,)).fetchall():
                        status = "needs_review" if self._unresolved(row["turn_id"]) else "interrupted"
                        self._event(row, "run.recovered", {"status": status, "recovered_by_epoch": self.epoch, "automatic_retry": False})
                        self._conn.execute("UPDATE executions SET status=? WHERE turn_id=?", (status, row["turn_id"]))
            finally:
                lease.close()

    def _decode(self, row) -> dict:
        return {**dict(row), "root_id": self.root_id, "input": json.loads(row["input"]),
                "result": json.loads(row["result"]) if row["result"] else None}

    def get(self, turn_id: str) -> dict:
        with self._lock:
            return self._decode(self._row(turn_id))

    def find(self, session_key: str, source: str, request_key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM executions WHERE session_key=? AND source=? AND request_key=?",
                                     (session_key, source, request_key)).fetchone()
            return self._decode(row) if row else None

    def list_executions(self, session_key: str, *, before: int = 0, limit: int = 50) -> dict:
        if type(before) is not int or before < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid execution page")
        with self._lock:
            rows = self._conn.execute(
                "SELECT rowid AS cursor,turn_id,run_id,epoch,source,request_key,status FROM executions "
                "WHERE session_key=? AND (?=0 OR rowid<?) ORDER BY rowid DESC LIMIT ?",
                (session_key, before, before, limit),
            ).fetchall()
            return {"executions": [dict(r) for r in rows], "next_cursor": rows[-1]["cursor"] if rows else before}

    def events(self, turn_id: str, *, after: int = 0, limit: int = 100) -> dict:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("Invalid event page")
        with self._lock:
            self._row(turn_id)
            rows = self._conn.execute("SELECT * FROM events WHERE turn_id=? AND seq>? ORDER BY seq LIMIT ?", (turn_id, after, limit)).fetchall()
            events = [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]
            return {"events": events, "next_cursor": events[-1]["seq"] if events else after}

    def close(self):
        if self._closed:
            return
        self._conn.close()
        self._lease.close()
        self._closed = True
