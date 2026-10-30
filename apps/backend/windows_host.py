"""Console-free Task Scheduler entry point; runs the Host in this process.

No supervisor child is left behind if this process dies. Task Scheduler owns
restart policy; the existing workspace lock, journal and queues own recovery.
"""
from __future__ import annotations

import logging
import json
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import traceback


class LogStream:
    encoding = "utf-8"

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handler = RotatingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(message)s"))

    def write(self, value: str) -> int:
        if value.strip():
            self.handler.handle(logging.LogRecord("host-console", logging.INFO, "", 0, value.rstrip("\r\n"), (), None))
        return len(value)

    def flush(self) -> None:
        self.handler.flush()

    def isatty(self) -> bool:
        return False


def run() -> int:
    # Deployment passes an absolute workspace. Do not silently choose another root.
    args = sys.argv[1:]
    if not args or args[0] != "host" or "--workspace" not in args:
        return 2
    workspace = Path(args[args.index("--workspace") + 1])
    if not workspace.is_absolute():
        return 2
    from core.common.restore_safety import RestoreQuarantined, require_live_workspace
    try:
        require_live_workspace(workspace)
        if "--config" in args:
            require_live_workspace(Path(args[args.index("--config") + 1]).resolve().parent)
    except RestoreQuarantined:
        return 0  # Do not even create logs inside an isolated restore.
    pause_file = workspace / "private_runtime/windows-host/paused.json"
    if pause_file.exists():
        try:
            pause = json.loads(pause_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return 0  # An unreadable explicit stop must not silently resume.
        if not isinstance(pause, dict) or pause.get("exit_code") != 2:
            return 0
        # Older deployments persisted a pause for a temporary owner conflict.
        # Such a record is not a user's stop request; allow the next guard tick.
    stream = LogStream(workspace / "private_runtime/windows-host/host.log")
    sys.stdout = sys.stderr = stream  # pythonw has no console streams.
    sys.stdin = open(os.devnull, encoding="utf-8")
    try:
        from main import main
        result = main(args)
        # A competing launcher (or status probe) must not disable the owner's
        # future recovery. The scheduler may try again after the lock is free.
        if result in {2, 3}:
            print(f"Host startup skipped without pausing deployment: exit={result}")
            return 0
        if result == 0:
            from bootstrap.host_state import _write
            _write(pause_file, {"exit_code": result, "reason": "normal stop; use deployment Start to resume"})
            print(f"Host stopped without automatic retry: exit={result}")
            return 0
        return result
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if code == 0:
            from bootstrap.host_state import _write
            _write(pause_file, {"exit_code": code})
        return 0 if code in {2, 3} else code
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        stream.flush()


if __name__ == "__main__":
    raise SystemExit(run())
