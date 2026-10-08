"""Persisted delivery budgets, local reset boundaries and probability gates."""

import json
import random
from datetime import datetime, timedelta, timezone

import pytest

from proactive_v2.anyaction import AnyActionGate, QuotaStore
from proactive_v2.config_loader import load_proactive_config

NOW = datetime(2026, 10, 8, 4, tzinfo=timezone.utc)


def _gate(tmp_path, **changes):
    cfg = load_proactive_config({"anyaction_reset_hour_local": 8, **changes})
    quota = QuotaStore(tmp_path / "quota.json")
    return AnyActionGate(cfg=cfg, quota_store=quota, rng=random.Random(1)), quota


def test_quota_reloads_and_resets_at_shanghai_eight_am(tmp_path):
    path = tmp_path / "quota.json"
    before_reset = datetime(2026, 10, 7, 23, 59, tzinfo=timezone.utc)
    quota = QuotaStore(path)
    quota.record_action(
        now_utc=before_reset, reset_hour=8, timezone_name="Asia/Shanghai"
    )
    reopened = QuotaStore(path)
    snap = reopened.snapshot(
        now_utc=before_reset, reset_hour=8, timezone_name="Asia/Shanghai"
    )
    assert snap.used == 1
    assert snap.next_reset_at == datetime(2026, 10, 8, tzinfo=timezone.utc)
    next_day = reopened.snapshot(
        now_utc=snap.next_reset_at, reset_hour=8, timezone_name="Asia/Shanghai"
    )
    assert next_day.used == 0
    assert next_day.last_action_at == before_reset


def test_admission_does_not_charge_quota_and_exhaustion_survives_restart(tmp_path):
    gate, quota = _gate(
        tmp_path,
        anyaction_daily_max_actions=1,
        anyaction_probability_min=1,
        anyaction_probability_max=1,
    )
    for _ in range(3):
        assert gate.should_act(now_utc=NOW, last_user_at=NOW)[0]
    assert not quota.path.exists()
    gate.record_action(now_utc=NOW)
    reopened = AnyActionGate(
        cfg=load_proactive_config(
            {"anyaction_daily_max_actions": 1, "anyaction_reset_hour_local": 8}
        ),
        quota_store=QuotaStore(quota.path),
    )
    allowed, meta = reopened.should_act(
        now_utc=NOW + timedelta(hours=1), last_user_at=NOW
    )
    assert not allowed
    assert meta["reason"] == "quota_exhausted"


def test_cooldown_survives_day_rollover(tmp_path):
    gate, _ = _gate(tmp_path, anyaction_min_interval_seconds=1800)
    before_reset = datetime(2026, 10, 7, 23, 59, tzinfo=timezone.utc)
    gate.record_action(now_utc=before_reset)
    allowed, meta = gate.should_act(
        now_utc=before_reset + timedelta(minutes=2), last_user_at=None
    )
    assert not allowed
    assert meta["reason"] == "min_interval"
    assert meta["used_today"] == 0


def test_idle_time_increases_probability_with_same_draw(tmp_path):
    gate, _ = _gate(
        tmp_path, anyaction_probability_min=0.1, anyaction_probability_max=0.9
    )
    allowed_recent, recent = gate.should_act(now_utc=NOW, last_user_at=NOW)
    gate, _ = _gate(
        tmp_path, anyaction_probability_min=0.1, anyaction_probability_max=0.9
    )
    allowed_idle, idle = gate.should_act(
        now_utc=NOW, last_user_at=NOW - timedelta(hours=12)
    )
    assert recent["draw"] == idle["draw"]
    assert not allowed_recent and allowed_idle
    recent_probability = recent["p_act"]
    idle_probability = idle["p_act"]
    assert isinstance(recent_probability, float)
    assert isinstance(idle_probability, float)
    assert recent_probability < idle_probability


def test_zero_probability_never_admits_and_disabled_gate_admits(tmp_path):
    gate, _ = _gate(tmp_path, anyaction_probability_min=0, anyaction_probability_max=0)
    assert not gate.should_act(now_utc=NOW, last_user_at=None)[0]
    gate, _ = _gate(tmp_path, anyaction_enabled=False, anyaction_daily_max_actions=0)
    assert gate.should_act(now_utc=NOW, last_user_at=None)[0]


@pytest.mark.parametrize("content", ["{bad", "[]"])
def test_corrupt_quota_is_not_silently_reset(tmp_path, content):
    gate, quota = _gate(tmp_path)
    quota.path.write_text(content, encoding="utf-8")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        gate.should_act(now_utc=NOW, last_user_at=None)
