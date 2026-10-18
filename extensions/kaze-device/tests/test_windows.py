import ctypes as ct
from types import SimpleNamespace

import pytest

from kaze_device.windows import (
    HANDLE,
    U32,
    SystemPowerStatus,
    WindowsProbes,
    WtsInfoEx,
    idle_seconds,
    power_payload,
)


def test_idle_timestamp_wraparound_after_long_uptime():
    assert idle_seconds(0x100000100, 0xFFFFFF00) == 0.512
    assert idle_seconds(123_456, 120_000) == 3.456


@pytest.mark.parametrize(
    "flags,percent,present,charge",
    [
        (0, 55, True, False),
        (8, 10, True, True),
        (128, 100, False, None),
        (255, 90, None, None),
        (0, 255, True, False),
    ],
)
def test_battery_unknown_and_absent_are_distinct(flags, percent, present, charge):
    raw = SystemPowerStatus(ac=0, flags=flags, percent=percent, saver=0, remaining=60)
    result = power_payload(raw)
    assert result["battery_present"] is present
    assert result["charging"] is charge
    assert result["battery_percent"] == (
        percent if present is True and percent <= 100 else None
    )


def test_power_sentinels_are_not_negative_battery_estimates():
    raw = SystemPowerStatus(
        ac=255, flags=0, percent=255, saver=255, remaining=0xFFFFFFFF
    )
    result = power_payload(raw)
    assert result["ac_connected"] is None
    assert result["battery_saver"] is None
    assert result["battery_life_seconds"] is None


@pytest.fixture
def native(monkeypatch):
    # Native functions are replaced at the DLL boundary; memory layout and cleanup stay real.
    monkeypatch.setattr(ct, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(ct, "set_last_error", lambda _: None, raising=False)
    probes = WindowsProbes.__new__(WindowsProbes)
    probes.user = SimpleNamespace()
    probes.kernel = SimpleNamespace()
    probes.wts = SimpleNamespace()
    return probes


def configure_session(native, *, level=1, flags=1, state=0, session_id=2, size=None):
    raw = WtsInfoEx(level=level)
    raw.data.session_id, raw.data.flags, raw.data.state = session_id, flags, state
    freed = []

    def query(server, session, info, pointer, length):
        assert server is None and session == 0xFFFFFFFF and info == 25
        ct.cast(pointer, ct.POINTER(HANDLE))[0] = ct.addressof(raw)
        ct.cast(length, ct.POINTER(U32))[0] = ct.sizeof(raw) if size is None else size
        return 1

    native.wts.WTSQuerySessionInformationW = query
    native.wts.WTSFreeMemory = lambda pointer: freed.append(pointer.value)
    return raw, freed


@pytest.mark.parametrize("flags,locked", [(0, True), (1, False), (-1, None)])
def test_session_lock_flags_and_native_memory_release(native, flags, locked):
    raw, freed = configure_session(native, flags=flags)
    value = native.session()
    assert value["locked"] is locked
    assert value["connected"] is True
    assert freed == [ct.addressof(raw)]


@pytest.mark.parametrize("changes", [{"level": 7}, {"size": 12}])
def test_invalid_session_buffers_are_rejected_and_freed(native, changes):
    raw, freed = configure_session(native, **changes)
    with pytest.raises(OSError):
        native.session()
    assert freed == [ct.addressof(raw)]


def test_session_zero_does_not_claim_an_interactive_user(native):
    configure_session(native, session_id=0)
    assert native.session() == {
        "session_id": 0,
        "connected": None,
        "locked": None,
        "interactive": False,
    }


def configure_foreground(native, *, succeeds=True, changes=False):
    closed, titles = [], []
    windows = iter((10, 11 if changes else 10))
    native.user.GetForegroundWindow = lambda: next(windows)

    def thread_id(window, process):
        ct.cast(process, ct.POINTER(U32))[0] = 42
        return 1

    def query(handle, flags, path, capacity):
        path.value = "C:\\Private\\User\\Applications\\Editor.exe"
        return int(succeeds)

    def title(window, text, capacity):
        titles.append(window)
        text.value = "文档标题"
        return len(text.value)

    native.user.GetWindowThreadProcessId = thread_id
    native.user.GetWindowTextW = title
    native.kernel.OpenProcess = lambda access, inherit, pid: (
        99 if access == 0x1000 else None
    )
    native.kernel.QueryFullProcessImageNameW = query
    native.kernel.CloseHandle = lambda handle: closed.append(handle)
    return closed, titles


def test_foreground_only_exports_process_name_and_releases_handle(native):
    closed, titles = configure_foreground(native)
    assert native.foreground() == {"process_id": 42, "process_name": "Editor.exe"}
    assert closed == [99]
    assert titles == []


def test_foreground_title_is_opt_in(native):
    _, titles = configure_foreground(native)
    assert native.foreground(include_title=True)["window_title"] == "文档标题"
    assert titles == [10]


def test_truncated_surrogate_in_window_title_cannot_break_mcp_utf8(native):
    configure_foreground(native)

    def title(window, text, capacity):
        text.value = "Caption\ud83d"
        return len(text.value)

    native.user.GetWindowTextW = title
    value = native.foreground(include_title=True)["window_title"]
    assert value == "Caption?"
    assert value.encode("utf-8")


@pytest.mark.parametrize("succeeds,changes", [(False, False), (True, True)])
def test_failed_or_racing_foreground_query_does_not_leave_a_handle_open(
    native, succeeds, changes
):
    closed, _ = configure_foreground(native, succeeds=succeeds, changes=changes)
    with pytest.raises(OSError):
        native.foreground()
    assert closed == [99]
