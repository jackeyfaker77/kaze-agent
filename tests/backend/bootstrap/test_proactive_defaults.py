from proactive_v2.config import ProactiveConfig


def test_heartbeat_defaults_are_disabled_and_session_scoped():
    config = ProactiveConfig()
    assert not config.enabled
    assert config.default_channel == "desktop"
    assert config.session_key == ""
    assert config.interval_seconds == 1800
    assert not hasattr(config, "default_role_id")
