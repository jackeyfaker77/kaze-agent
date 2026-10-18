from datetime import UTC, datetime, timedelta

import pytest

from kaze_device.config import Settings
from kaze_device.snapshot import SnapshotService


class FakeProbes:
    def __init__(self, *, locked=False, connected=True, idle=12.5, failed=()):
        self.locked, self.connected, self.duration = locked, connected, idle
        self.failed = set(failed)
        self.calls = []
        self.interactive = True

    def check(self, name):
        self.calls.append(name)
        if name in self.failed:
            raise OSError(f"{name} unavailable")

    def session(self):
        self.check("session")
        return {
            "session_id": 1,
            "locked": self.locked,
            "connected": self.connected,
            "interactive": self.interactive,
        }

    def idle(self):
        self.check("idle")
        return self.duration

    def foreground(self, *, include_title=False):
        self.check("foreground")
        result = {"process_name": "Example.exe", "process_id": 42}
        if include_title:
            result["window_title"] = "用户文档"
        return result

    def power(self):
        self.check("power")
        return {"ac_connected": True, "battery_present": False, "battery_percent": None}


def test_current_facts_have_short_lifetime_and_do_not_imply_availability():
    now = datetime(2026, 10, 16, 8, tzinfo=UTC)
    service = SnapshotService(Settings(), FakeProbes(), clock=lambda: now)
    item = service.capture()
    assert item["available"] is True
    assert item["status"] == "ok"
    assert item["presence"] == "active"
    assert item["idle_seconds"] == 12.5
    assert "window_title" not in item["foreground"]
    assert datetime.fromisoformat(item["expires_at"]) - now == timedelta(seconds=30)


@pytest.mark.parametrize(
    "idle,presence", [(299.9, "active"), (300, "idle"), (999, "idle")]
)
def test_idle_threshold_is_explicit(idle, presence):
    assert (
        SnapshotService(Settings(), FakeProbes(idle=idle)).capture()["presence"]
        == presence
    )


@pytest.mark.parametrize(
    "locked,connected", [(True, True), (False, False), (None, False)]
)
def test_locked_or_disconnected_session_skips_foreground(locked, connected):
    probes = FakeProbes(locked=locked, connected=connected)
    item = SnapshotService(Settings(include_window_title=True), probes).capture()
    assert item["presence"] == "offline"
    assert item["foreground"] is None
    assert "foreground" not in probes.calls


def test_unknown_lock_state_does_not_become_active():
    probes = FakeProbes(locked=None)
    item = SnapshotService(Settings(), probes).capture()
    assert item["presence"] == "unknown"
    assert item["confidence"] == 0
    assert item["foreground"] is None


def test_noninteractive_service_session_does_not_impersonate_the_user():
    probes = FakeProbes(locked=None, connected=None)
    probes.interactive = False
    item = SnapshotService(Settings(), probes).capture()
    assert item["presence"] == "unknown"
    assert item["idle_seconds"] is None
    assert item["foreground"] is None
    assert "idle" not in probes.calls
    assert "session" in item["errors"]


def test_each_call_refreshes_and_failed_fields_do_not_reuse_old_values():
    probes = FakeProbes()
    service = SnapshotService(Settings(), probes)
    assert service.capture()["foreground"]["process_name"] == "Example.exe"
    probes.failed.update({"foreground", "idle"})
    item = service.capture()
    assert item["status"] == "partial"
    assert item["presence"] == "unknown"
    assert item["foreground"] is None
    assert item["idle_seconds"] is None
    assert set(item["errors"]) == {"foreground", "idle"}


def test_all_probes_failed_is_unavailable_with_unknown_fields():
    item = SnapshotService(
        Settings(), FakeProbes(failed=("session", "idle", "power"))
    ).capture()
    assert item["available"] is False
    assert item["status"] == "unavailable"
    assert item["locked"] is None
    assert item["presence"] == "unknown"


def test_optional_titles_and_explicit_game_process_list():
    item = SnapshotService(
        Settings(include_window_title=True, game_processes=("example.exe",)),
        FakeProbes(),
    ).capture()
    assert item["foreground"]["window_title"] == "用户文档"
    assert item["presence"] == "in_game"
    disabled = SnapshotService(
        Settings(foreground_enabled=False), FakeProbes()
    ).capture()
    assert disabled["foreground"] is None
    assert disabled["presence"] == "active"


def test_lock_during_sampling_discards_foreground_details():
    probes = FakeProbes()
    original = probes.foreground

    def foreground(**kwargs):
        value = original(**kwargs)
        probes.locked = True
        return value

    probes.foreground = foreground
    item = SnapshotService(Settings(include_window_title=True), probes).capture()
    assert item["locked"] is True
    assert item["presence"] == "offline"
    assert item["foreground"] is None


@pytest.mark.parametrize("idle", [float("nan"), float("inf"), -0.5])
def test_invalid_idle_readings_are_unknown(idle):
    item = SnapshotService(Settings(), FakeProbes(idle=idle)).capture()
    assert item["idle_seconds"] is None
    assert item["presence"] == "unknown"


def test_expired_collection_discards_observations():
    now = datetime(2026, 10, 16, 8, tzinfo=UTC)
    times = iter((now, now + timedelta(seconds=31)))
    item = SnapshotService(
        Settings(), FakeProbes(), clock=lambda: next(times)
    ).capture()
    assert item["status"] == "stale"
    assert item["available"] is False
    assert item["presence"] == "unknown"
    assert item["foreground"] is None
    assert item["power"] is None


def test_unsupported_platform_still_returns_a_well_formed_snapshot(monkeypatch):
    def unavailable():
        raise OSError("Unsupported platform")

    monkeypatch.setattr("kaze_device.snapshot.WindowsProbes", unavailable)
    item = SnapshotService(Settings()).capture()
    assert item["status"] == "unavailable"
    assert item["locked"] is None
    assert item["errors"] == {"platform": "Unsupported platform"}
