"""Adapters for existing Wake persistence, scheduler occurrences and child results."""

from __future__ import annotations

from datetime import datetime
import json
import logging
import sqlite3

from .contracts import digest

logger = logging.getLogger(__name__)


def timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Source ordering timestamp requires a timezone")
    return int(parsed.timestamp() * 1_000_000)


def wake_identity(kind, event):
    route = str(
        event.get("_reservoir_ack_source_id")
        or event.get("ack_server")
        or event.get("_source")
        or ""
    )
    item = (
        str(
            event.get("_reservoir_source_event_id")
            or event.get("event_id")
            or event.get("id")
            or ""
        )
        if kind != "context"
        else route
    )
    if not route or not item:
        raise ValueError("Wake input requires its registered route and item identity")
    revision = event.get("revision", event.get("version"))
    order = event.get("source_sequence")
    if order is None and (
        type(revision) is int or (isinstance(revision, str) and revision.isdecimal())
    ):
        order = int(revision)
    stamp = next(
        (
            event[k]
            for k in ("updated_at", "observed_at", "published_at", "triggered_at")
            if event.get(k)
        ),
        None,
    )
    if order is None:
        order = timestamp(stamp) if stamp else 0
    # Transport/observation metadata must never create new content revisions.
    payload = {
        k: v
        for k, v in event.items()
        if k
        not in {
            "first_seen_at",
            "preprocess_score",
            "rank_score",
            "embedding",
            "ack_server",
            "ack_tool",
            "ack_args",
            "_event_embedding",
            "item_id",
            "id",
        }
        and not k.startswith("_reservoir_")
    }
    if kind != "context":
        payload["event_id"] = item
    version = str(revision) if revision is not None else digest(payload)
    return "wake." + kind, route, item, version, order


