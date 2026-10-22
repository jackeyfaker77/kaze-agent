"""V1 execution contract. RPC correlation IDs are NOT idempotency keys."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ExecutionStatus = Literal["accepted", "running", "completed", "failed", "interrupted", "needs_review"]
CONTRACT_VERSION = 1
ACTIVE = ("accepted", "running")
TERMINAL = ("completed", "failed", "interrupted", "needs_review")


@dataclass(frozen=True)
class ExecutionIdentity:
    root_id: str
    epoch: str
    session_key: str
    source: str
    request_key: str
    turn_id: str
    run_id: str


class ExecutionConflict(RuntimeError):
    """A request key was reused for different input, or a session is occupied."""


class ExecutionUnavailable(RuntimeError):
    """Accepted work must be queried, not silently submitted a second time."""


class JournalWriteError(RuntimeError):
    """No further effects may start after a durable boundary fails."""
