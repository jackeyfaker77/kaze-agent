"""Execution projections, operator decisions and retry admission safety."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.execution.journal import ExecutionJournal
from core.execution.contracts import ExecutionConflict
from core.execution.projection import ExecutionProjectionError, project_event


@pytest.fixture
def journal(tmp_path):
    store = ExecutionJournal(tmp_path)
    yield store
    store.close()


def ended(journal, *, key="one", policy=None, confirmed=False, source="desktop"):
    row, _ = journal.admit("desktop:test", source, key, {"content": "input"})
    turn = row["turn_id"]
    journal.start(turn)
    if policy is not None:
        journal.append(turn, "tool.dispatched", {"effect_id": "effect", "name": "test",
                                                "arguments": {}, "retry_policy": policy})
        if confirmed:
            journal.append(turn, "tool.returned", {"effect_id": "effect", "certainty": "confirmed", "result": "ok"})
    journal.finish(turn, "failed")
    return journal.get(turn)


def test_projection_rebuild_restores_dedupe_status_result_and_stable_cursor(journal):
    original = ended(journal)
    child, created = journal.admit_retry(original["turn_id"], expected_seq=original["last_seq"], action_id="retry")
    assert created
    journal.start(child["turn_id"])
    journal.finish(child["turn_id"], "completed", {"reply": "saved"})
    before = journal.list_executions()
    events = journal._conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
    for _ in range(2):
        journal._conn.execute("DELETE FROM executions")
        journal._conn.execute("DELETE FROM execution_views")
        journal._conn.commit()
        journal.rebuild_projection()
        assert journal.list_executions() == before
        assert journal._conn.execute("SELECT * FROM events ORDER BY seq").fetchall() == events
        replayed, created = journal.admit_retry(original["turn_id"], expected_seq=original["last_seq"], action_id="retry")
        assert not created and replayed["result"] == {"reply": "saved"}
        same, created = journal.admit("desktop:test", "desktop", "one", {"content": "input"})
        assert not created and same["turn_id"] == original["turn_id"]
    first = journal.list_executions(limit=1)
    second = journal.list_executions(before=first["next_cursor"])
    assert second["executions"][0]["turn_id"] == original["turn_id"]


@pytest.mark.parametrize("policy,confirmed,status,allowed", [
    (None, False, "failed", True),
    ("read_only", False, "failed", True),
    ("read_only", True, "failed", True),
    ("manual_review", False, "needs_review", False),
    ("manual_review", True, "failed", False),
    ("future_policy", False, "needs_review", False),
])
def test_retry_safety_comes_from_dispatch_fact_not_success_or_name(journal, policy, confirmed, status, allowed):
    row = ended(journal, policy=policy, confirmed=confirmed)
    assert row["status"] == status and row["retry"]["allowed"] == allowed
    if not allowed:
        with pytest.raises(ExecutionConflict):
            journal.admit_retry(row["turn_id"], expected_seq=row["last_seq"], action_id="retry")


def test_review_is_a_fact_not_a_fabricated_receipt_and_replay_is_idempotent(journal):
    row = ended(journal, policy="manual_review")
    args = dict(expected_seq=row["last_seq"], action_id="review", decision="no_effect", note="Target inspected after executor stopped; no write present")
    reviewed = journal.review(row["turn_id"], **args)
    assert reviewed["status"] == "needs_review"
    assert reviewed["effects"][0]["certainty"] == "unknown"
    assert reviewed["retry"]["allowed"]
    assert journal.review(row["turn_id"], **args) == reviewed
    with pytest.raises(ExecutionConflict):
        journal.review(row["turn_id"], **{**args, "note": "changed"})
    with pytest.raises(ExecutionConflict):
        journal.admit_retry(row["turn_id"], expected_seq=row["last_seq"], action_id="stale")
    journal.rebuild_projection()
    assert journal.get(row["turn_id"]) == reviewed
    retry, created = journal.admit_retry(row["turn_id"], expected_seq=reviewed["last_seq"], action_id="retry")
    assert created and retry["retry_of"] == row["turn_id"]


def test_confirmed_write_cannot_be_declared_absent_or_repeated(journal):
    row = ended(journal, policy="manual_review", confirmed=True)
    with pytest.raises(ExecutionProjectionError):
        journal.review(row["turn_id"], expected_seq=row["last_seq"], action_id="bad", decision="no_effect", note="pretend")
    closed = journal.review(row["turn_id"], expected_seq=row["last_seq"], action_id="close", decision="close", note="Write confirmed; remaining work will be handled separately")
    assert closed["status"] == "failed" and not closed["retry"]["allowed"]
    journal.rebuild_projection()
    assert journal.get(row["turn_id"]) == closed


def test_atomic_retry_rolls_back_parent_when_child_admission_fails(journal):
    row = ended(journal)
    journal._conn.execute("""CREATE TRIGGER reject_retry BEFORE INSERT ON events WHEN NEW.kind='request.accepted'
                            BEGIN SELECT RAISE(ABORT, 'fault'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        journal.admit_retry(row["turn_id"], expected_seq=row["last_seq"], action_id="retry")
    assert journal.get(row["turn_id"]) == row
    assert len(journal.list_executions()["executions"]) == 1


def test_two_connections_can_accept_only_one_retry(tmp_path):
    first = ExecutionJournal(tmp_path)
    second = ExecutionJournal(tmp_path)
    try:
        row = ended(first)
        def retry(store):
            return store.admit_retry(row["turn_id"], expected_seq=row["last_seq"], action_id="same")
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(retry, [first, second]))
        assert sum(created for _, created in results) == 1
        assert results[0][0]["turn_id"] == results[1][0]["turn_id"]
        assert len(first.list_executions()["executions"]) == 2
    finally:
        first.close()
        second.close()


