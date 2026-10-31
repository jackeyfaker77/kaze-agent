"""Explicit operator reconciliation of a recorded length failure; never calls a model."""
import json
import sqlite3
from contextlib import ExitStack
from pathlib import Path

from bootstrap.host_state import WorkspaceOwner
from core.execution.model_context import project_model_context
from .core.contracts import digest, require, text
from .core.feedback_routing import FeedbackNotebook


def review_length_failure(directory, event_db, turn_id, run_id, *, reason, apply=False):
    text(reason, "review reason", 500)
    directory, event_db = Path(directory).resolve(), Path(event_db).resolve()
    marker = json.loads((directory / "sandbox.json").read_text(encoding="utf-8"))
    # Read the authoritative event log without constructing a Runtime/lease.
    with sqlite3.connect(event_db.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        events = [{**dict(r), "payload": json.loads(r["payload"])} for r in db.execute(
            "SELECT * FROM events WHERE turn_id=? ORDER BY seq", (turn_id,))]
    require(bool(events), "Unknown execution")
    identity = events[0]["payload"]["identity"]
    require(identity["root_id"] == marker["owner"] and identity["session_key"] == marker["scope"],
            "Notebook and execution identities differ")
    require(any(e["kind"] == "run.ended" for e in events), "Execution still active")
    require(not any(e["kind"] == "delivery.dispatched" for e in events), "Execution includes delivery")
    with sqlite3.connect((directory / "notebook.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT request,state FROM runs WHERE id=?", (run_id,)).fetchone()
        captured = db.execute("SELECT payload FROM events WHERE id=? AND kind='capture'",
                              ("capture:" + run_id,)).fetchone()
    require(row is not None, "Unknown notebook run")
    require(captured is not None, "Missing captured reply evidence")
    capture = json.loads(captured[0])
    replies = {ref: capture["receipts"][job] for ref, job in capture["reply_jobs"].items()
               if job in capture["receipts"]}
    request, state = json.loads(row[0]), row[1]
    matches = []
    for event in events:
        if event["kind"] != "model.request.prepared":
            continue
        call_id = event["payload"]["call_id"]
        context = project_model_context(events, root_id=marker["owner"], turn_id=turn_id,
                                        call_id=call_id, through_seq=event["seq"])
        wire = context["request"]
        for message in wire.get("messages", []):
            if message.get("role") != "user":
                continue
            try:
                material = json.loads(message["content"])
            except (ValueError, TypeError):
                continue
            if not isinstance(material, dict) or material.get("input") != request:
                continue
            require(material.get("confirmed_replies") == replies, "Captured reply evidence differs")
            require(not wire.get("tools"), "Learning request unexpectedly has tools")
            responses = [e for e in events if e["kind"] == "model.response.recorded"
                         and e["payload"]["call_id"] == call_id]
            attempts = [e for e in events if e["kind"] == "model.attempt.started"
                        and e["payload"]["call_id"] == call_id]
            require(len(responses) == 1 and len(attempts) == 1, "Ambiguous model attempts")
            failure = responses[0]
            require(failure["payload"].get("error_type") == "ContextLengthError"
                    and "response" not in failure["payload"], "Not a recorded length failure; do not retry")
            matches.append({"turn_id": turn_id, "call_id": call_id, "event_seq": failure["seq"],
                            "request_digest": context["request_digest"], "run_id": run_id,
                            "reason": reason, "failure_type": "ContextLengthError"})
    require(len(matches) == 1, "No unique matching failed request")
    receipt = matches[0]
    require(state in {"awaiting_result", "discarded"}, "Run cannot be retired")
    if apply:
        with ExitStack() as stack:
            # Reuse the Host's OS lock, including when the event log is a copy.
            workspace = directory.parent.parent if directory.parent.name == "associative_notebook" else event_db.parents[2]
            if directory.is_relative_to(workspace):
                owner = WorkspaceOwner(workspace, "notebook-review")
                stack.callback(owner.close)
            book = FeedbackNotebook(directory, owner=marker["owner"], scope=marker["scope"])
            stack.callback(book.close)
            with book.transaction():
                existing = book.events("failure_review")
                prior = next((e for e in existing if e["id"] == "failure-review:" + run_id), None)
                require(prior is None or prior["payload"] == receipt, "Review receipt conflict")
                require(book.run(run_id)["state"] == state, "Run changed during review")
                require(not any(e["id"] == "response:" + run_id
                        for e in book.events("response")), "A response already exists")
                if prior is None:
                    require(state == "awaiting_result", "Discarded by another operation")
                    book._event("failure-review:" + run_id, "failure_review", receipt)
                    book._event("discard:" + run_id, "discarded", {"reason": reason})
                    book.db.execute("UPDATE runs SET state='discarded' WHERE id=?", (run_id,))
    return {"applied": apply, "receipt": receipt, "review_digest": digest(receipt)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("directory", "event-db", "turn-id", "run-id", "reason"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--apply", action="store_true", help="Retire only this verified failed run; no calls or coverage writes")
    args = parser.parse_args()
    print(json.dumps(review_length_failure(args.directory, args.event_db, args.turn_id,
                                          args.run_id, reason=args.reason, apply=args.apply)))
