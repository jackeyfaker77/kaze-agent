"""Record physical push parts, including scheduler/proactive sends outside a turn."""
from uuid import uuid4

from .journal import ExecutionJournal
from .scope import ExecutionScope, active_execution


async def record_delivery(journal: ExecutionJournal | None, session_key: str, payload: dict, operation):
    scope = active_execution()
    owned = scope is None and isinstance(journal, ExecutionJournal)
    if owned:
        assert journal is not None
        row, _ = journal.admit(session_key, "delivery", uuid4().hex, payload)
        journal.start(row["turn_id"])
        scope = ExecutionScope(journal, row["turn_id"])
    if scope is None:
        return await operation()
    effect_id = uuid4().hex
    try:
        scope.record("delivery.dispatched", {**payload, "effect_id": effect_id})
        message_id, error = await operation()
        scope.record("delivery.returned", {"effect_id": effect_id,
            "certainty": "unknown" if error else "confirmed", "message_id": message_id,
            "ok": not bool(error)})
        if owned:
            scope.journal.finish(scope.turn_id, "failed" if error else "completed")
        return message_id, error
    except BaseException:
        if owned and not scope.broken:
            scope.journal.finish(scope.turn_id, "failed")
        raise
