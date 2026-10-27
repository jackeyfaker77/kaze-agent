"""Detach queued work from the producer's lifetime, retaining a causal link."""
from contextvars import Context, ContextVar, copy_context

from .scope import current_effect, current_execution, current_model_tool

background_origin: ContextVar[dict | None] = ContextVar('background_execution_origin', default=None)


def background_context() -> Context:
    from .model_recording import current_model_call, history_lane

    context = copy_context()
    parent = current_execution.get()
    if parent is not None:
        context.run(background_origin.set, {'root_id': parent.journal.root_id, 'turn_id': parent.turn_id})
    context.run(current_execution.set, None)
    context.run(current_effect.set, None)
    context.run(current_model_tool.set, None)
    context.run(current_model_call.set, None)
    context.run(history_lane.set, False)
    return context
