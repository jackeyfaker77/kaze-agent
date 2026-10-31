"""Durable, Host-owned source subscriptions in the existing execution database.

Input capture never invokes a model or a delivery channel. Receipt, cursor and
Goal observation commit together; the Goal controller owns subsequent execution.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable

from core.execution.journal import encode
from .contracts import GoalConflict, TERMINAL, check_id, digest, utcnow

KINDS = {"wake.alert", "wake.content", "wake.context", "timer", "result"}


def field(payload, path):
    value = payload
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return {"missing": True}
        value = value[part]
    return {"value": value}


def projection(payload, fields):
    return {path: field(payload, path) for path in fields}


def matches(payload, filters):
    return all(
        field(payload, path) == {"value": value} for path, value in filters.items()
    )


class GoalInputs:
    def __init__(self, store):
        self.store, self.journal = store, store.journal
        self._signal: Callable[[], None] | None = None
        with self.journal._lock:
            self.journal._conn.executescript("""
                CREATE TABLE IF NOT EXISTS goal_input_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    source_id TEXT NOT NULL, item_id TEXT NOT NULL, version TEXT NOT NULL,
                    position INTEGER NOT NULL, payload TEXT NOT NULL, stale INTEGER NOT NULL,
                    created_at TEXT NOT NULL, owner_session TEXT NOT NULL,
                    UNIQUE(owner_session,kind,source_id,item_id,version));
                CREATE TABLE IF NOT EXISTS goal_input_heads (
                    kind TEXT, source_id TEXT, item_id TEXT, position INTEGER NOT NULL,
                    version TEXT NOT NULL, owner_session TEXT NOT NULL,
                    PRIMARY KEY(owner_session,kind,source_id,item_id));
                CREATE TABLE IF NOT EXISTS goal_input_subscriptions (
                    subscription_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL,
                    kind TEXT NOT NULL, source_id TEXT NOT NULL, item_id TEXT NOT NULL,
                    fields TEXT NOT NULL, filters TEXT NOT NULL, active INTEGER NOT NULL,
                    cursor INTEGER NOT NULL, fingerprint TEXT, owner_session TEXT NOT NULL,
                    created_at TEXT NOT NULL, positions TEXT NOT NULL, cancelled_at TEXT,
                    fingerprints TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS goal_input_receipts (
                    subscription_id TEXT NOT NULL, event_seq INTEGER NOT NULL,
                    outcome TEXT NOT NULL, goal_version INTEGER NOT NULL,
                    input_version TEXT, created_at TEXT NOT NULL,
                    PRIMARY KEY(subscription_id,event_seq));
                CREATE INDEX IF NOT EXISTS goal_input_route ON goal_input_events(kind,source_id,seq);
                CREATE INDEX IF NOT EXISTS goal_subscription_route ON goal_input_subscriptions(active,kind,source_id);
                CREATE TRIGGER IF NOT EXISTS goal_inputs_no_update BEFORE UPDATE ON goal_input_events
                    BEGIN SELECT RAISE(ABORT,'goal inputs are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS goal_inputs_no_delete BEFORE DELETE ON goal_input_events
                    BEGIN SELECT RAISE(ABORT,'goal inputs are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS goal_receipts_no_update BEFORE UPDATE ON goal_input_receipts
                    BEGIN SELECT RAISE(ABORT,'goal receipts are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS goal_receipts_no_delete BEFORE DELETE ON goal_input_receipts
                    BEGIN SELECT RAISE(ABORT,'goal receipts are immutable'); END;
            """)

    def bind_signal(self, signal):
        self._signal = signal

    def _highwater(self):
        return self.journal._conn.execute(
            "SELECT coalesce(max(seq),0) FROM goal_input_events"
        ).fetchone()[0]

    def _subscriptions(self, kind, source_id, item_id, owner_session):
        return self.journal._conn.execute(
            "SELECT * FROM goal_input_subscriptions WHERE active=1 AND kind=? AND source_id=? "
            "AND (item_id='' OR item_id=?) AND owner_session=?",
            (kind, source_id, item_id, owner_session),
        ).fetchall()

    def publish(
        self, kind, source_id, item_id, version, position, payload, *, owner_session=""
    ):
        """Capture one committed source revision. Position is a source-owned integer.

        A provider with only opaque revisions must supply a stable timestamp or
        sequence in its adapter. Arrival order is never treated as source order.
        """
        if kind not in KINDS or any(
            not isinstance(x, str) or not x for x in (source_id, item_id, version)
        ):
            raise ValueError("Stable kind/source/item/version identities required")
        if type(position) is not int or not 0 <= position <= 2**63 - 1:
            raise ValueError("Source position must be a nonnegative SQLite integer")
        if not isinstance(payload, dict):
            raise ValueError("Input payload must be an object")
        if not isinstance(owner_session, str) or (
            (kind.startswith("wake.") or kind == "timer") and not owner_session
        ):
            raise ValueError(
                "Wake and timer capture requires its validated owner session"
            )
        encoded = encode(payload)
        if len(encoded.encode()) > 256_000:
            raise ValueError("Goal input exceeds 256 KB")
        signal = False
        with self.journal._transaction():
            conn = self.journal._conn
            prior = conn.execute(
                "SELECT seq FROM goal_input_events WHERE kind=? AND source_id=? "
                "AND item_id=? AND version=? AND owner_session=?",
                (kind, source_id, item_id, version, owner_session),
            ).fetchone()
            if prior:
                return prior[0]
            head = conn.execute(
                "SELECT * FROM goal_input_heads WHERE kind=? AND source_id=? AND item_id=? AND owner_session=?",
                (kind, source_id, item_id, owner_session),
            ).fetchone()
            stale = bool(head and position <= head["position"])
            seq = conn.execute(
                "INSERT INTO goal_input_events(kind,source_id,item_id,version,position,payload,stale,created_at,owner_session) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    kind,
                    source_id,
                    item_id,
                    version,
                    position,
                    encoded,
                    int(stale),
                    utcnow(),
                    owner_session,
                ),
            ).lastrowid
            if not stale:
                conn.execute(
                    "INSERT INTO goal_input_heads VALUES (?,?,?,?,?,?) ON CONFLICT(owner_session,kind,source_id,item_id) "
                    "DO UPDATE SET position=excluded.position,version=excluded.version",
                    (kind, source_id, item_id, position, version, owner_session),
                )
                for sub in self._subscriptions(kind, source_id, item_id, owner_session):
                    if matches(payload, json.loads(sub["filters"])):
                        changed = digest(
                            projection(payload, json.loads(sub["fields"]))
                        ) != json.loads(sub["fingerprints"]).get(item_id)
                        state = self.store._read(sub["goal_id"])
                        signal |= changed and state["status"] not in TERMINAL
        if signal and self._signal:
            self._signal()
        return seq

    def subscribe(
        self,
        subscription_id,
        goal_id,
        *,
        kind,
        source_id,
        fields,
        expected_version,
        item_id="",
        filters=None,
        owner_session="",
        baseline=None,
        baseline_loader=None,
    ):
        """Explicit Host subscription. Old captured inputs establish a baseline.

        Subscription IDs are never reusable, including after cancellation. A new
        subscription starts at the current highwater and cannot resurrect backlog.
        """
        check_id(subscription_id)
        if kind not in KINDS or not isinstance(source_id, str) or not source_id:
            raise ValueError("A known source kind and identity are required")
        if (
            not isinstance(fields, list)
            or not fields
            or len(fields) > 32
            or any(
                not isinstance(p, str) or not p or any(not s for s in p.split("."))
                for p in fields
            )
        ):
            raise ValueError("Explicit relevant field paths are required")
        if not isinstance(item_id, str) or not isinstance(owner_session, str):
            raise ValueError("Item and session identities must be strings")
        if (kind.startswith("wake.") or kind == "timer") and not owner_session:
            raise ValueError(
                "Wake and timer subscriptions require their existing owner session"
            )
        filters = json.loads(encode({} if filters is None else filters))
        if not isinstance(filters, dict) or any(
            not isinstance(p, str) or not p for p in filters
        ):
            raise ValueError("Filters must map field paths to exact JSON values")
        with self.journal._transaction():
            conn = self.journal._conn
            if conn.execute(
                "SELECT 1 FROM goal_input_subscriptions WHERE subscription_id=?",
                (subscription_id,),
            ).fetchone():
                raise GoalConflict(
                    "Subscription identity already exists; cancellation is permanent"
                )
            state = self.store._read(goal_id)
            self.store._expected(state, expected_version)
            if state["status"] != "ready" or state["active_phase"]:
                raise GoalConflict(
                    "Only an idle ready goal can establish a subscription wait"
                )
            selected_baseline = {}
            positions = {}
            rows = conn.execute(
                "SELECT * FROM goal_input_events WHERE kind=? AND source_id=? "
                "AND (?='' OR item_id=?) AND owner_session=? AND stale=0 ORDER BY position,seq",
                (kind, source_id, item_id, item_id, owner_session),
            ).fetchall()
            snapshots = [
                {
                    "item_id": r["item_id"],
                    "position": r["position"],
                    "payload": json.loads(r["payload"]),
                }
                for r in rows
            ]
            snapshots.extend(baseline or [])
            if baseline_loader is not None:
                # Read authoritative result facts under this same transaction.
                snapshots.extend(baseline_loader())
            for row in sorted(snapshots, key=lambda r: r["position"]):
                payload = row["payload"]
                positions[row["item_id"]] = max(
                    positions.get(row["item_id"], -1), row["position"]
                )
                if matches(payload, filters):
                    selected_baseline[row["item_id"]] = projection(payload, fields)
            fingerprints = {
                item: digest(value) for item, value in selected_baseline.items()
            }
            fingerprint = digest(fingerprints) if fingerprints else None
            if len(encode(selected_baseline).encode()) > 256_000:
                raise ValueError("Goal baseline exceeds 256 KB")
            conn.execute(
                "INSERT INTO goal_input_subscriptions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
                (
                    subscription_id,
                    goal_id,
                    kind,
                    source_id,
                    item_id,
                    encode(fields),
                    encode(filters),
                    1,
                    self._highwater(),
                    fingerprint,
                    owner_session,
                    utcnow(),
                    encode(positions),
                    encode(fingerprints),
                ),
            )
            alias = "input:" + subscription_id
            if fingerprint is not None:
                self.store._observe_source(
                    state, alias, fingerprint, source_input=selected_baseline
                )
            self.store._wait(state, {"kind": "source", "source_id": alias})
        return self.get(subscription_id)

    def get(self, subscription_id):
        with self.journal._transaction():
            row = self.journal._conn.execute(
                "SELECT * FROM goal_input_subscriptions WHERE subscription_id=?",
                (subscription_id,),
            ).fetchone()
            if row is None:
                raise KeyError("Unknown subscription")
            return {
                **dict(row),
                "fields": json.loads(row["fields"]),
                "filters": json.loads(row["filters"]),
                "positions": json.loads(row["positions"]),
                "fingerprints": json.loads(row["fingerprints"]),
            }

    def cancel(self, subscription_id):
        with self.journal._transaction():
            conn = self.journal._conn
            changed = conn.execute(
                "UPDATE goal_input_subscriptions SET active=0,cancelled_at=?,cursor=? "
                "WHERE subscription_id=? AND active=1",
                (utcnow(), self._highwater(), subscription_id),
            )
            return changed.rowcount == 1

    def receipts(self, subscription_id):
        with self.journal._transaction():
            return [
                dict(r)
                for r in self.journal._conn.execute(
                    "SELECT * FROM goal_input_receipts WHERE subscription_id=? ORDER BY event_seq",
                    (subscription_id,),
                )
            ]

    def owns_input(self, session_key, kind, source_id, item_id, version, payload):
        """Only explicit subscriptions in this session own execution input.

        The cancelled cursor retains ownership of already captured input after
        unsubscribe; no channel send or delivery receipt is manufactured here.
        """
        with self.journal._transaction():
            for sub in self.journal._conn.execute(
                "SELECT * FROM goal_input_subscriptions WHERE kind=? AND source_id=? "
                "AND (item_id='' OR item_id=?) AND owner_session=?",
                (kind, source_id, item_id, session_key),
            ):
                if not matches(payload, json.loads(sub["filters"])):
                    continue
                if sub["active"]:
                    return True
                captured = self.journal._conn.execute(
                    "SELECT 1 FROM goal_input_events WHERE kind=? AND source_id=? AND item_id=? AND version=? AND seq<=? AND owner_session=?",
                    (kind, source_id, item_id, version, sub["cursor"], session_key),
                ).fetchone()
                if captured:
                    return True
        return False

    def consume(self):
        """Advance each cursor and Goal observation in the same transaction.

        Draining coalesces changes before the serial controller claims a phase.
        There is no await or model call inside this transaction.
        """
        applied = 0
        with self.journal._transaction():
            conn = self.journal._conn
            for subrow in conn.execute(
                "SELECT * FROM goal_input_subscriptions WHERE active=1 ORDER BY rowid"
            ).fetchall():
                sub = dict(subrow)
                if sub["cursor"] == self._highwater():
                    continue
                positions = json.loads(sub["positions"])
                fingerprints = json.loads(sub["fingerprints"])
                state = self.store._read(sub["goal_id"])
                rows = conn.execute(
                    "SELECT * FROM goal_input_events WHERE seq>? AND kind=? AND source_id=? "
                    "AND (?='' OR item_id=?) AND owner_session=? ORDER BY seq",
                    (
                        sub["cursor"],
                        sub["kind"],
                        sub["source_id"],
                        sub["item_id"],
                        sub["item_id"],
                        sub["owner_session"],
                    ),
                ).fetchall()
                for event in rows:
                    payload = json.loads(event["payload"])
                    selected = projection(payload, json.loads(sub["fields"]))
                    fingerprint = digest(selected)
                    outcome = "applied"
                    if event["stale"]:
                        outcome = "stale"
                    elif event["position"] <= positions.get(event["item_id"], -1):
                        outcome = "baseline"
                    elif not matches(payload, json.loads(sub["filters"])):
                        outcome = "filtered"
                    elif (
                        event["kind"] == "result"
                        and payload.get("execution_status") != "completed"
                    ):
                        outcome = "result_unresolved"
                    elif state["status"] in TERMINAL:
                        outcome = "goal_terminal"
                    elif fingerprint == fingerprints.get(event["item_id"]):
                        outcome = "unchanged"
                    if outcome == "applied" and payload.get("expires_at"):
                        try:
                            if datetime.fromisoformat(
                                payload["expires_at"]
                            ) <= datetime.now(timezone.utc):
                                outcome = "expired"
                        except (TypeError, ValueError):
                            outcome = "invalid_expiry"
                    if outcome == "applied":
                        fingerprints[event["item_id"]] = fingerprint
                        aggregate = digest(fingerprints)
                        envelope = {
                            "kind": event["kind"],
                            "source_id": event["source_id"],
                            "item_id": event["item_id"],
                            "version": event["version"],
                            "position": event["position"],
                            "event_seq": event["seq"],
                            "fields": selected,
                        }
                        self.store._observe_source(
                            state,
                            "input:" + sub["subscription_id"],
                            aggregate,
                            source_input=envelope,
                        )
                        sub["fingerprint"] = aggregate
                        applied += 1
                    conn.execute(
                        "INSERT INTO goal_input_receipts VALUES (?,?,?,?,?,?)",
                        (
                            sub["subscription_id"],
                            event["seq"],
                            outcome,
                            state["version"],
                            fingerprint if outcome == "applied" else None,
                            utcnow(),
                        ),
                    )
                    sub["cursor"] = event["seq"]
                    positions[event["item_id"]] = max(
                        positions.get(event["item_id"], -1), event["position"]
                    )
                conn.execute(
                    "UPDATE goal_input_subscriptions SET cursor=?,fingerprint=?,positions=?,fingerprints=? WHERE subscription_id=?",
                    (
                        self._highwater(),
                        sub["fingerprint"],
                        encode(positions),
                        encode(fingerprints),
                        sub["subscription_id"],
                    ),
                )
        return applied
