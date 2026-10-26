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
        from bus.durable import delivery_scope
        queued = delivery_scope.get()
        if queued is not None:
            queued.queue.store.link_execution(queued.key, scope.turn_id)
        scope.record("delivery.dispatched", {**payload, "effect_id": effect_id})
        message_id, error = await operation()
        scope.record("delivery.returned", {"effect_id": effect_id,
            "certainty": "unknown" if error else "confirmed", "message_id": message_id,
            "ok": not bool(error)})
        if owned:
            scope.journal.finish(scope.turn_id, "failed" if error else "completed")
        return message_id, error
    except BaseException as exc:
        from bus.durable import RetryDelivery, PermanentDelivery
        if isinstance(exc, (RetryDelivery, PermanentDelivery)) and not scope.broken:
            # The adapter confirmed rejection before acceptance. Close this attempt's
            # effect so a later successful retry doesn't falsely need manual review.
            scope.record('delivery.returned', {'effect_id': effect_id,
                'certainty': 'confirmed', 'ok': False, 'message_id': None})
        if owned and not scope.broken:
            scope.journal.finish(scope.turn_id, "failed")
        raise
