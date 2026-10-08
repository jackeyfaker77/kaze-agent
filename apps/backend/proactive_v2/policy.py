"""Adapt Shiori's energy and admission algorithms to Kaze session presence."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from core.common.timekit import utcnow
from proactive_v2.anyaction import AnyActionGate, QuotaStore
from proactive_v2.config import ProactiveConfig
from proactive_v2.energy import compute_energy, d_energy, next_tick_from_score


class PresenceReader(Protocol):
    """Presence data required by a session's proactive policy."""

    def get_last_user_at(self, session_key: str) -> datetime | None: ...
    def get_last_proactive_at(self, session_key: str) -> datetime | None: ...
    def most_recent_user_at(self) -> datetime | None: ...


@dataclass(frozen=True)
class EnergySnapshot:
    """The target's last interaction and effective energy at a check."""

    last_user_at: datetime | None
    energy: float
    base_score: float


class ProactivePolicy:
    """Own cadence, empty-feed contact admission and successful-send quota."""

    def __init__(
        self,
        *,
        config: ProactiveConfig,
        session_key: str,
        presence: PresenceReader | None,
        quota_store: QuotaStore,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config
        self.session_key = session_key
        self.presence = presence
        self._rng = rng
        self._gate = AnyActionGate(cfg=config, quota_store=quota_store, rng=rng)

    def sense(self, now: datetime | None = None) -> EnergySnapshot:
        """Use target energy or 60% of global energy, whichever is greater."""
        now = now or utcnow()
        target = (
            self.presence.get_last_user_at(self.session_key) if self.presence else None
        )
        global_last = self.presence.most_recent_user_at() if self.presence else None
        energy = max(
            compute_energy(target, now), 0.6 * compute_energy(global_last, now)
        )
        return EnergySnapshot(
            target, energy, self.config.score_weight_energy * d_energy(energy)
        )

    def next_interval(self, now: datetime | None = None) -> int:
        """Recompute cadence from current presence on every iteration."""
        if not self.config.adaptive_enabled or self.presence is None:
            return max(60, self.config.interval_seconds)
        return next_tick_from_score(
            self.sense(now).base_score,
            tick_s0=self.config.tick_interval_s0,
            tick_s1=self.config.tick_interval_s1,
            tick_jitter=self.config.tick_jitter,
            rng=self._rng,
        )

    def can_contact_without_candidates(self, snapshot: EnergySnapshot) -> bool:
        """Allow a low-energy check only for a previously contacted session."""
        return (
            self.config.energy_contact_enabled
            and snapshot.last_user_at is not None
            and snapshot.energy <= self.config.energy_contact_threshold
        )

    def should_act(
        self, snapshot: EnergySnapshot, now: datetime | None = None
    ) -> tuple[bool, dict[str, float | int | str]]:
        """Respect existing presence cooldown before drawing the admission gate."""
        now = now or utcnow()
        if self.config.anyaction_enabled and self.presence is not None:
            last_push = self.presence.get_last_proactive_at(self.session_key)
            if (
                last_push is not None
                and (now - last_push).total_seconds()
                < self.config.anyaction_min_interval_seconds
            ):
                return False, {"reason": "min_interval"}
        return self._gate.should_act(now_utc=now, last_user_at=snapshot.last_user_at)

    def user_replied_since(self, snapshot: EnergySnapshot) -> bool:
        """Detect user activity while candidates or a response were being fetched."""
        current = (
            self.presence.get_last_user_at(self.session_key) if self.presence else None
        )
        return current is not None and (
            snapshot.last_user_at is None or current > snapshot.last_user_at
        )

    def record_delivery(self, now: datetime | None = None) -> None:
        """Persist delivery usage even when admission is temporarily disabled."""
        self._gate.record_action(now_utc=now or utcnow())
