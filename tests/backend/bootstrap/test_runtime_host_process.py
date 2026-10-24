"""Actual process supervision, headless channel I/O and restart acceptance."""
import json
import os
import signal
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from bootstrap.host_state import host_status, request_host_stop

ROOT = Path(__file__).resolve().parents[3]


def wait_for(predicate, process, log, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        assert process.poll() is None, log.read_text(encoding="utf-8", errors="replace")[-7000:]
        time.sleep(0.1)
    pytest.fail(log.read_text(encoding="utf-8", errors="replace")[-7000:])


def launch(tmp_path, scenario="run", *, production=False):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "apps/backend"), "PYTHONIOENCODING": "utf-8"}
    log = tmp_path / f"process-{time.time_ns()}.log"
    args = [str(ROOT / "tests/e2e/runtime_host_backend.py"), str(tmp_path), scenario]
    if production:
        config = tmp_path / "config.toml"
        config.write_text('''[[llm.registrations]]
id = "00000000-0000-4000-a000-000000000001"
provider = "openai"
model = "offline"
api_key = "offline"
[proactive]
enabled = false
[agent.maintenance]
memory_optimizer_enabled = false
''', encoding="utf-8")
        args = [str(ROOT / "apps/backend/main.py"), "host", "--workspace", str(tmp_path), "--config", str(config)]
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen([sys.executable, *args], cwd=ROOT, env=env,
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=output)
    return process, log


def stop(process, tmp_path):
    if process.poll() is None:
        request_host_stop(tmp_path)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
            raise


def wire(tmp_path):
    path = tmp_path / "wire.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # An in-flight append may be incomplete; poll again.
    return rows


def test_real_cli_survives_closed_stdin_and_stops_without_desktop(tmp_path):
    process, log = launch(tmp_path, production=True)
    try:
        wait_for(lambda: host_status(tmp_path).get("state") == "running", process, log)
        status = host_status(tmp_path)
        assert status["healthy"] and status["mode"] == "host"
        assert status["workers"] == {"agent_loop": True, "bus_dispatch_outbound": True, "scheduler": True, "transport_outbox": True}
        assert process.poll() is None
        stop(process, tmp_path)
        assert process.returncode == 0 and not host_status(tmp_path)["running"]
    finally:
        stop(process, tmp_path)


