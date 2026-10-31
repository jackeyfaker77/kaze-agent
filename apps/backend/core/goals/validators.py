"""Independent local checks over Host-selected criteria, never model assertions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import subprocess

from .contracts import digest, file_version, inside


def input_versions(state, artifact_dir: Path):
    spec = state["definition"]
    sources = {
        p: file_version(inside(Path(spec["input_dir"]), p)) for p in spec["sources"]
    }
    artifacts = set(spec["artifacts"]) | {
        c["path"] for c in spec["criteria"] if c["kind"] != "command"
    }
    outputs = {p: file_version(inside(artifact_dir, p)) for p in sorted(artifacts)}
    return {
        "sources": sources,
        "artifacts": outputs,
        "definition_version": state["definition_version"],
    }


async def check(criterion, *, input_dir: Path, artifact_dir: Path):
    kind = criterion["kind"]
    evidence = {"criterion_id": criterion["id"], "kind": kind, "passed": False}
    if kind in {"file", "json"}:
        path = inside(artifact_dir, criterion["path"])
        evidence.update(path=str(path), sha256=file_version(path))
        if not path.is_file():
            return {**evidence, "error": "artifact_missing"}
        if kind == "file":
            evidence["passed"] = (
                not criterion.get("sha256") or evidence["sha256"] == criterion["sha256"]
            )
        else:
            try:

                def invalid_constant(value):
                    raise ValueError("Non-JSON numeric constant")

                value = json.loads(
                    path.read_text(encoding="utf-8"), parse_constant=invalid_constant
                )
                required = criterion.get("required", [])
                equals = criterion.get("equals", {})
                evidence["passed"] = (
                    isinstance(value, dict)
                    and all(k in value for k in required)
                    and all(
                        k in value and digest(value[k]) == digest(v)
                        for k, v in equals.items()
                    )
                )
                evidence["format"] = "json"
            except (ValueError, UnicodeError) as exc:
                evidence["error"] = type(exc).__name__
        return evidence
    argv = [
        arg.replace("{artifact_dir}", str(artifact_dir)).replace(
            "{input_dir}", str(input_dir)
        )
        for arg in criterion["argv"]
    ]

    # Fixed argv and no shell. Commands are trusted Host configuration, not an OS sandbox.
    def run():
        return subprocess.run(
            argv,
            cwd=input_dir,
            capture_output=True,
            timeout=criterion["timeout_seconds"],
            check=False,
        )

    completed = await asyncio.to_thread(run)
    evidence.update(
        argv=argv,
        returncode=completed.returncode,
        stdout=completed.stdout[:32768].decode("utf-8", errors="replace"),
        stderr=completed.stderr[:32768].decode("utf-8", errors="replace"),
        stdout_sha256=hashlib.sha256(completed.stdout).hexdigest(),
        stderr_sha256=hashlib.sha256(completed.stderr).hexdigest(),
        passed=completed.returncode == 0,
    )
    return evidence


async def validate_goal(context, state):
    root = Path(state["definition"]["input_dir"])
    artifact_dir = context.artifact_dir
    before = input_versions(state, artifact_dir)
    evidence = []
    for criterion in state["definition"]["criteria"]:
        evidence.append(
            await context.call(
                "validation",
                "verify:" + criterion["kind"],
                lambda c=criterion: check(c, input_dir=root, artifact_dir=artifact_dir),
            )
        )
    after = input_versions(state, artifact_dir)
    return {
        "passed": all(e["passed"] for e in evidence),
        "checks": evidence,
        "input_versions": before,
        "input_digest": digest(before),
        "output_versions": after,
        "stale": before != after,
    }
