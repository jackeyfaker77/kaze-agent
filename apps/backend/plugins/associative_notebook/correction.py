"""Explicit cross-store retirement with durable, idempotent completion."""

from dataclasses import asdict

from core.execution.scope import active_execution
from core.memory.engine import MemoryMutation, MemoryMutationResult, MemoryScope
from .core.contracts import digest, require


class CorrectionMixin:
    def pending_corrections(self):
        completed = {e["id"] for e in self.book.events("forget_completed")}
        return [e for e in self.book.events("forget_requested")
                if "forget-completed:" + e["payload"]["operation"] not in completed]

    async def forget(self, request, memory):
        execution = active_execution()
        require(execution is not None and execution.active and not execution.broken
                and not execution.review_required
                and not getattr(execution, "proactive", False)
                and execution.journal.get(execution.turn_id)["session_key"] == self.session_key
                and request.scope.session_key == self.session_key,
                "Correction requires the recorded notebook session")
        async with self.lock:
            ids = sorted(set(request.ids))
            operation = digest([execution.turn_id, asdict(request.scope), ids])
            identity = "forget-requested:" + operation
            previous = next((e for e in self.book.events("forget_requested")
                             if e["id"] == identity), None)
            if previous is None:
                notes = {e["id"]: e["payload"] for e in self.book.events("note")}
                targets = [i.removeprefix("notebook:") for i in ids if i.startswith("notebook:")]
                found = [i for i in targets if i in notes]
                payload = {"operation": operation, "scope": asdict(request.scope),
                           "memory_ids": [i for i in ids if not i.startswith("notebook:")],
                           "notebook_ids": ["notebook:" + i for i in found],
                           "missing_ids": ["notebook:" + i for i in targets if i not in notes]}
                # The intent and notebook tombstones become durable together. A
                # crash before the other store's receipt keeps consumption fenced.
                with self.book.transaction():
                    self.book._event(identity, "forget_requested", payload)
                    active = self.book.active_notes()
                    for topic in sorted({notes[i]["topic"] for i in found if i in active}):
                        replaced = [i for i in found if i in active and notes[i]["topic"] == topic]
                        self.book._event("correction:forget-" + digest([operation, topic]), "correction", {
                            "replaces": replaced, "note_id": None, "topic": topic,
                            "retired_refs": sorted({e["ref"] for i in replaced for e in notes[i]["evidence"]}),
                        })
            else:
                payload = previous["payload"]
            return await self._complete_forget(payload, memory)

    async def _complete_forget(self, payload, memory):
        identity = "forget-completed:" + payload["operation"]
        previous = next((e for e in self.book.events("forget_completed") if e["id"] == identity), None)
        if previous is not None:
            return MemoryMutationResult(**previous["payload"])
        result = MemoryMutationResult(accepted=False)
        if payload["memory_ids"]:
            result = await memory.mutate(MemoryMutation(kind="forget", ids=tuple(payload["memory_ids"]),
                                scope=MemoryScope(**payload["scope"])))
            require(set(result.affected_ids) | set(result.missing_ids) == set(payload["memory_ids"]),
                    "Memory correction did not account for all targets")
            require(result.status != "disabled", "Memory engine unavailable for correction")
        result.affected_ids = list(result.affected_ids) + payload["notebook_ids"]
        result.missing_ids = list(result.missing_ids) + payload["missing_ids"]
        result.accepted = bool(result.affected_ids)
        result.status = "superseded"
        self.book._event(identity, "forget_completed", asdict(result))
        return result

    async def resume_corrections(self):
        for event in self.pending_corrections():
            require(self.memory_engine is not None, "Memory engine required to finish correction")
            await self._complete_forget(event["payload"], self.memory_engine)
