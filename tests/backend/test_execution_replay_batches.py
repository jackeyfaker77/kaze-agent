"""Streaming replay keeps all facts and safety checks without rewriting large states per delta."""
import json

import pytest

from core.execution import journal as journal_module
from core.execution.journal import ExecutionJournal
from core.execution.projection import ExecutionProjectionError, project_event


@pytest.fixture
def journal(tmp_path):
    store = ExecutionJournal(tmp_path)
    try:
        yield store
    finally:
        store.close()


def raw_event(journal, turn, kind="reply.delta", payload='{"content":"piece"}', **overrides):
    row = dict(journal._row(turn))
    row.update(version=1, kind=kind, payload=payload, created_at="now")
    row.update(overrides)
    journal._conn.execute("""INSERT INTO events(turn_id,run_id,epoch,version,kind,payload,created_at)
        VALUES (:turn_id,:run_id,:epoch,:version,:kind,:payload,:created_at)""", row)


def test_replay_matches_each_fact_and_bounds_full_state_writes(journal, monkeypatch):
    turns = []
    for name in ("first", "other"):
        row, _ = journal.admit(f"desktop:{name}", "desktop", name, {"content": "large input" * 2000})
        journal.start(row["turn_id"])
        turns.append(row["turn_id"])
    for turn, count in ((turns[0], 100), (turns[1], 20), (turns[0], 100)):
        for _ in range(count):
            raw_event(journal, turn)
    journal._conn.commit()
    facts = [dict(r) for r in journal._conn.execute("SELECT * FROM events ORDER BY seq")]
    expected = {}
    for event in facts:
        expected[event["turn_id"]] = project_event(expected.get(event["turn_id"]),
            {**event, "payload": json.loads(event["payload"])}, journal.root_id)
    calls = []
    def project(previous, event, root):
        calls.append(event["kind"])
        return project_event(previous, event, root)
    monkeypatch.setattr(journal_module, "project_event", project)
    journal.rebuild_projection()
    assert calls.count("reply.delta") == 3
    for turn in turns:
        assert journal._state(turn) == expected[turn]
    assert [dict(r) for r in journal._conn.execute("SELECT * FROM events ORDER BY seq")] == facts
    assert journal._conn.execute("SELECT seq FROM execution_checkpoint").fetchone()[0] == facts[-1]["seq"]
    # Ordinary incremental commits after replay still use the next exact prefix.
    journal.append(turns[0], "reply.delta", {"content": "next"})
    journal.finish(turns[0], "completed", {"reply": "done"})
    assert journal.get(turns[0])["result"] == {"reply": "done"}


@pytest.mark.parametrize("damage", ["payload", "json", "version", "epoch", "run_id", "turn_id"])
def test_invalid_middle_delta_still_fails_and_rolls_back(journal, damage):
    row, _ = journal.admit("desktop:test", "desktop", "one", {})
    turn = row["turn_id"]
    journal.start(turn)
    before = journal._state(turn)
    raw_event(journal, turn)
    overrides = {"payload": "[]"} if damage == "payload" else {"payload": "{"} if damage == "json" else {
        damage: 999 if damage == "version" else "different"}
    raw_event(journal, turn, **overrides)
    raw_event(journal, turn)
    journal._conn.commit()
    with pytest.raises((ExecutionProjectionError, json.JSONDecodeError)):
        journal.rebuild_projection()
    assert journal._state(turn) == before


def test_delta_after_terminal_state_is_not_hidden(journal):
    row, _ = journal.admit("desktop:test", "desktop", "one", {})
    turn = row["turn_id"]
    journal.start(turn)
    journal.finish(turn, "completed")
    before = journal._state(turn)
    raw_event(journal, turn)
    raw_event(journal, turn)
    journal._conn.commit()
    with pytest.raises(ExecutionProjectionError):
        journal.rebuild_projection()
    assert journal._state(turn) == before
