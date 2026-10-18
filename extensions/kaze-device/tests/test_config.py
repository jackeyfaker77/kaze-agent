import json

import pytest

from kaze_device.config import Settings


@pytest.mark.parametrize(
    "values",
    [
        {"snapshot_ttl_seconds": 0},
        {"snapshot_ttl_seconds": 301},
        {"snapshot_ttl_seconds": True},
        {"idle_threshold_seconds": -1},
        {"foreground_enabled": "false"},
        {"include_window_title": 1},
        {"game_processes": "game.exe"},
        {"game_processes": ["C:/game.exe"]},
        {"game_processes": [""]},
        {"game_processes": [None]},
    ],
)
def test_invalid_settings_are_rejected(values):
    with pytest.raises(ValueError):
        Settings(**values)


def test_process_names_are_normalized_without_guessing_games():
    assert Settings().game_processes == ()
    assert Settings(game_processes=[" Game.EXE ", "game.exe"]).game_processes == (
        "game.exe",
    )


def test_config_file_and_unknown_settings(monkeypatch, tmp_path):
    path = tmp_path / "device.json"
    path.write_text(json.dumps({"include_window_title": True}), encoding="utf-8")
    monkeypatch.setenv("KAZE_DEVICE_CONFIG", str(path))
    assert Settings.from_env().include_window_title is True
    path.write_text(json.dumps({"typo": True}), encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown"):
        Settings.from_env()


def test_default_config_does_not_enable_window_titles(monkeypatch):
    monkeypatch.delenv("KAZE_DEVICE_CONFIG", raising=False)
    assert Settings.from_env().include_window_title is False
