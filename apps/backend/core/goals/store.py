"""Goal authority in Runtime Event Log, with disposable transactional projections.

Only Host code receives this API. No goal-creation, grant or cancellation tool is
registered with a model. All effect boundaries reserve before leaving SQLite.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from core.execution.contracts import CONTRACT_VERSION
from core.execution.journal import encode
from .contracts import (
    ACTION_KINDS,
    GoalBlocked,
    GoalConflict,
    KINDS,
    TERMINAL,
    check_id,
    utcnow,
    validate_authorization,
    validate_definition,
)


class GoalStore:
    def __init__(self, journal, workspace: Path):
        self.journal = journal
        self.workspace = workspace.resolve()
        with journal._lock:
            journal._conn.executescript("""
                CREATE TABLE IF NOT EXISTS goal_views (
                    goal_id TEXT PRIMARY KEY, state TEXT NOT NULL, seq INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS goal_checkpoint (
                    id INTEGER PRIMARY KEY CHECK(id=1), seq INTEGER NOT NULL);
            """)
        with journal._transaction():
            self._sync()

    def _sync(self):
        conn = self.journal._conn
        row = conn.execute("SELECT seq FROM goal_checkpoint WHERE id=1").fetchone()
        after = row[0] if row else 0
        for event in conn.execute(
            "SELECT * FROM events WHERE kind='goal.state' AND seq>? ORDER BY seq",
            (after,),
        ).fetchall():
            payload = json.loads(event["payload"])
            state = payload["state"]
            prior = conn.execute(
                "SELECT state FROM goal_views WHERE goal_id=?", (state["goal_id"],)
            ).fetchone()
            version = json.loads(prior[0])["version"] if prior else 0
            if (
                payload.get("schema_version") != 1
                or payload.get("root_id") != self.journal.root_id
                or state["version"] != version + 1
                or event["turn_id"] != "goal:" + state["goal_id"]
            ):
                raise GoalConflict("Invalid goal event prefix; dispatch is disabled")
            conn.execute(
                "INSERT INTO goal_views VALUES (?,?,?) ON CONFLICT(goal_id) "
                "DO UPDATE SET state=excluded.state,seq=excluded.seq",
                (state["goal_id"], encode(state), event["seq"]),
            )
            after = event["seq"]
        conn.execute(
            "INSERT INTO goal_checkpoint VALUES (1,?) ON CONFLICT(id) DO UPDATE SET seq=excluded.seq",
            (after,),
        )

    def _read(self, goal_id):
        self._sync()
        row = self.journal._conn.execute(
            "SELECT state FROM goal_views WHERE goal_id=?", (goal_id,)
        ).fetchone()
        if row is None:
            raise KeyError("Unknown goal")
        return json.loads(row[0])

    def _save(self, state, reason):
        state["version"] += 1
        state["updated_at"] = utcnow()
        self.journal._conn.execute(
            "INSERT INTO events(turn_id,run_id,epoch,version,kind,payload,created_at) VALUES (?,?,?,?,?,?,?)",
            (
                "goal:" + state["goal_id"],
                "goal:" + state["goal_id"],
                self.journal.epoch,
                CONTRACT_VERSION,
                "goal.state",
                encode(
                    {
                        "schema_version": 1,
                        "root_id": self.journal.root_id,
                        "reason": reason,
                        "state": state,
                    }
                ),
                utcnow(),
            ),
        )
        self._sync()

    @staticmethod
    def _expected(state, version):
        if type(version) is not int or state["version"] != version:
            raise GoalConflict("Goal version changed; refresh before advancing")

    def create(
        self,
        goal_id: str,
        definition: dict,
        authorization: dict,
        *,
        parent_execution_id="",
    ):
        check_id(goal_id)
        definition = validate_definition(definition)
        authorization = validate_authorization(authorization)
        artifact_dir = self.artifact_dir(goal_id)
        if Path(definition["input_dir"]).is_relative_to(
            artifact_dir
        ) or artifact_dir.is_relative_to(Path(definition["input_dir"])):
            raise ValueError(
                "Trusted inputs and writable artifacts must be separate directories"
            )
        with self.journal._transaction():
            self._sync()
            old = self.journal._conn.execute(
                "SELECT state FROM goal_views WHERE goal_id=?", (goal_id,)
            ).fetchone()
            if old:
                state = json.loads(old[0])
                if (
                    state["definitions"][0] != definition
                    or state["authorization"] != authorization
                    or state["parent_execution_id"] != parent_execution_id
                ):
                    raise GoalConflict("Goal ID already belongs to another request")
                return state
            if parent_execution_id:
                self.journal._state(parent_execution_id)
            state = {
                "goal_id": goal_id,
                "version": 0,
                "definition_version": 1,
                "definition": definition,
                "definitions": [definition],
                "authorization": authorization,
                "authorization_version": 1,
                "cancel_version": 0,
                "parent_execution_id": parent_execution_id,
                "status": "ready",
                "step": "execute",
                "wait": None,
                "observed_sources": {},
                "attempts": [],
                "phases": [],
                "active_phase": None,
                "reservations": {},
                "evidence": [],
                "block": None,
                "reason": "explicit_host_authorization",
                "created_at": utcnow(),
            }
            from .contracts import digest
            from .validators import input_versions

            state["observed_sources"]["__local_files__"] = digest(
                input_versions(state, artifact_dir)
            )
            self._save(state, "created")
            return state

    def artifact_dir(self, goal_id):
        check_id(goal_id)
        root = (self.workspace / "goal-runs").resolve()
        if not root.is_relative_to(self.workspace):
            raise ValueError("Goal artifact root escaped the workspace")
        target = (root / goal_id / "artifacts").resolve()
        if not target.is_relative_to(root.resolve()):
            raise ValueError("Goal artifact directory escaped its root")
        return target

    @staticmethod
    def budget(state):
        used = {key: 0 for key in ("total", *KINDS)}
        held = dict(used)
        for item in state["reservations"].values():
            bucket = used if item["status"] == "settled" else held
            bucket["total"] += item["units"]
            bucket[item["kind"]] += item["units"]
        return {
            "limits": state["authorization"]["limits"],
            "used": used,
            "held": held,
            "remaining": {
                k: v - used[k] - held[k]
                for k, v in state["authorization"]["limits"].items()
            },
        }

    def get(self, goal_id):
        with self.journal._transaction():
            state = self._read(goal_id)
            return {
                **state,
                "budget": self.budget(state),
                "artifact_dir": str(self.artifact_dir(goal_id)),
            }

    def list(self):
        with self.journal._transaction():
            self._sync()
            return [
                json.loads(r[0])
                for r in self.journal._conn.execute(
                    "SELECT state FROM goal_views ORDER BY rowid"
                )
            ]

    def history(self, goal_id):
        with self.journal._transaction():
            self._read(goal_id)
            return [
                {**dict(r), "payload": json.loads(r["payload"])}
                for r in self.journal._conn.execute(
                    "SELECT * FROM events WHERE turn_id=? AND kind='goal.state' ORDER BY seq",
                    ("goal:" + goal_id,),
                )
            ]

    def rebuild(self):
        with self.journal._transaction():
            self.journal._conn.execute("DELETE FROM goal_views")
            self.journal._conn.execute("DELETE FROM goal_checkpoint")
            self._sync()

    def cancel(self, goal_id, *, expected_version, revoke=False):
        with self.journal._transaction():
            state = self._read(goal_id)
            self._expected(state, expected_version)
            state["cancel_version"] += 1
            if revoke:
                state["authorization_version"] += 1
            state.update(
                status="cancelled",
                wait=None,
                reason="authorization_revoked" if revoke else "cancelled_by_host",
            )
            self._save(state, state["reason"])
            return state

    def revise(self, goal_id, definition, *, expected_version):
        definition = validate_definition(definition)
        with self.journal._transaction():
            state = self._read(goal_id)
            self._expected(state, expected_version)
            if state["active_phase"] or state["status"] in TERMINAL:
                raise GoalConflict(
                    "Cannot revise an active, cancelled or unresolved goal"
                )
            if definition["input_dir"] != state["definition"]["input_dir"]:
                raise GoalConflict(
                    "Changing the input root requires new explicit authorization"
                )
            state["definition_version"] += 1
            state["definition"] = definition
            state["definitions"].append(definition)
            state.update(
                status="ready", step="execute", wait=None, reason="definition_revised"
            )
            self._save(state, state["reason"])
            return state

    def wait(self, goal_id, condition, *, expected_version):
        condition = json.loads(encode(condition))
        if condition.get("kind") == "time":
            parsed = datetime.fromisoformat(condition["until"])
            if parsed.tzinfo is None:
                raise ValueError("Wait deadline needs a timezone")
        elif condition.get("kind") == "source":
            if (
                not isinstance(condition.get("source_id"), str)
                or not condition["source_id"]
            ):
                raise ValueError("Waiting requires a source identity")
        else:
            raise ValueError("Only time and source waits are supported")
        with self.journal._transaction():
            state = self._read(goal_id)
            self._expected(state, expected_version)
            return self._wait(state, condition)

    def _wait(self, state, condition):
        """Transaction-local entry used by atomic subscription creation."""
        if state["status"] != "ready" or state["active_phase"]:
            raise GoalConflict("Only a ready goal can wait")
        if condition["kind"] == "source":
            condition["after_version"] = state["observed_sources"].get(
                condition["source_id"]
            )
        state.update(status="waiting", wait=condition, reason="waiting_for_condition")
        self._save(state, "waiting")
        return state

    def observe_source(
        self, goal_id, source_id, source_version, *, expected_version=None
    ):
        if (
            not isinstance(source_id, str)
            or not source_id
            or not isinstance(source_version, str)
        ):
            raise ValueError("Source identity/version must be strings")
        with self.journal._transaction():
            state = self._read(goal_id)
            if expected_version is not None:
                self._expected(state, expected_version)
            return self._observe_source(state, source_id, source_version)

    def _observe_source(self, state, source_id, source_version, *, source_input=None):
        """Transaction-local observation; input receipt joins this same commit."""
        if (
            state["status"] in TERMINAL
            or state["observed_sources"].get(source_id) == source_version
        ):
            return state
        state["observed_sources"][source_id] = source_version
        if source_input is not None:
            state.setdefault("source_inputs", {})[source_id] = source_input
        condition = state["wait"] or {}
        wake = (
            condition.get("kind") == "source"
            and condition.get("source_id") == source_id
            and condition.get("after_version") != source_version
        )
        if state["status"] == "succeeded":
            state.update(
                status="ready", step="verify", reason="acceptance_inputs_changed"
            )
        elif state["status"] == "waiting" and wake:
            state.update(status="ready", wait=None, reason="waiting_condition_changed")
        self._save(state, "source_observed")
        return state

    def wake_time(self, goal_id, now):
        with self.journal._transaction():
            state = self._read(goal_id)
            condition = state["wait"] or {}
            if (
                state["status"] == "waiting"
                and condition.get("kind") == "time"
                and now >= datetime.fromisoformat(condition["until"])
            ):
                state.update(status="ready", wait=None, reason="wait_deadline_reached")
                self._save(state, state["reason"])
            return state

    def hold(self, goal_id, reason, *, expected_version):
        """Persist an invalid-input boundary instead of retrying it every tick."""
        with self.journal._transaction():
            state = self._read(goal_id)
            self._expected(state, expected_version)
            if state["active_phase"] or state["status"] in TERMINAL:
                return state
            state.update(status="needs_review", reason=reason)
            self._save(state, reason)
            return state

    def claim(self, goal_id, *, expected_version):
        with self.journal._transaction():
            state = self._read(goal_id)
            self._expected(state, expected_version)
            if state["status"] != "ready" or state["active_phase"]:
                return None
            # P0 runs one goal phase at a time, including across journal instances.
            if any(
                self._blocks_dispatch(json.loads(r[0]))
                for r in self.journal._conn.execute("SELECT state FROM goal_views")
            ):
                return None
            auth = state["authorization"]
            if auth.get("expires_at") and datetime.now(
                timezone.utc
            ) >= datetime.fromisoformat(auth["expires_at"]):
                state.update(status="cancelled", reason="authorization_expired")
                state["authorization_version"] += 1
                self._save(state, state["reason"])
                return None
            if state["step"] == "execute":
                if len(state["attempts"]) >= auth["max_attempts"]:
                    state.update(status="failed", reason="attempt_limit_exhausted")
                    self._save(state, state["reason"])
                    return None
                state["attempts"].append(
                    {
                        "attempt": len(state["attempts"]) + 1,
                        "definition_version": state["definition_version"],
                        "execution_ids": [],
                    }
                )
            phase_id = uuid4().hex
            payload = {
                "goal_id": goal_id,
                "definition_version": state["definition_version"],
                "attempt": len(state["attempts"]),
                "step": state["step"],
                "parent_execution_id": state["parent_execution_id"],
                "authorization_version": state["authorization_version"],
                "cancel_version": state["cancel_version"],
            }
            # P0a's transaction-local admission joins identity and goal claim atomically.
            row, created = self.journal._admit(
                "goal:" + goal_id, "goal", phase_id, payload
            )
            if not created:
                raise GoalConflict("Unexpected duplicate phase identity")
            self.journal._event(row, "run.started", {})
            phase = {
                **payload,
                "phase_id": phase_id,
                "turn_id": row["turn_id"],
                "status": "running",
                "input_versions": dict(state["observed_sources"]),
            }
            state["phases"].append(phase)
            state["attempts"][-1]["execution_ids"].append(row["turn_id"])
            state.update(
                active_phase=phase_id,
                status="running",
                block=None,
                reason="phase_claimed",
            )
            self._save(state, state["reason"])
            return state, phase

    @staticmethod
    def _phase(state, phase_id):
        return next(p for p in state["phases"] if p["phase_id"] == phase_id)

    def check_dispatch(self, goal_id, phase_id):
        """Fence every physical send, including retries already budgeted."""
        with self.journal._transaction():
            state = self._read(goal_id)
            phase = self._phase(state, phase_id)
            execution = self.journal._row(phase["turn_id"])
            expiry = state["authorization"].get("expires_at")
            if (
                state["status"] != "running"
                or state["active_phase"] != phase_id
                or execution["epoch"] != self.journal.epoch
                or execution["status"] != "running"
                or phase["authorization_version"] != state["authorization_version"]
                or phase["cancel_version"] != state["cancel_version"]
                or state["block"]
                or (
                    expiry
                    and datetime.now(timezone.utc) >= datetime.fromisoformat(expiry)
                )
                or any(r["status"] == "unknown" for r in state["reservations"].values())
            ):
                raise GoalBlocked("Goal dispatch authorization is no longer valid")

    def reserve(self, goal_id, phase_id, kind, action, *, units=1):
        if (
            kind not in KINDS
            or ACTION_KINDS.get(action) != kind
            or type(units) is not int
            or units < 1
        ):
            raise ValueError("Invalid budget reservation")
        denied = None
        reservation_id = uuid4().hex
        with self.journal._transaction():
            state = self._read(goal_id)
            phase = self._phase(state, phase_id)
            execution = self.journal._row(phase["turn_id"])
            if (
                state["status"] != "running"
                or state["active_phase"] != phase_id
                or execution["epoch"] != self.journal.epoch
                or execution["status"] != "running"
                or phase["authorization_version"] != state["authorization_version"]
                or phase["cancel_version"] != state["cancel_version"]
            ):
                raise GoalBlocked("Goal cancelled, revoked or phase no longer active")
            auth = state["authorization"]
            if state["block"]:
                raise GoalBlocked(state["block"]["detail"])
            if any(r["status"] == "unknown" for r in state["reservations"].values()):
                raise GoalBlocked(
                    "A previous call has an unknown result; inspect before continuing"
                )
            if auth.get("expires_at") and datetime.now(
                timezone.utc
            ) >= datetime.fromisoformat(auth["expires_at"]):
                denied = {
                    "code": "authorization_expired",
                    "detail": "Host authorization expired",
                }
            elif action not in auth["allowed_actions"]:
                denied = {
                    "code": "action_not_authorized",
                    "detail": f"Host did not authorize {action}",
                }
            else:
                budget = self.budget(state)
                if (
                    budget["remaining"]["total"] < units
                    or budget["remaining"][kind] < units
                ):
                    denied = {
                        "code": "budget_exhausted",
                        "detail": f"{kind} needs {units} units; remaining={budget['remaining']}",
                    }
            if denied:
                state["block"] = denied
                self._save(state, "dispatch_denied")
            else:
                state["reservations"][reservation_id] = {
                    "phase_id": phase_id,
                    "kind": kind,
                    "action": action,
                    "units": units,
                    "status": "reserved",
                    "receipt": None,
                }
                self._save(state, "budget_reserved")
        if denied:
            raise GoalBlocked(denied["detail"])
        return reservation_id

    @staticmethod
    def _blocks_dispatch(state):
        return state["active_phase"] or any(
            item["action"] == "verify:command"
            and item["status"] != "settled"
            and not item.get("command_resolution")
            for item in state["reservations"].values()
        )

    def resolve_command(
        self, goal_id, reservation_id, *, expected_version, confirmed_stopped, note
    ):
        """Host attestation only: release serialization, never refund or retry work."""
        if (
            confirmed_stopped is not True
            or not isinstance(note, str)
            or not note.strip()
        ):
            raise ValueError("Confirm command exit and record the artifact inspection")
        with self.journal._transaction():
            state = self._read(goal_id)
            self._expected(state, expected_version)
            item = state["reservations"][reservation_id]
            if state["active_phase"] or item["action"] != "verify:command":
                raise GoalConflict(
                    "Only an inactive command reservation can be resolved"
                )
            if item["status"] == "settled" or item.get("command_resolution"):
                raise GoalConflict("Command already has a receipt or resolution")
            item["command_resolution"] = {
                "confirmed_stopped": True,
                "note": note.strip(),
                "created_at": utcnow(),
            }
            self._save(state, "command_stop_confirmed_by_host")
            return state

    def settle(self, goal_id, reservation_id, *, known, receipt):
        receipt = json.loads(encode(receipt))
        with self.journal._transaction():
            state = self._read(goal_id)
            item = state["reservations"][reservation_id]
            status = "settled" if known else "unknown"
            if item["status"] == "settled":
                if not known or item["receipt"] != receipt:
                    raise GoalConflict("Budget receipt cannot be replaced")
                return
            if item["status"] == status and item["receipt"] == receipt:
                return
            item.update(status=status, receipt=receipt)
            self._save(state, "budget_settled" if known else "budget_result_unknown")

    def finish(self, goal_id, phase_id, result, *, execution_status="completed"):
        result = json.loads(encode(result))
        with self.journal._transaction():
            state = self._read(goal_id)
            phase = self._phase(state, phase_id)
            if phase["status"] != "running":
                return state
            row = self.journal._row(phase["turn_id"])
            unknown = any(
                r["phase_id"] == phase_id and r["status"] != "settled"
                for r in state["reservations"].values()
            )
            status = (
                "needs_review"
                if unknown or self.journal._unresolved(phase["turn_id"])
                else execution_status
            )
            if row["status"] in {"accepted", "running"}:
                self.journal._event(
                    row, "run.ended", {"status": status, "result": result}
                )
            elif row["status"] != "completed":
                status = "needs_review"
            phase.update(status=status, result=result)
            state["active_phase"] = None
            if (
                state["status"] == "cancelled"
                or phase["cancel_version"] != state["cancel_version"]
            ):
                self._save(state, "late_result_after_cancellation")
                return state
            if unknown:
                state.update(
                    status="needs_review", reason="execution_or_call_result_unknown"
                )
            elif state["block"]:
                expired = state["block"]["code"] == "authorization_expired"
                state.update(
                    status="cancelled" if expired else "failed",
                    reason=state["block"]["code"],
                )
                if expired:
                    state["authorization_version"] += 1
            elif status != "completed":
                state.update(
                    status="needs_review", reason="execution_or_call_result_unknown"
                )
            elif phase["step"] == "execute":
                state.update(
                    status="ready",
                    step="verify",
                    reason="execution_completed_acceptance_pending",
                )
            elif phase["step"] == "replan":
                state.update(
                    status="ready", step="execute", reason="bounded_revision_prepared"
                )
            else:
                stale = phase["input_versions"] != state[
                    "observed_sources"
                ] or result.get("stale", False)
                state["evidence"].append(
                    {
                        "phase_id": phase_id,
                        "turn_id": phase["turn_id"],
                        "definition_version": phase["definition_version"],
                        **result,
                        "stale": stale,
                        "observed_sources": phase["input_versions"],
                    }
                )
                if not stale and "output_versions" in result:
                    from .contracts import digest

                    state["observed_sources"]["__local_files__"] = digest(
                        result["output_versions"]
                    )
                if stale:
                    state.update(
                        status="ready",
                        step="verify",
                        reason="acceptance_inputs_changed",
                    )
                elif result.get("passed") is True:
                    state.update(
                        status="succeeded", reason="all_executable_criteria_passed"
                    )
                elif len(state["attempts"]) < state["authorization"]["max_attempts"]:
                    state.update(
                        status="ready",
                        step="replan",
                        reason="acceptance_failed_revision_available",
                    )
                else:
                    state.update(
                        status="failed", reason="acceptance_failed_attempt_limit"
                    )
            self._save(state, state["reason"])
            return state

    def recover(self):
        # The journal recovers only dead epochs; live owners are never stolen.
        self.journal.recover_abandoned()
        for state in self.list():
            if state["active_phase"]:
                phase = self._phase(state, state["active_phase"])
                record = self.journal.get(phase["turn_id"])
                if record["status"] not in {"accepted", "running"}:
                    self.finish(
                        state["goal_id"],
                        phase["phase_id"],
                        record["result"] or {},
                        execution_status="needs_review",
                    )
