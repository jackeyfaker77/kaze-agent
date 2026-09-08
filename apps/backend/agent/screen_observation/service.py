from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent.provider import LLMProvider
from core.memory.engine import MemoryWriteApi
from agent.screen_observation.memory import ObservationMemoryWriter
from agent.screen_observation.model import ObservationModelAdapter


class ScreenObservationService:
    """Composes role screen analysis and common-memory episode persistence."""

    def __init__(
        self,
        *,
        provider: LLMProvider | None,
        model: str,
        memory: MemoryWriteApi,
    ) -> None:
        self._model_adapter = ObservationModelAdapter(
            provider=provider,
            model=model,
        )
        self._memory_writer = ObservationMemoryWriter(
            memory=memory,
        )

    async def analyze(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Delegates an ephemeral frame to the observation model adapter."""

        return await self._model_adapter.analyze(payload)

    async def remember(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Delegates a settled episode to the common memory writer."""

        return await self._memory_writer.remember(payload)


def build_screen_observation_service(
    *,
    provider: LLMProvider,
    model: str,
    memory: MemoryWriteApi,
) -> ScreenObservationService:
    """Builds the default role capability with role-owned visual selection."""

    return ScreenObservationService(
        provider=provider,
        model=model,
        memory=memory,
    )
