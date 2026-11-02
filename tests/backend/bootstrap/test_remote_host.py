import asyncio
import json
import sqlite3
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx

import pytest
import pytest_asyncio
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from agent.config_models import Config
from agent.provider import LLMResponse
from bootstrap.app import AppRuntime, RuntimeFeatures
from bootstrap import tools as tool_module
from desktop_bridge import host_models
from bootstrap.remote_host import RemoteHostServer, read_remote_token
from bootstrap.runtime_host import RuntimeHost
from bootstrap.host_state import host_status
from main import main

TOKEN = "offline-test-token-" + "a" * 32


async def rpc(socket, method, payload=None, request_id="req"):
    await socket.send(json.dumps({"id": request_id, "method": method, "payload": payload or {}}))
    async with asyncio.timeout(15):
        while True:
            value = json.loads(await socket.recv())
            if value.get("type") == "response" and value["id"] == request_id:
                return value


async def authenticate(url):
    socket = await connect(url, proxy=None)
    await socket.send(json.dumps({"type": "auth", "token": TOKEN}))
    assert json.loads(await socket.recv()) == {"type": "auth.ok", "protocol": 1}
    return socket


@pytest_asyncio.fixture
async def remote(tmp_path, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []
    model_names = []
    async def model(**kwargs):
        content = str(kwargs["messages"][-1]["content"])
        calls.append(content)
        model_names.append(kwargs['model'])
        await kwargs["on_content_delta"]({"content_delta": "remote partial"})
        if "hold-input" in content:
            started.set()
            await release.wait()
        return LLMResponse(content="remote reply")
    monkeypatch.setattr(tool_module, "build_providers", lambda *a: (SimpleNamespace(chat=model), None, None))
    monkeypatch.setattr(host_models, "build_providers", lambda *a: (SimpleNamespace(chat=model), None, None))
    monkeypatch.setattr(tool_module, "_resolve_plugin_dirs", lambda *a: [])
    config_path = tmp_path / 'config.toml'
    config_path.write_text('[[llm.registrations]]\nid="00000000-0000-4000-a000-000000000001"\nprovider="openai"\nmodel="offline"\napi_key="offline"\n[agent.maintenance]\nmemory_optimizer_enabled=false\n')
    app = AppRuntime(Config.load(config_path), tmp_path,
                     features=RuntimeFeatures(False, False))
    host = RuntimeHost(app)
    servers = []
    async def attach(core):
        server = RemoteHostServer(core, TOKEN, port=0, config_path=config_path)
        servers.append(server)
        await server.run()
    task = asyncio.create_task(host.run(attach))
    try:
        async with asyncio.timeout(20):
            while not servers:
                if task.done():
                    await task
                await asyncio.sleep(.02)
            await servers[0].ready.wait()
        yield SimpleNamespace(url=f"ws://127.0.0.1:{servers[0].port}", server=servers[0], host=host,
                              task=task, started=started, release=release, calls=calls, models=model_names, app=app, workspace=tmp_path)
    finally:
        release.set()
        host.request_stop()
        await asyncio.wait_for(task, 15)


@pytest.mark.asyncio
async def test_auth_origin_and_remote_capabilities(remote):
    async with connect(remote.url, proxy=None) as socket:
        await socket.send(json.dumps({"type": "auth", "token": "incorrect"}))
        with pytest.raises(ConnectionClosed):
            await socket.recv()
        assert socket.close_code == 1008
    with pytest.raises(InvalidStatus):
        async with connect(remote.url, origin="https://untrusted.example", proxy=None):
            pass
    socket = await authenticate(remote.url)
    try:
        health = await rpc(socket, "health")
        assert health["payload"]["transport"] == "remote"
        for method, payload in [("pet.import", {"path": "private"}), ("codex.unsupported", {}),
                                ("chat.send", {"session_key": "desktop:x", "content": "x", "media": ["C:/private.png"]})]:
            result = await rpc(socket, method, payload)
            assert result["error"]["code"] == "remote_rejected"
        assert not remote.calls
    finally:
        await socket.close()


@pytest.mark.asyncio
async def test_two_desktops_share_history_selection_is_private_and_disconnect_keeps_host(remote):
    first, second = await authenticate(remote.url), await authenticate(remote.url)
    try:
        await rpc(first, "session.draft", {"session_key": "desktop:first"})
        # A selection must not make the other desktop switch sessions.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(second.recv(), .1)
        reply = await rpc(first, "chat.send", {"session_key": "desktop:shared", "content": "hello"})
        assert reply["payload"]["reply"] == "remote reply"
        snapshot = await rpc(second, "session.get", {"session_key": "desktop:shared"})
        assert [m["role"] for m in snapshot["payload"]["messages"]] == ["user", "assistant"]
        await rpc(second, "session.rename", {"session_key": "desktop:shared", "title": "shared title"})
        assert (await rpc(first, "session.get", {"session_key": "desktop:shared"}))["payload"]["title"] == "shared title"
    finally:
        await first.close()
        await second.close()
    assert not remote.task.done() and host_status(remote.workspace)["healthy"]
    third = await authenticate(remote.url)
    try:
        assert len((await rpc(third, "session.get", {"session_key": "desktop:shared"}))["payload"]["messages"]) == 2
        assert len(remote.calls) == 1
    finally:
        await third.close()


@pytest.mark.asyncio
async def test_accepted_turn_survives_disconnect_and_is_not_replayed(remote):
    first = await authenticate(remote.url)
    await first.send(json.dumps({"id": "lost", "method": "chat.send", "payload": {
        "session_key": "desktop:lost", "content": "hold-input", "execution_request_id": "stable-request"}}))
    await asyncio.wait_for(remote.started.wait(), 5)
    await first.close()
    second = await authenticate(remote.url)
    try:
        busy = await rpc(second, "chat.send", {"session_key": "desktop:lost", "content": "duplicate"})
        assert busy["error"] and len(remote.calls) == 1
        remote.release.set()
        async with asyncio.timeout(10):
            while True:
                event = json.loads(await second.recv())
                if event.get("method") == "chat.done":
                    break
        actual = await rpc(second, "session.get", {"session_key": "desktop:lost"})
        assert len(actual["payload"]["messages"]) == 2
        execution = await rpc(second, "execution.get", {"session_key": "desktop:lost", "execution_request_id": "stable-request"})
        assert execution["payload"]["status"] == "completed"
        assert len(remote.calls) == 1
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_one_desktop_can_cancel_host_owned_turn(remote):
    first, second = await authenticate(remote.url), await authenticate(remote.url)
    try:
        pending = asyncio.create_task(rpc(first, "chat.send", {"session_key": "desktop:cancel", "content": "hold-input"}))
        await asyncio.wait_for(remote.started.wait(), 5)
        assert (await rpc(second, "chat.cancel", {"session_key": "desktop:cancel"}))["payload"]["cancelled"]
        result = await pending
        assert result["payload"]["cancelled"]
        assert len(result["payload"]["session"]["messages"]) == 2
        assert result["payload"]["session"]["messages"][1]["content"] == "remote partial"
    finally:
        await first.close()
        await second.close()


def test_remote_cli_is_opt_in_and_requires_token(tmp_path):
    for args in [["bridge", "--remote-port", "9000"], ["host", "--remote-port", "9000"],
                 ["host", "--remote-port", "0", "--remote-token-file", "unused"]]:
        with pytest.raises(SystemExit) as exc:
            main(args)
        assert exc.value.code == 2
    token = tmp_path / "token"
    token.write_text("short")
    with pytest.raises(ValueError):
        read_remote_token(token)


@pytest.mark.asyncio
async def test_host_stop_closes_connected_clients_without_waiting_for_desktops(remote):
    socket = await authenticate(remote.url)
    try:
        remote.host.request_stop()
        await asyncio.wait_for(remote.task, 5)
        with pytest.raises(ConnectionClosed):
            await socket.recv()
        assert host_status(remote.workspace)["state"] == "stopped"
    finally:
        await socket.close()


@pytest.mark.asyncio
async def test_external_channel_commit_is_visible_to_remote_desktop(remote):
    socket = await authenticate(remote.url)
    try:
        await remote.app.core.loop.process_direct(content="external input", channel="telegram", chat_id="42",
                                                 session_key="telegram:42", stream_events=True)
        async with asyncio.timeout(5):
            while True:
                event = json.loads(await socket.recv())
                if event.get("method") == "message.pushed":
                    break
        assert event["payload"]["session_key"] == "telegram:42"
        snapshot = await rpc(socket, "session.get", {"session_key": "telegram:42"})
        assert [m["role"] for m in snapshot["payload"]["messages"]] == ["user", "assistant"]
        assert snapshot["payload"]["messages"][-1]["content"] == "remote reply"
    finally:
        await socket.close()


@pytest.mark.asyncio
async def test_host_shutdown_persists_inflight_input_and_partial_reply(remote):
    socket = await authenticate(remote.url)
    try:
        await socket.send(json.dumps({"id": "shutdown", "method": "chat.send", "payload": {
            "session_key": "desktop:shutdown", "content": "hold-input"}}))
        await asyncio.wait_for(remote.started.wait(), 5)
        remote.host.request_stop()
        await asyncio.wait_for(remote.task, 5)
        with sqlite3.connect(remote.workspace / "sessions.db") as db:
            rows = db.execute("SELECT role,content FROM messages WHERE session_key='desktop:shutdown' ORDER BY seq").fetchall()
        assert [row[0] for row in rows] == ["user", "assistant"]
        assert rows[0][1] == "hold-input" and rows[1][1] == "remote partial"
    finally:
        await socket.close()


@pytest.mark.asyncio
async def test_model_switch_changes_next_host_call_without_changing_owner_or_epoch(remote):
    socket = await authenticate(remote.url)
    try:
        before = (await rpc(socket, 'health'))['payload']
        owner = host_status(remote.workspace)['owner_id']
        registration = {'id': '00000000-0000-4000-a000-000000000002', 'model': 'switched',
                        'provider': 'openai', 'apiKey': 'offline', 'baseUrl': '', 'effort': 'none'}
        saved = await rpc(socket, 'models.save', {'expected_revision': before['model_revision'], 'registrations': [registration]})
        assert saved['error'] is None and saved['payload']['ok']
        after = (await rpc(socket, 'health'))['payload']
        assert after['epoch'] == before['epoch']
        assert after['model'] == 'switched'
        assert host_status(remote.workspace)['owner_id'] == owner
        reply = await rpc(socket, 'chat.send', {'session_key': 'desktop:model-switch', 'content': 'verify model'})
        assert reply['error'] is None
        assert remote.models[-1] == 'switched'
        stale = await rpc(socket, 'models.save', {'expected_revision': before['model_revision'], 'registrations': [registration]})
        assert stale['error']['code'] == 'model_settings_failed'
        assert Config.load(remote.workspace / 'config.toml').model == 'switched'
    finally:
        await socket.close()



@pytest.mark.asyncio
async def test_resident_codex_browser_login_catalog_and_model_save(remote, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from agent.model_runtime.auth.codex import CodexAuthDriver
    from agent.model_runtime.auth.store import Credential
    from agent.model_runtime.catalog.codex import CodexModel, CodexModelCatalog
    from agent.model_runtime.types import ModelCapabilities

    monkeypatch.setattr("desktop_bridge.codex_oauth.CODEX_CALLBACK_PORT", 0)
    monkeypatch.setattr(CodexAuthDriver, "complete_browser_login", lambda *_: Credential(
        "codex", "synthetic-access", "synthetic-refresh", "synthetic-account",
        (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()))

    async def catalog(self):
        assert self.auth.headers()["Authorization"] == "Bearer synthetic-access"
        return [CodexModel("fixture-codex", ModelCapabilities(32768, 4096, supported_reasoning_efforts=("low", "high")), False)]

    monkeypatch.setattr(CodexModelCatalog, "list_models", catalog)
    connection = {"connection_id": "00000000-0000-4000-a000-000000000003"}
    socket = await authenticate(remote.url)
    try:
        assert (await rpc(socket, "codex.status", connection))["payload"] == {"configured": False}
        waiting = await rpc(socket, "codex.login.start", connection)
        assert waiting["error"] is None
        state = waiting["payload"]
        assert state["status"] == "waiting"
        login = {**connection, "login_id": state["login_id"]}
        assert (await rpc(socket, "codex.login.status", login))["payload"]["status"] == "waiting"
        assert (await rpc(socket, "codex.login.cancel", login))["payload"]["status"] == "cancelled"

        state = (await rpc(socket, "codex.login.start", connection))["payload"]
        params = parse_qs(urlsplit(state["authorization_url"]).query)
        redirect = urlsplit(params["redirect_uri"][0])
        async with httpx.AsyncClient(trust_env=False) as client:
            callback = await client.get(f"http://127.0.0.1:{redirect.port}{redirect.path}",
                params={"state": params["state"][0], "code": "synthetic-code"})
        assert callback.status_code == 200
        await asyncio.wait_for(remote.server.service.codex.tasks[connection["connection_id"]], 5)
        connected = await rpc(socket, "codex.login.status", {**connection, "login_id": state["login_id"]})
        assert connected["payload"]["status"] == "connected"
        assert "synthetic" not in json.dumps(connected)
        assert (await rpc(socket, "codex.status", connection))["payload"] == {"configured": True}
        models = await rpc(socket, "codex.models", connection)
        assert models["error"] is None
        assert models["payload"]["models"] == [{"id": "fixture-codex", "efforts": ["low", "high"]}]

        before = (await rpc(socket, "health"))["payload"]
        registration = {"id": connection["connection_id"], "provider": "codex", "model": "fixture-codex",
                        "apiKey": "", "baseUrl": "", "effort": "high"}
        saved = await rpc(socket, "models.save", {"expected_revision": before["model_revision"], "registrations": [registration]})
        assert saved["error"] is None
        after = (await rpc(socket, "health"))["payload"]
        assert after["model"] == "fixture-codex" and after["epoch"] == before["epoch"]
        config = Config.load(remote.workspace / "config.toml")
        assert config.provider == "codex" and not config.api_key
    finally:
        await socket.close()
