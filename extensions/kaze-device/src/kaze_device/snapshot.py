from datetime import UTC, datetime, timedelta
import math
from typing import Callable, Protocol

from .config import Settings
from .windows import WindowsProbes


class Probes(Protocol):
    def session(self) -> dict: ...
    def idle(self) -> float: ...
    def foreground(self, *, include_title: bool = False) -> dict: ...
    def power(self) -> dict: ...


class SnapshotService:
    def __init__(
        self,
        settings: Settings,
        probes: Probes | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(UTC))
        self.probes = probes
        self.startup_error = ""
        if self.probes is None:
            try:
                self.probes = WindowsProbes()
            except OSError as exc:
                self.startup_error = str(exc)

    def capture(self) -> dict:
        observed = self.clock().astimezone(UTC)
        errors: dict[str, str] = {}

        def read(name: str, callback):
            try:
                return callback()
            except OSError as exc:
                errors[name] = str(exc)
                return None

        session = idle = foreground = power = None
        if self.probes is None:
            errors["platform"] = self.startup_error
        else:
            session = read("session", self.probes.session)
            if session and session.get("interactive") is False:
                errors["session"] = (
                    "The MCP process is not in an interactive Windows session"
                )
            else:
                idle = read("idle", self.probes.idle)
            if idle is not None and (not math.isfinite(idle) or idle < 0):
                errors["idle"] = "Invalid idle duration"
                idle = None
            if (
                self.settings.foreground_enabled
                and session
                and session["connected"]
                and session["locked"] is False
            ):
                foreground = read(
                    "foreground",
                    lambda: self.probes.foreground(
                        include_title=self.settings.include_window_title,
                    ),
                )
                # Discard an app snapshot if the user locks/disconnects while sampling.
                session = read("session", self.probes.session)
                if (
                    not session
                    or not session["connected"]
                    or session["locked"] is not False
                ):
                    foreground = None
            power = read("power", self.probes.power)

        locked = session.get("locked") if session else None
        connected = session.get("connected") if session else None
        presence, confidence = "unknown", 0.0
        if locked is True or connected is False:
            presence, confidence = "offline", 1.0
        elif locked is False and connected is True and idle is not None:
            process = str((foreground or {}).get("process_name") or "").casefold()
            if process and process in self.settings.game_processes:
                presence, confidence = "in_game", 0.8
            else:
                presence = (
                    "idle" if idle >= self.settings.idle_threshold_seconds else "active"
                )
                confidence = 0.9

        available = any(
            value is not None for value in (session, idle, foreground, power)
        )
        elapsed = (self.clock().astimezone(UTC) - observed).total_seconds()
        stale = elapsed < 0 or elapsed >= self.settings.snapshot_ttl_seconds
        if stale:
            errors["freshness"] = (
                "Collection exceeded the snapshot lifetime or the clock changed; query again"
            )
            available = False
            presence, confidence = "unknown", 0.0
            session = idle = foreground = power = locked = connected = None
        return {
            "kind": "context",
            "context_type": "windows_device",
            "schema_version": 1,
            "available": available,
            "status": "stale"
            if stale
            else "unavailable"
            if not available
            else "partial"
            if errors
            else "ok",
            "observed_at": observed.isoformat(),
            "expires_at": (
                observed + timedelta(seconds=self.settings.snapshot_ttl_seconds)
            ).isoformat(),
            "ttl_seconds": self.settings.snapshot_ttl_seconds,
            "presence": presence,
            "confidence": confidence,
            "locked": locked,
            "session_connected": connected,
            "session_id": session.get("session_id") if session else None,
            "idle_seconds": round(idle, 3) if idle is not None else None,
            "foreground": foreground,
            "power": power,
            "errors": errors,
        }
