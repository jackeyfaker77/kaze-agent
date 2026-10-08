import pytest
from proactive_v2.config_loader import ProactiveConfigError, load_proactive_config


def test_loads_session_and_transport_settings():
    config = load_proactive_config(
        {
            "enabled": True,
            "session_key": "daily",
            "target": {"channel": "telegram", "chat_id": "42"},
            "interval_seconds": 600,
        }
    )
    assert config.enabled
    assert (config.session_key, config.default_channel, config.default_chat_id) == (
        "daily",
        "telegram",
        "42",
    )
    assert config.interval_seconds == 600


@pytest.mark.parametrize("interval", [0, 59, "bad", None])
def test_invalid_interval_is_rejected(interval):
    with pytest.raises(ProactiveConfigError, match="interval_seconds"):
        load_proactive_config({"interval_seconds": interval})


def test_retired_role_settings_do_not_override_plain_session():
    config = load_proactive_config(
        {
            "default_role_id": "old",
            "agent": {"max_steps": 9},
            "drift": {"enabled": True},
            "session_key": "desktop:new",
        }
    )
    assert config.session_key == "desktop:new"
    assert not hasattr(config, "default_role_id")


@pytest.mark.parametrize(
    "profile,intervals",
    [("daily", (480, 240)), ("quiet", (1800, 900)), ("dev_verify", (60, 30))],
)
def test_shiori_cadence_profiles_are_restored(profile, intervals):
    config = load_proactive_config({"profile": profile})
    assert (config.tick_interval_s0, config.tick_interval_s1) == intervals
    assert config.adaptive_enabled and config.anyaction_enabled


def test_nested_admission_settings_and_trigger_overrides_are_loaded():
    config = load_proactive_config(
        {
            "profile": "quiet",
            "adaptive_enabled": False,
            "overrides": {
                "trigger": {
                    "tick_interval_s0": 600,
                    "tick_interval_s1": 180,
                    "tick_jitter": 0,
                }
            },
            "anyaction": {
                "daily_max_actions": 3,
                "min_interval_seconds": 3600,
                "timezone": "UTC",
            },
        }
    )
    assert (config.tick_interval_s0, config.tick_interval_s1) == (600, 180)
    assert config.tick_jitter == 0
    assert not config.adaptive_enabled
    assert config.anyaction_daily_max_actions == 3
    assert config.anyaction_min_interval_seconds == 3600
    assert config.anyaction_timezone == "UTC"


def test_custom_profiles_merge_before_overrides():
    config = load_proactive_config(
        {
            "profile": "custom",
            "profiles": {
                "custom": {
                    "trigger": {"tick_interval_s0": 900, "tick_interval_s1": 300}
                }
            },
            "overrides": {"trigger": {"tick_interval_s1": 180}},
        }
    )
    assert (config.tick_interval_s0, config.tick_interval_s1) == (900, 180)


@pytest.mark.parametrize(
    "settings",
    [
        {"profile": "missing"},
        {"target": []},
        {"anyaction": []},
        {"adaptive_enabled": "false"},
        {"tick_jitter": 1},
        {"tick_jitter": float("nan")},
        {"tick_interval_s1": 0},
        {"tick_interval_s0": 60},
        {"interval_seconds": 60.5},
        {"energy_contact_threshold": 2},
        {"delivery_dedupe_hours": 0},
        {"delivery_dedupe_hours": float("inf")},
        {"message_dedupe_enabled": "false"},
        {"message_dedupe_recent_n": 0},
        {"message_dedupe_recent_n": 51},
        {"anyaction": {"daily_max_actions": -1}},
        {"anyaction": {"probability_min": 0.9, "probability_max": 0.1}},
        {"anyaction": {"timezone": "invalid"}},
        {"anyaction": {"idle_scale_minutes": 0}},
        {"anyaction": {"reset_hour_local": 24}},
        {"overrides": {"trigger": {"typo": 1}}},
    ],
)
def test_invalid_policies_fail_at_load(settings):
    with pytest.raises(ProactiveConfigError):
        load_proactive_config(settings)
