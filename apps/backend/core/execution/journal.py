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

from .contracts import ACTIVE, CONTRACT_VERSION, TERMINAL, ExecutionConflict, ExecutionIdentity, ExecutionNeedsReview, ExecutionUnavailable
from .lease import EpochLease
from .schema import ensure_event_schema
from .projection import project_event, public_state, retry_policy, unresolved, unsafe_effects, ExecutionProjectionError


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
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_ordinary ON executions(session_key)
                    WHERE status IN ('accepted', 'running') AND source!='proactive';
                DROP INDEX IF EXISTS one_active_execution;
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_proactive ON executions(session_key)
                    WHERE status IN ('accepted', 'running') AND source='proactive';
                CREATE TABLE IF NOT EXISTS execution_views (turn_id TEXT PRIMARY KEY, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_checkpoint (id INTEGER PRIMARY KEY CHECK(id=1), seq INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS proactive_claims (session_key TEXT, candidate TEXT, turn_id TEXT,
                    PRIMARY KEY(session_key,candidate));
            """)
            with self._transaction():
                self._conn.execute("INSERT OR IGNORE INTO identity VALUES (1, ?, ?)", (uuid4().hex, CONTRACT_VERSION))
                identity = self._conn.execute("SELECT * FROM identity WHERE id=1").fetchone()
                if identity["version"] != CONTRACT_VERSION:
                    raise RuntimeError("Unsupported execution journal version")
                self.root_id = identity["root_id"]
            self.rebuild_projection()
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
                if hasattr(self, "root_id"):
                    self._sync_projection()
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
        self._sync_projection()

    def _state(self, turn_id: str) -> dict:
        row = self._conn.execute("SELECT state FROM execution_views WHERE turn_id=?", (turn_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown execution")
        return json.loads(row[0])

    def _sync_projection(self):
        checkpoint = self._conn.execute("SELECT seq FROM execution_checkpoint WHERE id=1").fetchone()
        after = checkpoint[0] if checkpoint else 0
        for event in self._conn.execute("SELECT * FROM events WHERE seq>? ORDER BY seq", (after,)).fetchall():
            # Conversation facts have their own strict projection and identities.
            if event["kind"] not in {"conversation.baseline", "conversation.changed"}:
                old = self._conn.execute("SELECT state FROM execution_views WHERE turn_id=?", (event["turn_id"],)).fetchone()
                state = project_event(json.loads(old[0]) if old else None,
                                      {**dict(event), "payload": json.loads(event["payload"])}, self.root_id)
                self._conn.execute("INSERT INTO execution_views VALUES (?,?) ON CONFLICT(turn_id) DO UPDATE SET state=excluded.state",
                                   (state["turn_id"], encode(state)))
                if event['kind'] == 'proactive.candidates.claimed':
                    for candidate in json.loads(event['payload'])['candidates']:
                        prior = self._conn.execute('SELECT turn_id FROM proactive_claims WHERE session_key=? AND candidate=?',
                                                   (state['session_key'], candidate)).fetchone()
                        if prior:
                            owner = self._state(prior['turn_id'])
                            if not (owner['status'] in TERMINAL and owner['status'] != 'completed' and
                                    owner['review'] and owner['review']['decision'] == 'close'):
                                raise ExecutionProjectionError('Candidate already belongs to an unclosed proactive attempt')
                        self._conn.execute('INSERT INTO proactive_claims VALUES (?,?,?) ON CONFLICT(session_key,candidate) DO UPDATE SET turn_id=excluded.turn_id',
                                           (state['session_key'], candidate, state['turn_id']))
                elif event['kind'] == 'proactive.candidates.released':
                    for candidate in json.loads(event['payload'])['candidates']:
                        deleted = self._conn.execute('DELETE FROM proactive_claims WHERE session_key=? AND candidate=? AND turn_id=?',
                                                     (state['session_key'], candidate, state['turn_id']))
                        if deleted.rowcount != 1:
                            raise ExecutionProjectionError('Released candidate does not belong to this attempt')
                self._conn.execute("""INSERT INTO executions(rowid,turn_id,run_id,epoch,session_key,source,request_key,input,status,result)
                    VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(turn_id) DO UPDATE SET status=excluded.status,result=excluded.result""",
                    (state["cursor"], state["turn_id"], state["run_id"], state["epoch"], state["session_key"],
                     state["source"], state["request_key"], encode(state["input"]), state["status"],
                     encode(state["result"]) if state["result"] is not None else None))
            after = event["seq"]
        self._conn.execute("INSERT INTO execution_checkpoint VALUES (1,?) ON CONFLICT(id) DO UPDATE SET seq=excluded.seq", (after,))

    def rebuild_projection(self):
        # One transaction: interruption leaves either the previous view or the
        # complete replacement, never a half rebuilt admission index.
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM executions")
                self._conn.execute("DELETE FROM execution_views")
                self._conn.execute("DELETE FROM execution_checkpoint")
                self._conn.execute("DELETE FROM proactive_claims")
                self._sync_projection()
                # Retry links are atomic facts; a missing successor is corruption.
                for row in self._conn.execute("SELECT state FROM execution_views"):
                    state = json.loads(row[0])
                    if state["retry_turn_id"]:
                        child = self._state(state["retry_turn_id"])
                        if child["retry_of"] != state["turn_id"]:
                            raise ExecutionProjectionError("Invalid retry lineage")
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

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
            # A fresh transport/request id must not bypass pending human review.
            # Only the exact same desktop input is held; distinct requests and
            # external transports retain their existing admission semantics.
            if source == "desktop":
                pending = self._conn.execute(
                    "SELECT turn_id FROM executions WHERE source=? AND session_key=? AND input=? AND status IN ('failed','interrupted','needs_review')",
                    (source, session_key, serialized),
                ).fetchall()
                for candidate in pending:
                    state = self._state(candidate["turn_id"])
                    if unsafe_effects(state) and not state["retry_turn_id"] and not (state["review"] and state["review"]["decision"] == "close"):
                        error = ExecutionNeedsReview if state["status"] == "needs_review" else ExecutionUnavailable
                        raise error("同一输入已有外部操作，请在工作台核对执行记录并处置；请勿直接重发", state)
            identity = ExecutionIdentity(self.root_id, self.epoch, session_key, source, request_key, uuid4().hex, uuid4().hex)
            row = asdict(identity)
            if self._conn.execute("SELECT 1 FROM executions WHERE session_key=? AND status IN ('accepted','running') AND (source='proactive')=?", (session_key, source == 'proactive')).fetchone():
                raise ExecutionConflict("此会话已有接纳的执行，请查询其状态")
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

    def claim_proactive(self, turn_id: str, candidates: list[str]) -> bool:
        """Claim canonical candidates before model work; rebuilt entirely from facts.

        A failed/interrupted attempt requires explicit close before another tick can
        evaluate its candidates. Completed candidates stay consumed by this index.
        """
        if not candidates or any(not isinstance(c, str) or not c for c in candidates):
            raise ValueError('Nonempty proactive candidate identities required')
        with self._transaction():
            row = self._row(turn_id)
            if row['source'] != 'proactive' or row['status'] != 'running' or row['epoch'] != self.epoch:
                raise ExecutionConflict('Proactive attempt is not running')
            for candidate in candidates:
                claim = self._conn.execute('SELECT turn_id FROM proactive_claims WHERE session_key=? AND candidate=?',
                                           (row['session_key'], candidate)).fetchone()
                if claim:
                    prior = self._state(claim['turn_id'])
                    if not (prior['status'] in TERMINAL and prior['status'] != 'completed' and
                            prior['review'] and prior['review']['decision'] == 'close'):
                        return False
            self._event(row, 'proactive.candidates.claimed', {'candidates': sorted(set(candidates))})
            return True

    def available_proactive_candidates(self, session_key: str, candidates: list[str]) -> list[bool]:
        with self._transaction():
            available = []
            for candidate in candidates:
                claim = self._conn.execute('SELECT turn_id FROM proactive_claims WHERE session_key=? AND candidate=?',
                                           (session_key, candidate)).fetchone()
                prior = self._state(claim['turn_id']) if claim else None
                available.append(prior is None or bool(prior['status'] in TERMINAL and
                    prior['status'] != 'completed' and prior['review'] and prior['review']['decision'] == 'close'))
            return available

    def proactive_blocker(self, session_key: str) -> dict | None:
        with self._transaction():
            for row in self._conn.execute("SELECT turn_id FROM executions WHERE session_key=? AND source='proactive' AND status IN ('interrupted','needs_review') ORDER BY rowid", (session_key,)):
                state = self._state(row['turn_id'])
                if not (state['review'] and state['review']['decision'] == 'close'):
                    return public_state(state)
        return None

    def append(self, turn_id: str, kind: str, payload: dict):
        if kind not in {"tool.dispatched", "tool.returned", "delivery.dispatched", "delivery.returned", "reply.delta", "messages.committed", "run.error",
                        "proactive.stage.started", "proactive.stage.completed", "proactive.observed",
                        "proactive.candidates.released",
                        "proactive.model.requested", "proactive.model.returned"}:
            raise ValueError("Unknown execution event")
        with self._transaction():
            row = self._row(turn_id)
            if row["status"] != "running" or row["epoch"] != self.epoch:
                raise ExecutionConflict("Execution is not running in this epoch")
            self._event(row, kind, payload)

    def _unresolved(self, turn_id: str) -> bool:
        return unresolved(self._state(turn_id))

    def finish(self, turn_id: str, status: str, result: dict | None = None):
        if status not in TERMINAL:
            raise ValueError("Not a terminal status")
        with self._transaction():
            row = self._row(turn_id)
            if row["status"] not in ACTIVE or row["epoch"] != self.epoch:
                raise ExecutionConflict("Execution already ended or belongs to another epoch")
            status = "needs_review" if self._unresolved(turn_id) else status
            self._event(row, "run.ended", {"status": status, "result": result})

    def recover_abandoned(self):
        with self._transaction():
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
            finally:
                lease.close()

    def _decode(self, row) -> dict:
        return {**dict(row), "root_id": self.root_id, "input": json.loads(row["input"]),
                "result": json.loads(row["result"]) if row["result"] else None}

    def get(self, turn_id: str) -> dict:
        with self._transaction():
            return public_state(self._state(turn_id))

    def find(self, session_key: str, source: str, request_key: str) -> dict | None:
        with self._transaction():
            row = self._conn.execute("SELECT * FROM executions WHERE session_key=? AND source=? AND request_key=?",
                                     (session_key, source, request_key)).fetchone()
            return public_state(self._state(row["turn_id"])) if row else None

    def list_executions(self, session_key: str = "", *, before: int = 0, limit: int = 50, status: str = "") -> dict:
        if type(before) is not int or before < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid execution page")
        if status and status not in (*ACTIVE, *TERMINAL, "attention"):
            raise ValueError("Invalid execution status")
        with self._transaction():
            rows = self._conn.execute(
                "SELECT rowid AS cursor,turn_id,run_id,epoch,source,request_key,status FROM executions "
                "WHERE (?='' OR session_key=?) AND (?=0 OR rowid<?) "
                "AND (?='' OR status=? OR (?='attention' AND status IN ('failed','interrupted','needs_review'))) "
                "ORDER BY rowid DESC LIMIT ?",
                (session_key, session_key, before, before, status, status, status, limit),
            ).fetchall()
            items = [public_state(self._state(r["turn_id"])) for r in rows]
            return {"executions": items, "next_cursor": rows[-1]["cursor"] if rows else before}

    def events(self, turn_id: str, *, after: int = 0, limit: int = 100) -> dict:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("Invalid event page")
        with self._transaction():
            self._row(turn_id)
            rows = self._conn.execute("SELECT * FROM events WHERE turn_id=? AND seq>? ORDER BY seq LIMIT ?", (turn_id, after, limit)).fetchall()
            events = [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]
            return {"events": events, "next_cursor": events[-1]["seq"] if events else after}

    @staticmethod
    def _check_action(state: dict, expected_seq: int, action_id: str):
        if not isinstance(action_id, str) or not action_id.strip() or len(action_id) > 200:
            raise ValueError("处置标识必须是 1–200 字符的非空字符串")
        if type(expected_seq) is not int or state["last_seq"] != expected_seq:
            raise ExecutionConflict("执行记录已变化，请刷新后再处置")

    def review(self, turn_id: str, *, expected_seq: int, action_id: str, decision: str, note: str) -> dict:
        if decision not in {"close", "no_effect"} or not isinstance(note, str) or not note.strip() or len(note) > 4000:
            raise ValueError("请选择处置结论并填写核对依据（1–4000 字符）")
        payload = {"action_id": action_id, "decision": decision, "note": note.strip()}
        with self._transaction():
            state = self._state(turn_id)
            old = state["actions"].get(action_id)
            if old:
                if old != payload:
                    raise ExecutionConflict("处置标识已用于不同结论")
                return public_state(state)
            self._check_action(state, expected_seq, action_id)
            self._event(state, "execution.reviewed", payload)
            return public_state(self._state(turn_id))

    def admit_retry(self, turn_id: str, *, expected_seq: int, action_id: str) -> tuple[dict, bool]:
        with self._transaction():
            state = self._state(turn_id)
            old = state["actions"].get(action_id)
            if old:
                if "retry_turn_id" not in old:
                    raise ExecutionConflict("处置标识已用于不同操作")
                return public_state(self._state(old["retry_turn_id"])), False
            self._check_action(state, expected_seq, action_id)
            policy = retry_policy(state)
            if not policy["allowed"]:
                raise ExecutionConflict(policy["reason"])
            identity = asdict(ExecutionIdentity(self.root_id, self.epoch, state["session_key"], state["source"],
                              f"retry:{turn_id}", uuid4().hex, uuid4().hex))
            self._event(state, "execution.retried", {"action_id": action_id, "retry_turn_id": identity["turn_id"]})
            self._event(identity, "request.accepted", {"identity": identity, "input": state["input"], "retry_of": turn_id})
            return public_state(self._state(identity["turn_id"])), True

    def close(self):
        if self._closed:
            return
        self._conn.close()
        self._lease.close()
        self._closed = True
