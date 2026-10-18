"""Read-only Win32 probes scoped to the calling process's Windows session."""

import ctypes as ct
from pathlib import PureWindowsPath
import sys

U32, I32, U16, U8 = ct.c_uint32, ct.c_int32, ct.c_uint16, ct.c_ubyte
BOOL, HANDLE = I32, ct.c_void_p


class LastInputInfo(ct.Structure):
    _fields_ = [("size", U32), ("tick", U32)]


class SystemPowerStatus(ct.Structure):
    _fields_ = [
        ("ac", U8),
        ("flags", U8),
        ("percent", U8),
        ("saver", U8),
        ("remaining", U32),
        ("full", U32),
    ]


class WtsLevel1(ct.Structure):
    _fields_ = [
        ("session_id", U32),
        ("state", I32),
        ("flags", I32),
        ("station", U16 * 33),
        ("user", U16 * 21),
        ("domain", U16 * 18),
        ("logon", ct.c_int64),
        ("connect", ct.c_int64),
        ("disconnect", ct.c_int64),
        ("last_input", ct.c_int64),
        ("current", ct.c_int64),
        ("incoming_bytes", U32),
        ("outgoing_bytes", U32),
        ("incoming_frames", U32),
        ("outgoing_frames", U32),
        ("incoming_compressed", U32),
        ("outgoing_compressed", U32),
    ]


class WtsInfoEx(ct.Structure):
    # WTSINFOEX_LEVEL_W currently contains only the level-1 structure.
    _fields_ = [("level", U32), ("data", WtsLevel1)]


def idle_seconds(current_tick: int, last_input_tick: int) -> float:
    # LASTINPUTINFO.dwTime is a DWORD even when uptime exceeds 49.7 days.
    return ((current_tick - last_input_tick) & 0xFFFFFFFF) / 1000.0


def power_payload(raw: SystemPowerStatus) -> dict:
    present = None if raw.flags == 255 else not bool(raw.flags & 128)
    return {
        "ac_connected": {0: False, 1: True}.get(raw.ac),
        "battery_present": present,
        "battery_percent": raw.percent
        if present is True and raw.percent <= 100
        else None,
        "charging": bool(raw.flags & 8) if present is True else None,
        "battery_saver": {0: False, 1: True}.get(raw.saver),
        "battery_life_seconds": raw.remaining
        if present is True and raw.ac == 0 and raw.remaining != 0xFFFFFFFF
        else None,
    }


class WindowsProbes:
    def __init__(self) -> None:
        if sys.platform != "win32" or sys.getwindowsversion().major < 10:
            raise OSError("Windows 10 or later is required")
        self.user = ct.WinDLL("user32", use_last_error=True)
        self.kernel = ct.WinDLL("kernel32", use_last_error=True)
        self.wts = ct.WinDLL("wtsapi32", use_last_error=True)
        self._bind()

    def _bind(self) -> None:
        definitions = (
            (self.user.GetLastInputInfo, [ct.POINTER(LastInputInfo)], BOOL),
            (self.kernel.GetTickCount64, [], ct.c_uint64),
            (self.kernel.GetSystemPowerStatus, [ct.POINTER(SystemPowerStatus)], BOOL),
            (self.user.GetForegroundWindow, [], HANDLE),
            (self.user.GetWindowThreadProcessId, [HANDLE, ct.POINTER(U32)], U32),
            (self.user.GetWindowTextW, [HANDLE, ct.c_wchar_p, I32], I32),
            (self.kernel.OpenProcess, [U32, BOOL, U32], HANDLE),
            (
                self.kernel.QueryFullProcessImageNameW,
                [HANDLE, U32, ct.c_wchar_p, ct.POINTER(U32)],
                BOOL,
            ),
            (self.kernel.CloseHandle, [HANDLE], BOOL),
            (
                self.wts.WTSQuerySessionInformationW,
                [HANDLE, U32, I32, ct.POINTER(HANDLE), ct.POINTER(U32)],
                BOOL,
            ),
            (self.wts.WTSFreeMemory, [HANDLE], None),
        )
        for function, arguments, result in definitions:
            function.argtypes, function.restype = arguments, result

    @staticmethod
    def _error(api: str) -> OSError:
        # Error codes are sufficient diagnostics; no user names or process paths.
        return OSError(f"{api} failed (Win32 error {ct.get_last_error()})")

    def session(self) -> dict:
        pointer, length = HANDLE(), U32()
        try:
            if not self.wts.WTSQuerySessionInformationW(
                None,
                0xFFFFFFFF,
                25,
                ct.byref(pointer),
                ct.byref(length),
            ):
                raise self._error("WTSQuerySessionInformationW")
            if not pointer.value or length.value < ct.sizeof(WtsInfoEx):
                raise OSError("WTS session response was truncated")
            raw = ct.cast(pointer, ct.POINTER(WtsInfoEx)).contents
            if raw.level != 1:
                raise OSError("Unsupported WTS session information level")
            interactive = raw.data.session_id != 0
            return {
                "session_id": raw.data.session_id,
                "connected": raw.data.state == 0 if interactive else None,
                "locked": {0: True, 1: False}.get(raw.data.flags)
                if interactive
                else None,
                "interactive": interactive,
            }
        finally:
            if pointer.value:
                self.wts.WTSFreeMemory(pointer)

    def idle(self) -> float:
        raw = LastInputInfo(size=ct.sizeof(LastInputInfo))
        if not self.user.GetLastInputInfo(ct.byref(raw)):
            raise self._error("GetLastInputInfo")
        return idle_seconds(self.kernel.GetTickCount64(), raw.tick)

    def foreground(self, *, include_title: bool = False) -> dict:
        window = self.user.GetForegroundWindow()
        if not window:
            raise OSError("No foreground window is available")
        process_id = U32()
        if (
            not self.user.GetWindowThreadProcessId(window, ct.byref(process_id))
            or not process_id.value
        ):
            raise self._error("GetWindowThreadProcessId")
        handle = self.kernel.OpenProcess(0x1000, False, process_id.value)
        if not handle:
            raise self._error("OpenProcess")
        try:
            capacity = U32(32768)
            path = ct.create_unicode_buffer(capacity.value)
            if not self.kernel.QueryFullProcessImageNameW(
                handle, 0, path, ct.byref(capacity)
            ):
                raise self._error("QueryFullProcessImageNameW")
            payload = {
                "process_id": process_id.value,
                "process_name": PureWindowsPath(path.value)
                .name.encode("utf-8", "replace")
                .decode("utf-8"),
            }
        finally:
            self.kernel.CloseHandle(handle)
        if include_title:
            title = ct.create_unicode_buffer(513)
            ct.set_last_error(0)
            length = self.user.GetWindowTextW(window, title, len(title))
            if length == 0 and ct.get_last_error():
                raise self._error("GetWindowTextW")
            # A UTF-16 caption truncated at the buffer boundary may split a surrogate pair.
            payload["window_title"] = (
                title.value[:512].encode("utf-8", "replace").decode("utf-8")
            )
        if self.user.GetForegroundWindow() != window:
            raise OSError("Foreground window changed during collection; query again")
        return payload

    def power(self) -> dict:
        raw = SystemPowerStatus()
        if not self.kernel.GetSystemPowerStatus(ct.byref(raw)):
            raise self._error("GetSystemPowerStatus")
        return power_payload(raw)
