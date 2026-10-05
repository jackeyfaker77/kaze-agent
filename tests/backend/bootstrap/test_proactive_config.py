import pytest
from proactive_v2.config_loader import ProactiveConfigError, load_proactive_config


def test_loads_session_and_transport_settings():
    config = load_proactive_config({"enabled": True, "session_key": "daily",
        "target": {"channel": "telegram", "chat_id": "42"}, "interval_seconds": 600})
    assert config.enabled
    assert (config.session_key, config.default_channel, config.default_chat_id) == ("daily", "telegram", "42")
    assert config.interval_seconds == 600


@pytest.mark.parametrize("interval", [0, 59, "bad", None])
def test_invalid_interval_is_rejected(interval):
    with pytest.raises(ProactiveConfigError, match="interval_seconds"):
        load_proactive_config({"interval_seconds": interval})


def test_retired_role_settings_do_not_override_plain_session():
    config = load_proactive_config({"default_role_id": "old", "agent": {"max_steps": 9},
        "drift": {"enabled": True}, "session_key": "desktop:new"})
    assert config.session_key == "desktop:new"
    assert not hasattr(config, "default_role_id")
