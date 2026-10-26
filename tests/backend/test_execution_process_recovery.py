"""Process-level regression coverage for completion loss and detached execution."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("scenario", ["spawn_effect", "reply_completed"])
def test_hard_exit_restarts_replay_without_effects_or_duplicate_messages(tmp_path, scenario):
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(root / "apps/backend"), "PYTHONIOENCODING": "utf-8"}
    script = root / "tests/e2e/execution_recovery_crash.py"

    def run(phase):
        return subprocess.run([sys.executable, str(script), str(tmp_path), scenario, phase],
                              cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=45)

    crashed = run("crash")
    assert crashed.returncode == 73, crashed.stdout + crashed.stderr
    reports = []
    for _ in range(2):
        recovered = run("recover")
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        reports.append(json.loads(recovered.stdout))
    assert reports[0] == reports[1]
    report = reports[0]
    assert report["calls"] == {"model": 0, "tool": 0}
    assert report["effect_count"] == 1
    messages = report["messages"]
    assert [message["content"] for message in messages if message["role"] == "user"] == ["write once"]
    assert [message["content"] for message in messages if message["role"] == "assistant" and message["content"]] == ["durable reply"]
    message_ids = [message["id"] for message in messages]
    assert all(isinstance(identity, str) and identity for identity in message_ids)
    assert len(message_ids) == len(set(message_ids))
    rows = report["executions"]
    parent, = [row for row in rows if row["source"] == "desktop"]
    assert parent["status"] == "completed"
    if scenario == "spawn_effect":
        child, = [row for row in rows if row["source"] == "subagent"]
        assert child["status"] == "needs_review" and not child["retry"]["allowed"]
        effect, = child["effects"]
        assert effect["name"] == "recovery_write" and effect["certainty"] == "unknown"
    else:
        assert len(rows) == 1
        effect, = parent["effects"]
        assert effect["certainty"] == "confirmed"