def test_legacy_effect_without_policy_is_not_retroactively_safe(journal):
    row, _ = journal.admit("desktop:test", "desktop", "old", {})
    journal.start(row["turn_id"])
    journal.append(row["turn_id"], "tool.dispatched", {"effect_id": "old-read", "name": "read_file", "arguments": {}})
    journal.finish(row["turn_id"], "failed")
    journal.rebuild_projection()
    assert not journal.get(row["turn_id"])["retry"]["allowed"]


@pytest.mark.parametrize("damage", ["version", "kind", "identity", "orphan_return"])
def test_invalid_event_prefix_fails_closed_without_destroying_previous_projection(journal, damage):
    row = ended(journal)
    snapshot = journal.get(row["turn_id"])
    event = journal.events(row["turn_id"])["events"][0]
    if damage == "version":
        event["version"] = 999
    elif damage == "kind":
        event["kind"] = "future.fact"
    elif damage == "identity":
        event["epoch"] = "different"
    else:
        event.update(kind="tool.returned", payload={"effect_id": "missing", "certainty": "confirmed"})
    with pytest.raises(ExecutionProjectionError):
        project_event(None, event, journal.root_id)
    assert journal.get(row["turn_id"]) == snapshot
    journal._conn.execute("INSERT INTO events(turn_id,run_id,epoch,version,kind,payload,created_at) VALUES (?,?,?,?,?,?,?)",
                          (row["turn_id"], row["run_id"], row["epoch"], 999, "run.ended", '{}', 'now'))
    journal._conn.commit()
    old_view = journal._conn.execute("SELECT state FROM execution_views").fetchone()[0]
    with pytest.raises(ExecutionProjectionError):
        journal.rebuild_projection()
    assert journal._conn.execute("SELECT state FROM execution_views").fetchone()[0] == old_view


def test_channel_owned_executions_are_reviewable_but_not_desktop_retried(journal):
    row = ended(journal, source="telegram")
    assert not row["retry"]["allowed"]
    closed = journal.review(row["turn_id"], expected_seq=row["last_seq"], action_id="close", decision="close", note="Handled in source channel")
    assert closed["review"]["decision"] == "close"
    assert len(journal.list_executions(status="attention")["executions"]) == 1
    assert not journal.list_executions(status="completed")["executions"]


@pytest.mark.parametrize("fault", ["retry_before_commit", "retry_accepted", "retry_effect", "retry_completed", "projection_midway", "review_committed", "revoke_before_commit", "revoke_committed"])
def test_hard_exit_rebuild_and_duplicate_replay_do_not_repeat_effects(tmp_path, fault):
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(root / "apps/backend"), "PYTHONIOENCODING": "utf-8"}
    script = root / "tests/e2e/execution_projection_crash.py"
    def run(mode):
        return subprocess.run([sys.executable, str(script), str(tmp_path), mode], cwd=root, env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=45)
    crash = run(fault)
    assert crash.returncode == 73, crash.stdout + crash.stderr
    effects = tmp_path / "effects.txt"
    before = effects.read_text() if effects.exists() else ""
    reports = []
    for _ in range(2):
        recovery = run("recover")
        assert recovery.returncode == 0, recovery.stdout + recovery.stderr
        reports.append(json.loads(recovery.stdout))
        assert (effects.read_text() if effects.exists() else "") == before
    assert reports[0] == reports[1]
    original = reports[0]["original"]
    if fault in {"retry_before_commit", "projection_midway", "review_committed", "revoke_before_commit", "revoke_committed"}:
        assert original["retry_turn_id"] is None
    else:
        assert reports[0]["child"]["status"] == {"retry_accepted": "interrupted", "retry_effect": "needs_review", "retry_completed": "completed"}[fault]
    if fault == "review_committed":
        assert original["review"]["note"] == "verified"
    if fault == "revoke_before_commit":
        assert original["review"]["decision"] == "no_effect" and original["retry"]["allowed"]
        assert "revoke" not in original["actions"]
    if fault == "revoke_committed":
        assert original["review"]["decision"] == "close" and not original["retry"]["allowed"]
        assert original["actions"]["review"]["decision"] == "no_effect"
        assert original["actions"]["revoke"]["decision"] == "close"


def test_execution_rebuild_cli_obeys_workspace_owner(tmp_path, capsys):
    from bootstrap.host_state import WorkspaceOwner
    from main import main
    store = ExecutionJournal(tmp_path)
    ended(store)
    store.close()
    owner = WorkspaceOwner(tmp_path, "maintenance")
    try:
        assert main(["execution-rebuild", "--workspace", str(tmp_path)]) == 2
    finally:
        owner.close()
    assert main(["execution-rebuild", "--workspace", str(tmp_path)]) == 0
    assert '"rebuilt": true' in capsys.readouterr().out