class GoalInputSources:
    def __init__(self, inputs, scheduler=None):
        self.inputs = inputs
        self.store = inputs.store
        self.scheduler = scheduler
        self._wake_validators = {}
        self._wake_error = None

    def bind_wake_validator(self, session_key, validator):
        self._wake_validators[session_key] = validator

    def unbind_wake_validator(self, session_key, validator):
        if self._wake_validators.get(session_key) is validator:
            del self._wake_validators[session_key]

    def subscribe(self, subscription_id, goal_id, **options):
        """Host adapter entry: baseline persisted Wake data, including while off.

        Baselines only suppress historical data; they cannot authorize execution
        and therefore do not require an external validation call.
        """
        baseline = []
        if options["kind"].startswith("wake."):
            for kind, event in self._wake_rows({options["source_id"]}):
                try:
                    identity = wake_identity(kind, event)
                except ValueError:
                    continue
                if (
                    identity[0] == options["kind"]
                    and identity[1] == options["source_id"]
                    and (
                        not options.get("item_id") or options["item_id"] == identity[2]
                    )
                ):
                    baseline.append(
                        {
                            "item_id": identity[2],
                            "position": identity[4],
                            "payload": event,
                        }
                    )
        return self.inputs.subscribe(
            subscription_id,
            goal_id,
            baseline=baseline,
            baseline_loader=(
                lambda: [
                    row
                    for row in self._result_rows({options["source_id"]})
                    if not options.get("item_id")
                    or row["item_id"] == options["item_id"]
                ]
            )
            if options["kind"] == "result"
            else None,
            **options,
        )

    def _wanted(self, kind=None):
        with self.inputs.journal._transaction():
            rows = self.inputs.journal._conn.execute(
                "SELECT * FROM goal_input_subscriptions WHERE active=1"
            ).fetchall()
            return [dict(r) for r in rows if kind is None or r["kind"] == kind]

    def _known(self, identity, session_key):
        with self.inputs.journal._transaction():
            return (
                self.inputs.journal._conn.execute(
                    "SELECT 1 FROM goal_input_events WHERE kind=? AND source_id=? AND item_id=? AND version=? AND owner_session=?",
                    (*identity[:4], session_key),
                ).fetchone()
                is not None
            )

    async def capture_wake(self, session_key, kind, event, now):
        identity = wake_identity(kind, event)
        wanted = [
            s
            for s in self._wanted(identity[0])
            if s["source_id"] == identity[1]
            and s["owner_session"] == session_key
            and (not s["item_id"] or s["item_id"] == identity[2])
        ]
        if not wanted:
            # Historical ownership still fences already consumed notifications.
            return self.inputs.owns_input(session_key, *identity[:4], event)
        if not self._known(identity, session_key):
            validator = self._wake_validators.get(session_key)
            if validator is None or not await validator(kind, event, now):
                return False
            self.inputs.publish(*identity, event, owner_session=session_key)
        return self.inputs.owns_input(session_key, *identity[:4], event)

    def _wake_rows(self, routes):
        path = self.store.workspace / "wake_proactive.db"
        if not path.is_file():
            return []
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in routes)
        try:
            events = [
                (
                    r["kind"],
                    {
                        **json.loads(r["payload_json"]),
                        "event_id": r["source_event_id"],
                        "ack_server": r["ack_source_id"],
                    },
                )
                for r in conn.execute(
                    f"SELECT kind,payload_json,source_event_id,ack_source_id FROM reservoir_events WHERE ack_source_id IN ({placeholders})",
                    tuple(routes),
                )
            ]
            contexts = [
                ("context", json.loads(r[0]))
                for r in conn.execute(
                    f"SELECT payload_json FROM context_state WHERE source_id IN ({placeholders})",
                    tuple(routes),
                )
            ]
        finally:
            conn.close()
        return events + contexts

    async def poll_wake(self, now):
        wanted = [s for s in self._wanted() if s["kind"].startswith("wake.")]
        if not wanted:
            return
        try:
            events = self._wake_rows({s["source_id"] for s in wanted})
            self._wake_error = None
        except (sqlite3.Error, ValueError) as exc:
            # A broken Wake store must not block unrelated timer/result goals.
            if self._wake_error != type(exc).__name__:
                logger.warning("Goal Wake input unavailable: %s", type(exc).__name__)
                self._wake_error = type(exc).__name__
            return
        for session in {s["owner_session"] for s in wanted}:
            for kind, payload in events:
                try:
                    await self.capture_wake(session, kind, payload, now)
                except ValueError:
                    # Invalid or unorderable source data never grants execution.
                    continue

    def owns_timer(self, job):
        return self.inputs.owns_input(
            job.session_key,
            "timer",
            job.id,
            job.id,
            job.fire_at.isoformat(),
            {
                "job_id": job.id,
                "scheduled_at": job.fire_at.isoformat(),
                "session_key": job.session_key,
            },
        )

    def capture_timer(self, job, now):
        if not self.owns_timer(job):
            return False
        if job.fire_at > now:
            return True
        self.inputs.publish(
            "timer",
            job.id,
            job.id,
            job.fire_at.isoformat(),
            timestamp(job.fire_at),
            {
                "job_id": job.id,
                "scheduled_at": job.fire_at.isoformat(),
                "session_key": job.session_key,
            },
            owner_session=job.session_key,
        )
        return True

    def recover_timers(self, jobs, now):
        # Run before ordinary misfire policy discards/advances persisted reminders.
        for job in jobs:
            if job.enabled:
                self.capture_timer(job, now)

    def _result_rows(self, wanted):
        """Snapshot committed result facts inside the caller's journal transaction."""
        journal = self.inputs.journal
        completions = [
            json.loads(r[0])
            for r in journal._conn.execute("SELECT payload FROM spawn_completions")
        ]
        phases = [
            dict(r)
            for r in journal._conn.execute(
                "SELECT turn_id,request_key FROM executions WHERE source='goal' AND status NOT IN ('accepted','running')"
            )
        ]
        rows = []
        for completion in completions:
            event = completion["event"]
            if event["job_id"] not in wanted:
                continue
            record = journal._state(event["child_turn_id"])
            rows.append(
                {
                    "source_id": event["job_id"],
                    "item_id": event["child_turn_id"],
                    "position": record["last_seq"],
                    "payload": {**event, "execution_status": record["status"]},
                }
            )
        for phase in phases:
            if phase["request_key"] not in wanted:
                continue
            record = journal._state(phase["turn_id"])
            rows.append(
                {
                    "source_id": phase["request_key"],
                    "item_id": phase["turn_id"],
                    "position": record["last_seq"],
                    "payload": {
                        "child_turn_id": phase["turn_id"],
                        "job_id": phase["request_key"],
                        "execution_status": record["status"],
                        "result": record["result"],
                    },
                }
            )
        return rows

    def poll_results(self):
        subscriptions = self._wanted("result")
        wanted = {s["source_id"] for s in subscriptions}
        if not wanted:
            return
        with self.inputs.journal._transaction():
            rows = self._result_rows(wanted)
        for row in rows:
            owners = {
                s["owner_session"]
                for s in subscriptions
                if s["source_id"] == row["source_id"]
            }
            for owner in owners:
                self.inputs.publish(
                    "result",
                    row["source_id"],
                    row["item_id"],
                    str(row["position"]),
                    row["position"],
                    row["payload"],
                    owner_session=owner,
                )

    async def poll(self, now):
        await self.poll_wake(now)
        if self.scheduler is not None:
            self.recover_timers(self.scheduler.list_jobs(), now)
        self.poll_results()
        self.inputs.consume()
