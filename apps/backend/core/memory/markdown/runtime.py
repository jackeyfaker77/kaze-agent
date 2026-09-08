"""Markdown memory store、读取 runtime 与装配入口。"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent.memory import MemoryStore

if TYPE_CHECKING:
    from agent.provider import LLMProvider
    from bus.event_bus import EventBus
    from .maintenance import MarkdownMemoryMaintenance

class MarkdownMemoryStore(MemoryStore):
    def read_recent_history(self, *, max_chars: int = 0) -> str:
        return self.read_history(max_chars=max_chars)

    def backup_long_term(self, backup_name: str = "MEMORY.bak.md") -> None:
        if self.memory_file.exists():
            shutil.copyfile(
                self.memory_file,
                self.memory_file.with_name(backup_name),
            )

    def has_long_term_memory(self) -> bool:
        return bool(self.read_long_term().strip())


def resolve_markdown_store(
    *,
    workspace: Path,
    default_store: MarkdownMemoryStore | None = None,
    session_metadata: dict[str, Any] | None = None,
) -> MarkdownMemoryStore:
    return default_store if default_store is not None else MarkdownMemoryStore(workspace)


@dataclass
class MarkdownMemoryRuntime:
    store: MarkdownMemoryStore
    maintenance: "MarkdownMemoryMaintenance"
    workspace: Path

    def resolve_store(
        self,
        *,
        session_metadata: dict[str, Any] | None = None,
    ) -> MarkdownMemoryStore:
        return resolve_markdown_store(
            workspace=self.workspace,
            default_store=self.store,
            session_metadata=session_metadata,
        )

    def read_long_term(
        self,
        *,
        session_metadata: dict[str, Any] | None = None,
    ) -> str:
        return self.resolve_store(
            session_metadata=session_metadata,
        ).read_long_term()

    def read_self(
        self,
        *,
        session_metadata: dict[str, Any] | None = None,
    ) -> str:
        return self.resolve_store(
            session_metadata=session_metadata,
        ).read_self()

    def read_recent_context(
        self,
        *,
        session_metadata: dict[str, Any] | None = None,
    ) -> str:
        return self.resolve_store(
            session_metadata=session_metadata,
        ).read_recent_context()

    def read_recent_history(
        self,
        *,
        max_chars: int = 0,
        session_metadata: dict[str, Any] | None = None,
    ) -> str:
        return self.resolve_store(
            session_metadata=session_metadata,
        ).read_recent_history(max_chars=max_chars)

    def get_memory_context(
        self,
        *,
        session_metadata: dict[str, Any] | None = None,
    ) -> str:
        return self.resolve_store(
            session_metadata=session_metadata,
        ).get_memory_context()

def build_markdown_memory_runtime(
    *,
    workspace: Path,
    provider: "LLMProvider",
    model: str,
    keep_count: int,
    event_bus: "EventBus | None" = None,
    recent_context_provider: "LLMProvider | None" = None,
    recent_context_model: str | None = None,
) -> MarkdownMemoryRuntime:
    from .maintenance import MarkdownMemoryMaintenance

    store = MarkdownMemoryStore(workspace)
    maintenance = MarkdownMemoryMaintenance(
        store=store,
        provider=provider,
        model=model,
        keep_count=keep_count,
        event_bus=event_bus,
        recent_context_provider=recent_context_provider,
        recent_context_model=recent_context_model,
    )
    return MarkdownMemoryRuntime(
        store=store,
        maintenance=maintenance,
        workspace=workspace,
    )
