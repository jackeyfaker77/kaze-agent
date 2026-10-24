"""Local workspace ownership and control records, independent of any UI or socket."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from core.execution.lease import EpochLease


class WorkspaceInUse(RuntimeError):
    pass


def _directory(workspace: Path) -> Path:
    return workspace.resolve() / "private_runtime" / "host"


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write(path: Path, data: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class WorkspaceOwner:
    """Keep the lock file in place: unlinking a live lock can create two owners."""

    def __init__(self, workspace: Path, mode: str):
        self.directory = _directory(workspace)
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            self.lease = EpochLease(self.directory / "owner.lock")
        except (BlockingIOError, PermissionError) as exc:
            raise WorkspaceInUse("工作区已有后端运行；请先退出原桌面本地模式或停止 Host，避免重复轮询和推送") from exc
        self.owner_id = uuid4().hex
        self.base = {"owner_id": self.owner_id, "pid": os.getpid(), "mode": mode,
                     "started_at": datetime.now(timezone.utc).isoformat()}

    def publish(self, state: str, **details) -> None:
        _write(self.directory / "status.json", {**self.base, **details, "state": state,
               "updated_at": datetime.now(timezone.utc).isoformat()})

    def stop_requested(self) -> bool:
        return _read(self.directory / "stop.json").get("owner_id") == self.owner_id

    def close(self) -> None:
        self.lease.close()


def host_status(workspace: Path) -> dict:
    directory = _directory(workspace)
    if not (directory / "owner.lock").exists():
        return {"running": False, "healthy": False, "state": "stopped"}
    # A status/PID file alone never proves that a host is alive.
    try:
        lease = EpochLease(directory / "owner.lock")
    except (BlockingIOError, PermissionError):
        status = _read(directory / "status.json")
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(status["updated_at"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            age = float("inf")
        return {**status, "running": True,
                "healthy": bool(status.get("healthy")) and age < 15,
                "status_fresh": age < 15}
    else:
        try:
            status = _read(directory / "status.json")
            return {**status, "running": False, "healthy": False,
                    "state": status["state"] if status.get("state") in {"stopped", "failed"} else "abandoned"}
        finally:
            lease.close()


def request_host_stop(workspace: Path) -> dict:
    status = host_status(workspace)
    if not status["running"]:
        return {"requested": False, "reason": "not_running"}
    if status.get("mode") != "host":
        raise RuntimeError("当前是桌面本地模式，请从 Kaze 退出客户端")
    owner_id = status.get("owner_id")
    if not owner_id:
        raise RuntimeError("Host 正在取得工作区所有权，请稍后重试")
    _write(_directory(workspace) / "stop.json", {"owner_id": owner_id})
    return {"requested": True, "owner_id": owner_id}
