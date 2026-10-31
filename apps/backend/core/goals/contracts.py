"""Host-owned goal definitions and bounded local acceptance contracts."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from core.execution.contracts import ExecutionConflict, ExecutionNeedsReview


class GoalConflict(ExecutionConflict):
    pass


class GoalBlocked(ExecutionNeedsReview):
    pass


KINDS = ("model", "tool", "validation", "replan")
ACTIONS = {
    "model",
    "read_file",
    "write_file",
    "verify:file",
    "verify:json",
    "verify:command",
    "replan",
}
ACTION_KINDS = {
    "model": "model",
    "read_file": "tool",
    "write_file": "tool",
    "verify:file": "validation",
    "verify:json": "validation",
    "verify:command": "validation",
    "replan": "replan",
}
TERMINAL = {"cancelled", "failed", "needs_review"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def inside(root: Path, value: str) -> Path:
    root = root.resolve()
    path = (root / value).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError(f"Path must be a file inside {root}")
    return path


def file_version(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"Not a regular file: {path}")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_definition(spec: dict) -> dict:
    # Serialization also rejects opaque values and NaN before any durable write.
    spec = json.loads(json.dumps(spec, allow_nan=False))
    if not isinstance(spec.get("objective"), str) or not spec["objective"].strip():
        raise ValueError("A nonempty objective is required")
    root = Path(spec["input_dir"]).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("input_dir must be a trusted input directory")
    spec["input_dir"] = str(root)
    spec.setdefault("sources", [])
    spec.setdefault("artifacts", [])
    for key in ("sources", "artifacts"):
        if not isinstance(spec[key], list) or any(
            not isinstance(p, str) for p in spec[key]
        ):
            raise ValueError(f"{key} must be a list of paths")
    for path in spec["sources"]:
        inside(root, path)
    criteria = spec.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("At least one executable acceptance criterion is required")
    seen = set()
    for criterion in criteria:
        key, kind = criterion.get("id"), criterion.get("kind")
        if not isinstance(key, str) or not key or key in seen:
            raise ValueError("Criterion identities must be unique")
        seen.add(key)
        if kind not in {"file", "json", "command"}:
            raise ValueError("Only file, json and command validators are supported")
        if kind in {"file", "json"}:
            if not isinstance(criterion.get("path"), str):
                raise ValueError("File validators require an artifact path")
            # Check relative syntax now, resolve again at every use.
            inside(Path.cwd() / "_goal_artifact_validation", criterion["path"])
        if kind == "json":
            required = criterion.get("required", [])
            if not isinstance(required, list) or any(
                not isinstance(k, str) for k in required
            ):
                raise ValueError("JSON required must contain field names")
            if not isinstance(criterion.get("equals", {}), dict):
                raise ValueError("JSON equals must be an object")
        if (
            kind == "file"
            and criterion.get("sha256")
            and not re.fullmatch(r"[0-9a-f]{64}", criterion["sha256"])
        ):
            raise ValueError("Expected artifact SHA256 must be lowercase hex")
        if kind == "command":
            argv = criterion.get("argv")
            if (
                not isinstance(argv, list)
                or not argv
                or any(not isinstance(a, str) or not a for a in argv)
            ):
                raise ValueError(
                    "Command validator requires a fixed argv, never a shell string"
                )
            if not Path(argv[0]).is_absolute():
                raise ValueError(
                    "Validator executable must be an absolute Host-selected path"
                )
            timeout = criterion.setdefault("timeout_seconds", 30)
            if type(timeout) is not int or not 1 <= timeout <= 120:
                raise ValueError("Validator timeout must be 1..120 seconds")
    iterations = spec.setdefault("max_iterations", 8)
    if type(iterations) is not int or not 1 <= iterations <= 20:
        raise ValueError("max_iterations must be 1..20")
    return spec


def validate_authorization(auth: dict) -> dict:
    auth = json.loads(json.dumps(auth, allow_nan=False))
    if not isinstance(auth.get("source"), str) or not auth["source"].strip():
        raise ValueError("Host authorization must identify its source")
    allowed = auth.get("allowed_actions")
    if not isinstance(allowed, list) or not set(allowed) <= ACTIONS:
        raise ValueError("Unknown authorized action")
    limits = auth.get("limits", {})
    if set(limits) != {"total", *KINDS} or any(
        type(v) is not int or v < 0 for v in limits.values()
    ):
        raise ValueError(
            "Explicit nonnegative total/model/tool/validation/replan limits required"
        )
    attempts = auth.get("max_attempts")
    if type(attempts) is not int or not 1 <= attempts <= 10:
        raise ValueError("max_attempts must be 1..10")
    if auth.get("expires_at"):
        parsed = datetime.fromisoformat(auth["expires_at"])
        if parsed.tzinfo is None:
            raise ValueError("Authorization expiry needs a timezone")
    return auth


def check_id(goal_id: str):
    if not isinstance(goal_id, str) or not re.fullmatch(
        r"[a-zA-Z0-9_-]{1,100}", goal_id
    ):
        raise ValueError(
            "Goal ID must be 1..100 ASCII letters, digits, underscore or hyphen"
        )