def test_headless_telegram_and_proactive_delivery_survive_host_restart(tmp_path):
    process, log = launch(tmp_path)
    try:
        wait_for(lambda: host_status(tmp_path).get("state") == "running", process, log)
        first = host_status(tmp_path)
        assert first["healthy"] and first["proactive"] == "running"
        (tmp_path / "inbound.json").write_text("{}")
        expected = {"host reply", "host reminder", "host schedule"}
        wait_for(lambda: expected <= {r.get("text") for r in wire(tmp_path)}, process, log)
        wait_for(lambda: any(r["kind"] == "ack" for r in wire(tmp_path)), process, log)
        # Competing desktop bridge must fail before opening channels or the store.
        collision = subprocess.run([sys.executable, str(ROOT / "apps/backend/main.py"), "bridge",
            "--workspace", str(tmp_path), "--config", str(tmp_path / "config.toml")],
            cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        assert collision.returncode == 2
        stop(process, tmp_path)
        assert process.returncode == 0
        before = wire(tmp_path)
        (tmp_path / "inbound.json").unlink()
        process, log = launch(tmp_path)
        wait_for(lambda: host_status(tmp_path).get("state") == "running", process, log)
        wait_for(lambda: sum(r["kind"] == "fetch" for r in wire(tmp_path)) > sum(r["kind"] == "fetch" for r in before), process, log)
        # Let the real polling/scheduler/proactive loops run again with no UI.
        time.sleep(2)
        second = host_status(tmp_path)
        assert first["root_id"] == second["root_id"] and first["epoch"] != second["epoch"]
        assert len([r for r in wire(tmp_path) if r["kind"] == "outbound"]) == len([r for r in before if r["kind"] == "outbound"])
        assert len([r for r in wire(tmp_path) if r["kind"] == "model"]) == len([r for r in before if r["kind"] == "model"])
        with sqlite3.connect(tmp_path / "sessions.db") as db:
            assert db.execute("SELECT count(*) FROM messages WHERE session_key='telegram:43'").fetchone()[0] == 2
            assert db.execute("SELECT count(*) FROM messages WHERE session_key='telegram:42'").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM messages WHERE session_key='telegram:44'").fetchone()[0] == 1
        stop(process, tmp_path)
        assert process.returncode == 0
    finally:
        stop(process, tmp_path)


def test_headless_crash_recovers_execution_without_replaying_input(tmp_path):
    process, log = launch(tmp_path, "crash")
    try:
        wait_for(lambda: host_status(tmp_path).get("state") == "running", process, log)
        (tmp_path / "inbound.json").write_text("{}")
        assert process.wait(timeout=30) == 73
        assert not host_status(tmp_path)["running"]
        (tmp_path / "inbound.json").unlink()
        process, log = launch(tmp_path)
        wait_for(lambda: host_status(tmp_path).get("state") == "running", process, log)
        with sqlite3.connect(tmp_path / "private_runtime/execution/events.sqlite3") as db:
            row = db.execute("SELECT turn_id,status,input FROM executions WHERE session_key='telegram:43'").fetchone()
            assert row and row[1] == "interrupted" and json.loads(row[2])["content"] == "host input"
            deltas = [json.loads(r[0]).get("content", "") for r in db.execute("SELECT payload FROM events WHERE turn_id=? AND kind='reply.delta'", (row[0],))]
            assert "host partial" in deltas
        assert sum(r["kind"] == "model" and r["passive"] for r in wire(tmp_path)) == 1
    finally:
        stop(process, tmp_path)


def test_production_desktop_bridge_still_answers_health_and_exits_on_eof(tmp_path):
    # Start and stop a host first; its stale stop request must not affect bridge.
    process, log = launch(tmp_path, production=True)
    try:
        wait_for(lambda: host_status(tmp_path).get("state") == "running", process, log)
        stop(process, tmp_path)
    finally:
        stop(process, tmp_path)
    result = subprocess.run([sys.executable, str(ROOT / "apps/backend/main.py"), "bridge",
        "--workspace", str(tmp_path), "--config", str(tmp_path / "config.toml")],
        input='{"id":"probe","method":"health","payload":{}}\n', text=True,
        encoding="utf-8", cwd=ROOT, capture_output=True, timeout=30,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert result.returncode == 0, result.stderr[-5000:]
    frames = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    health = next(frame for frame in frames if frame.get("id") == "probe")
    assert health["payload"]["ok"] and health["payload"]["execution_contract_version"] == 1
    assert not host_status(tmp_path)["running"]


@pytest.mark.parametrize('scenario,expected_state', [
    ('queue-inbox','done'), ('queue-unknown','needs_review'), ('queue-receipt','done'), ('queue-retry','done'),
])
def test_durable_telegram_sdk_and_host_recovery(tmp_path, scenario, expected_state):
    def reply_state():
        path = tmp_path/'private_runtime/transport/queue.sqlite3'
        if not path.exists():
            return None
        with sqlite3.connect(path) as db:
            row = db.execute("SELECT state FROM jobs WHERE kind='bus:telegram'").fetchone()
            return row[0] if row else None
    process, log = launch(tmp_path, scenario)
    try:
        wait_for(lambda: host_status(tmp_path).get('state') == 'running', process, log)
        (tmp_path/'inbound.json').write_text('{}')
        if scenario == 'queue-retry':
            wait_for(lambda: reply_state() == 'done', process, log)
            assert sum(r['kind']=='rejected' for r in wire(tmp_path)) == 1
            stop(process, tmp_path)
        else:
            assert process.wait(timeout=30) == 73, log.read_text(encoding='utf-8')[-4000:]
        process, log = launch(tmp_path)
        wait_for(lambda: host_status(tmp_path).get('state') == 'running', process, log)
        wait_for(lambda: reply_state() == expected_state, process, log)
        time.sleep(.5)
        rows = wire(tmp_path)
        assert sum(r['kind']=='model' and r['passive'] for r in rows) == 1
        assert sum(r['kind']=='outbound' and r['text']=='host reply' for r in rows) == 1
        with sqlite3.connect(tmp_path/'sessions.db') as db:
            assert db.execute("SELECT count(*) FROM messages WHERE session_key='telegram:43'").fetchone()[0] == 2
        with sqlite3.connect(tmp_path/'private_runtime/transport/queue.sqlite3') as db:
            assert db.execute("SELECT count(*) FROM jobs WHERE kind='inbound'").fetchone()[0] == 1
            assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    finally:
        stop(process, tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="Windows terminate is a hard exit, not POSIX SIGTERM")
def test_service_sigterm_runs_normal_shutdown(tmp_path):
    process, log = launch(tmp_path, production=True)
    try:
        wait_for(lambda: host_status(tmp_path).get("state") == "running", process, log)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=20) == 0
        assert host_status(tmp_path)["state"] == "stopped"
    finally:
        stop(process, tmp_path)
