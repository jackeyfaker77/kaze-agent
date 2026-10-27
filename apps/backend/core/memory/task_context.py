"""Context carried only by recoverable memory work, never by an ordinary turn."""
from contextvars import ContextVar
from functools import wraps
from typing import Any

memory_intent: ContextVar[dict | None] = ContextVar('memory_intent', default=None)
memory_task: ContextVar[Any] = ContextVar('memory_task', default=None)


class MemoryTaskBlocked(RuntimeError):
    """The task remains visible; an unknown result must not be retried."""


def checkpoint_async(operation):
    @wraps(operation)
    async def invoke(self, *args, **kwargs):
        task = memory_task.get()
        if task is None:
            return await operation(self, *args, **kwargs)
        return await task.async_step(operation.__qualname__, {'args': args, 'kwargs': kwargs}, lambda: operation(self, *args, **kwargs))
    return invoke


def checkpoint_store(operation):
    @wraps(operation)
    def invoke(self, *args, **kwargs):
        task = memory_task.get()
        if task is None or self is not task.store:
            return operation(self, *args, **kwargs)
        return task.store_step(operation.__name__, {'args': args, 'kwargs': kwargs}, lambda: operation(self, *args, **kwargs))
    return invoke


def checkpoint_external(operation):
    @wraps(operation)
    async def invoke(self, *args, **kwargs):
        task = memory_task.get()
        if task is None:
            return await operation(self, *args, **kwargs)
        return await task.external_step(operation.__qualname__, {'args': args, 'kwargs': kwargs}, lambda: operation(self, *args, **kwargs))
    return invoke
