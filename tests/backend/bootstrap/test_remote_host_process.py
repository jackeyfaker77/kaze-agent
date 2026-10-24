"""Exercise the production CLI remote listener across a full process restart."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from websockets.sync.client import connect

from bootstrap.host_state import request_host_stop

ROOT = Path(__file__).resolve().parents[3]


def test_remote_cli_preserves_one_workspace_across_process_restart(tmp_path):
    token = "production-entrypoint-test-" + "x" * 32
    token_file = tmp_path / "token"
    token_file.write_text(token)
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
''')
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    args = [sys.executable, str(ROOT / "apps/backend/main.py"), "host", "--workspace", str(tmp_path),
            "--config", str(config), "--remote-port", str(port), "--remote-token-file", str(token_file)]
    health = []
    for run in range(2):
        log = tmp_path / f"host-{run}.log"
        with log.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(args, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        client = None
        try:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                assert process.poll() is None, log.read_text(encoding="utf-8")[-4000:]
                try:
                    client = connect(f"ws://127.0.0.1:{port}", proxy=None, open_timeout=1)
                    break
                except OSError:
                    time.sleep(.05)
            assert client is not None
            client.send(json.dumps({"type": "auth", "token": token}))
            assert json.loads(client.recv(timeout=5))["type"] == "auth.ok"
            def rpc(method, payload=None):
                client.send(json.dumps({"id": method, "method": method, "payload": payload or {}}))
                while True:
                    value = json.loads(client.recv(timeout=5))
                    if value.get("type") == "response":
                        assert value["error"] is None
                        return value["payload"]
            health.append(rpc("health"))
            if run == 0:
                rpc("session.create", {"session_key": "desktop:shared", "title": "remote persistent session"})
            else:
                assert rpc("session.get", {"session_key": "desktop:shared"})["title"] == "remote persistent session"
                assert len(rpc("sessions.list")["sessions"]) == 1
            assert request_host_stop(tmp_path)["requested"]
            assert process.wait(timeout=15) == 0  # Still-connected desktop cannot block service stop.
        finally:
            if client is not None:
                client.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    assert health[0]["root_id"] == health[1]["root_id"]
    assert health[0]["epoch"] != health[1]["epoch"]
