"""Persistent delivery quotas and Shiori's idle-dependent probability gate."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from core.common.timekit import parse_iso
from infra.persistence.json_store import atomic_save_json, load_json
from proactive_v2.config import ProactiveConfig


@dataclass(frozen=True)
class QuotaSnapshot:
    """Successful deliveries in one local-day quota window."""

    window_key: str
    next_reset_at: datetime
    used: int
    last_action_at: datetime | None


class QuotaStore:
    """Keep a single session's quota across process restarts."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def snapshot(
        self, *, now_utc: datetime, reset_hour: int, timezone_name: str
    ) -> QuotaSnapshot:
        # Reload on each read so recreated policies share the persisted counter.
        state = load_json(self.path, default={}, domain="proactive.quota")
        if not isinstance(state, dict):
            raise ValueError("proactive quota must be a JSON object")
        local_now = now_utc.astimezone(ZoneInfo(timezone_name))
        reset = local_now.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
        start = reset if local_now >= reset else reset - timedelta(days=1)
        key = f"{start.date().isoformat()}@{reset_hour:02d}@{timezone_name}"
        return QuotaSnapshot(
            window_key=key,
            next_reset_at=(start + timedelta(days=1)).astimezone(timezone.utc),
            used=int(state.get("used", 0)) if state.get("window_key") == key else 0,
            # Cooldown survives a day rollover even though the count resets.
            last_action_at=parse_iso(state.get("last_action_at")),
        )

    def record_action(
        self, *, now_utc: datetime, reset_hour: int, timezone_name: str
    ) -> None:
        """Record one successful delivery, never a check or failed send."""
        snap = self.snapshot(
            now_utc=now_utc, reset_hour=reset_hour, timezone_name=timezone_name
        )
        atomic_save_json(
            self.path,
            {
                "version": 1,
                "window_key": snap.window_key,
                "next_reset_at": snap.next_reset_at.isoformat(),
                "used": snap.used + 1,
                "last_action_at": now_utc.isoformat(),
            },
            domain="proactive.quota",
        )


class AnyActionGate:
    """Apply quota, delivery cooldown and an idle-dependent random draw."""

    def __init__(
        self,
        *,
        cfg: ProactiveConfig,
        quota_store: QuotaStore,
        rng: random.Random | None = None,
    ) -> None:
        self._cfg = cfg
        self._quota = quota_store
        self._rng = rng

    def should_act(
        self, *, now_utc: datetime, last_user_at: datetime | None
    ) -> tuple[bool, dict[str, float | int | str]]:
        """Evaluate admission without charging the delivery quota."""
        cfg = self._cfg
        if not cfg.anyaction_enabled:
            return True, {"reason": "disabled"}
        snap = self._quota.snapshot(
            now_utc=now_utc,
            reset_hour=cfg.anyaction_reset_hour_local,
            timezone_name=cfg.anyaction_timezone,
        )
        remaining = max(0, cfg.anyaction_daily_max_actions - snap.used)
        meta: dict[str, float | int | str] = {
            "used_today": snap.used,
            "remaining_today": remaining,
        }
        if remaining <= 0:
            return False, {**meta, "reason": "quota_exhausted"}
        if snap.last_action_at is not None:
            elapsed = max(0.0, (now_utc - snap.last_action_at).total_seconds())
            if elapsed < cfg.anyaction_min_interval_seconds:
                return False, {
                    **meta,
                    "reason": "min_interval",
                    "seconds_since_last_action": elapsed,
                }
        idle = (
            max(0.0, (now_utc - last_user_at).total_seconds() / 60.0)
            if last_user_at is not None
            else cfg.anyaction_idle_scale_minutes * 2.0
        )
        factor = 1.0 - math.exp(-idle / cfg.anyaction_idle_scale_minutes)
        probability = (
            cfg.anyaction_probability_min
            + (cfg.anyaction_probability_max - cfg.anyaction_probability_min) * factor
        )
        draw = (self._rng or random).random()
        return draw < probability, {
            **meta,
            "reason": "probability",
            "idle_minutes": idle,
            "p_act": probability,
            "draw": draw,
        }

    def record_action(self, *, now_utc: datetime) -> None:
        """Charge quota after the transport confirms success."""
        self._quota.record_action(
            now_utc=now_utc,
            reset_hour=self._cfg.anyaction_reset_hour_local,
            timezone_name=self._cfg.anyaction_timezone,
        )
