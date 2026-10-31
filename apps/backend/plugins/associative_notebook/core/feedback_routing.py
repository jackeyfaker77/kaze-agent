"""Offline attribution/target contracts; intent interpretation remains external.

No language classifier, delivery, model, topic closure or notification policy.
"""

from contextlib import contextmanager
from copy import deepcopy
import json

from .contracts import digest, require, text
from .reference_contract_v3 import Notebook


class FeedbackNotebook(Notebook):
    @contextmanager
    def transaction(self):
        # Let frozen correct() participate in the outer resolution transaction.
        # All callers are synchronous and own this explicit isolated connection.
        if not self.db.in_transaction:
            with super().transaction():
                yield
        else:
            self.db.execute("SAVEPOINT feedback_correction")
            try:
                yield
                self.db.execute("RELEASE feedback_correction")
            except BaseException:
                self.db.execute("ROLLBACK TO feedback_correction")
                self.db.execute("RELEASE feedback_correction")
                raise


class FeedbackRouter:
    def __init__(self, book, transport_store):
        require(isinstance(book, FeedbackNotebook), "Use the explicit feedback sandbox")
        self.book, self.transport = book, transport_store

    def delivery(self, key):
        """Kaze Outbox receipt, NOT a Maka database or a model-provided sent flag."""
        try:
            job = self.transport.get(key)
        except KeyError:
            return None
        payload = job["payload"]
        require(
            job["kind"] == "notebook-offline-candidate"
            and payload.get("simulation_only") is True,
            "Unsupported delivery",
        )
        cached = payload["cached"]
        require(
            cached["identity"] == self.book.identity
            and cached["directory"] == str(self.book.directory),
            "Delivery scope mismatch",
        )
        part = self.transport.db.execute(
            "SELECT * FROM parts WHERE job=? AND part='notebook-text'",
            (key,),
        ).fetchone()
        if part is None or part["state"] != "done":
            return None
        # A confirmed part survives interruption before the job completion write.
        # Job state alone is neither proof of delivery nor proof of non-delivery.
        receipt = json.loads(part["result"]) if part["result"] else None
        require(
            isinstance(receipt, dict)
            and isinstance(receipt.get("message_id"), str)
            and bool(receipt["message_id"])
            and json.loads(part["payload"]) == {"body": payload["body"]},
            "Delivery receipt differs",
        )
        require(
            job["result"] is None
            or (
                isinstance(job["result"], dict)
                and job["result"].get("message_id") == receipt["message_id"]
            ),
            "Job result differs from confirmed part",
        )
        findings = {e["id"]: e["payload"] for e in self.book.events("finding")}
        sent = {c["id"]: c for c in cached["candidates"]}
        ids = payload["candidate_ids"]
        require(
            bool(ids) and set(ids) <= set(findings) & set(sent),
            "Unknown delivered candidate",
        )
        require(
            payload["body"] == "\n\n".join(sent[i]["content"] for i in ids),
            "Sent body differs",
        )
        # Topic is a grouping label, not an instruction to change a particular note.
        targets = sorted({n for i in ids for n in findings[i]["note_ids"]})
        return {
            "job": key,
            "message_id": receipt["message_id"],
            "candidate_ids": ids,
            "cited_note_ids": targets,
            "payload_digest": digest(payload),
        }

    def plan(
        self, *, source_ref, action, reply_job=None, target_id=None, replacement=None
    ):
        """Actions/replacements are explicit hand-reviewed interpretations, not inferred.

        A correction always requires an explicit target. A delivery reply limits
        it to notes actually cited by the sent candidates, regardless of topic. Ordinary
        conversation can correct a specific statement without becoming feedback.
        """
        require(
            action
            in {
                "cancel_occurrence",
                "correct_statement",
                "record_preference",
                "no_change",
                "ambiguous",
            },
            "Unknown interpretation action",
        )
        source = self.book.sources().get(source_ref)
        require(
            source is not None and source["kind"] == "user",
            "User source required; silence is not feedback",
        )
        if reply_job is not None:
            text(reply_job, "reply job", 160)
        receipt = self.delivery(reply_job) if reply_job is not None else None
        request = {
            "source_ref": source_ref,
            "action": action,
            "reply_job": reply_job,
            "target_id": target_id,
            "replacement": deepcopy(replacement),
        }
        result = {
            "request": request,
            "identity": self.book.identity,
            "head": digest(self.book.events()),
            "receipt": receipt,
            "attribution": "delivery_feedback" if receipt else "direct_statement",
            "status": "needs_clarification",
            "target_id": None,
        }
        if reply_job is not None and receipt is None:
            return {
                **result,
                "attribution": "unconfirmed_reply",
                "reason": "No confirmed delivery; do not guess",
            }
        if action in {"ambiguous", "no_change", "record_preference"}:
            require(
                target_id is None and replacement is None,
                "This interpretation cannot replace notes",
            )
            return {
                **result,
                "status": "needs_clarification"
                if action == "ambiguous"
                else "record_only",
            }
        active = self.book.active_notes()
        if target_id is None:
            return {
                **result,
                "reason": "Explicit target required; never infer from topic",
            }
        require(
            target_id in active,
            "Target is no longer active; do not redirect to its successor",
        )
        if receipt:
            require(
                target_id in receipt["cited_note_ids"],
                "Target was not cited by the delivered suggestion",
            )
        require(isinstance(replacement, dict), "Explicit replacement required")
        require(
            replacement.get("topic") == active[target_id]["topic"]
            and replacement.get("title") == active[target_id]["title"],
            "Replacement changed target topic/title",
        )
        require(
            replacement.get("kind") == "statement"
            and replacement.get("related", []) == [],
            "Correction cannot expand related topics",
        )
        evidence = replacement.get("evidence", [])
        require(
            bool(evidence)
            and all(
                e.get("ref") == source_ref
                and isinstance(e.get("quote"), str)
                and e["quote"]
                and e["quote"] in source["text"]
                for e in evidence
            ),
            "Replacement must cite this user source verbatim",
        )
        return {**result, "status": "ready", "target_id": target_id}

    def apply(self, plan):
        """Persist attribution and correction atomically, preserving all other notes."""
        request = plan["request"]
        event_id = "feedback-resolution:" + request["source_ref"]
        with self.book.transaction():
            previous = next(
                (
                    e
                    for e in self.book.events("feedback_resolution")
                    if e["id"] == event_id
                ),
                None,
            )
            if previous:
                require(
                    previous["payload"]["plan_digest"] == digest(plan),
                    "Feedback already resolved differently",
                )
                return previous["payload"]
            require(plan == self.plan(**request), "Feedback plan is stale or changed")
            require(
                plan["status"] in {"ready", "record_only"}, "Clarify before applying"
            )
            before = self.book.active_notes()
            new_id = None
            if plan["status"] == "ready":
                new_id = self.book.correct(
                    "feedback-" + digest(plan),
                    replaces=[plan["target_id"]],
                    replacement=request["replacement"],
                )
                after = self.book.active_notes()
                require(
                    all(
                        after.get(n) == v
                        for n, v in before.items()
                        if n != plan["target_id"]
                    ),
                    "Unrelated understanding changed",
                )
            result = {
                "plan_digest": digest(plan),
                "source_ref": request["source_ref"],
                "action": request["action"],
                "attribution": plan["attribution"],
                "receipt": plan["receipt"],
                "target_id": plan["target_id"],
                "replacement_id": new_id,
                "policy_changed": False,
            }
            self.book._event(event_id, "feedback_resolution", result)
            return result
