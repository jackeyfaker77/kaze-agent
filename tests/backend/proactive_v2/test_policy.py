"""Exercise adaptive policy using the real session presence store."""

from datetime import datetime, timedelta, timezone

from proactive_v2.anyaction import QuotaStore
from proactive_v2.config_loader import load_proactive_config
from proactive_v2.policy import ProactivePolicy
from proactive_v2.presence import PresenceStore
from session.store import SessionStore

NOW = datetime(2026, 10, 8, 4, tzinfo=timezone.utc)
KEY = "telegram:42"


def _policy(tmp_path, **changes):
    presence = PresenceStore(SessionStore(tmp_path / "sessions.db"))
    policy = ProactivePolicy(
        config=load_proactive_config({"tick_jitter": 0, **changes}),
        session_key=KEY,
        presence=presence,
        quota_store=QuotaStore(tmp_path / "quota.json"),
    )
    return policy, presence


def test_target_reply_recharges_energy_and_slows_cadence(tmp_path):
    policy, presence = _policy(tmp_path)
    presence.record_user_message(KEY, NOW - timedelta(hours=24))
    assert policy.next_interval(NOW) == 240
    snapshot = policy.sense(NOW)
    assert policy.can_contact_without_candidates(snapshot)
    presence.record_user_message(KEY, NOW)
    assert policy.next_interval(NOW) == 480
    assert policy.sense(NOW).energy == 1
    assert policy.user_replied_since(snapshot)
    assert not policy.can_contact_without_candidates(policy.sense(NOW))


def test_recent_global_activity_reduces_cross_session_interruption(tmp_path):
    policy, presence = _policy(tmp_path)
    presence.record_user_message(KEY, NOW - timedelta(hours=24))
    presence.record_user_message("desktop:other", NOW)
    snapshot = policy.sense(NOW)
    assert snapshot.energy == 0.6
    assert policy.next_interval(NOW) == 480
    assert not policy.can_contact_without_candidates(snapshot)


def test_unknown_session_does_not_initiate_contact(tmp_path):
    policy, _ = _policy(tmp_path)
    assert not policy.can_contact_without_candidates(policy.sense(NOW))


def test_contact_can_be_disabled_independently_of_cadence(tmp_path):
    policy, presence = _policy(tmp_path, energy_contact_enabled=False)
    presence.record_user_message(KEY, NOW - timedelta(hours=24))
    assert policy.next_interval(NOW) == 240
    assert not policy.can_contact_without_candidates(policy.sense(NOW))


def test_fixed_interval_without_presence_or_when_adaptation_disabled(tmp_path):
    policy, _ = _policy(tmp_path, interval_seconds=600, adaptive_enabled=False)
    assert policy.next_interval(NOW) == 600
    policy.config.adaptive_enabled = True
    policy.presence = None
    assert policy.next_interval(NOW) == 600


def test_existing_delivery_presence_enforces_cooldown(tmp_path):
    policy, presence = _policy(tmp_path)
    presence.record_proactive_sent(KEY, NOW - timedelta(minutes=1))
    allowed, metadata = policy.should_act(policy.sense(NOW), NOW)
    assert not allowed
    assert metadata["reason"] == "min_interval"
