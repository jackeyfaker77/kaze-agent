"""Real process death at retry/review/projection transaction boundaries."""
import json
import os
from pathlib import Path
import sys

from core.execution.journal import ExecutionJournal
from core.execution.contracts import ExecutionConflict

workspace, mode = Path(sys.argv[1]), sys.argv[2]
journal = ExecutionJournal(workspace)
if mode == "recover":
    original = journal.find("desktop:test", "desktop", "original")
    child = journal.get(original["retry_turn_id"]) if original["retry_turn_id"] else None
    if child:
        same, created = journal.admit_retry(original["turn_id"], expected_seq=0, action_id="retry")
        assert not created and same == child
    if original["review"]:
        assert journal.review(original["turn_id"], expected_seq=0, action_id="review", decision="no_effect", note="verified") == original
        if original["review"]["decision"] == "close":
            assert journal.review(original["turn_id"], expected_seq=0, action_id="revoke", decision="close", note="late receipt") == original
            try:
                journal.admit_retry(original["turn_id"], expected_seq=original["last_seq"], action_id="forbidden-retry")
            except ExecutionConflict:
                pass
            else:
                raise AssertionError("A committed revocation must survive restart")
    journal.rebuild_projection()
    assert journal.get(original["turn_id"]) == original
    print(json.dumps({"original": original, "child": child, "count": journal._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]}))
    journal.close()
    raise SystemExit(0)

original, _ = journal.admit("desktop:test", "desktop", "original", {"content": "original"})
journal.start(original["turn_id"])
if mode in {"review_committed", "revoke_before_commit", "revoke_committed"}:
    journal.append(original["turn_id"], "tool.dispatched", {"effect_id": "unknown", "name": "external"})
journal.finish(original["turn_id"], "failed")
original = journal.get(original["turn_id"])
if mode in {"review_committed", "revoke_before_commit", "revoke_committed"}:
    journal.review(original["turn_id"], expected_seq=original["last_seq"], action_id="review", decision="no_effect", note="verified")
    if mode != "review_committed":
        original = journal.get(original["turn_id"])
        if mode == "revoke_before_commit":
            event = journal._event
            def crash_revocation(row, kind, payload):
                event(row, kind, payload)
                if kind == "execution.reviewed" and payload["decision"] == "close":
                    os._exit(73)
            journal._event = crash_revocation
        journal.review(original["turn_id"], expected_seq=original["last_seq"], action_id="revoke", decision="close", note="late receipt")
    os._exit(73)
if mode == "projection_midway":
    sync = journal._sync_projection
    def crash_projection():
        sync()
        os._exit(73)
    journal._sync_projection = crash_projection
    journal.rebuild_projection()
if mode == "retry_before_commit":
    event = journal._event
    def crash_event(row, kind, payload):
        event(row, kind, payload)
        if kind == "execution.retried":
            os._exit(73)
    journal._event = crash_event
child, _ = journal.admit_retry(original["turn_id"], expected_seq=original["last_seq"], action_id="retry")
if mode == "retry_accepted":
    os._exit(73)
journal.start(child["turn_id"])
journal.append(child["turn_id"], "tool.dispatched", {"effect_id": "write", "name": "write", "arguments": {}})
with (workspace / "effects.txt").open("a") as effect:
    effect.write("one\n")
    effect.flush()
    os.fsync(effect.fileno())
if mode == "retry_effect":
    os._exit(73)
journal.append(child["turn_id"], "tool.returned", {"effect_id": "write", "certainty": "confirmed", "result": "ok"})
journal.finish(child["turn_id"], "completed", {"reply": "saved"})
os._exit(73)
