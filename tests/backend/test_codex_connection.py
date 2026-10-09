"""验证 Codex 登录隔离、取消提交及 Agent 工具调用边界，不访问外网。"""
import asyncio
import base64
import hashlib
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
import pytest_asyncio

from agent.config_models import Config, ModelRegistration
from agent.model_runtime.auth.codex import CODEX_CLIENT_ID, CODEX_TOKEN_URL, CodexAuthDriver
from agent.model_runtime.auth.store import Credential, workspace_store
from agent.model_runtime.errors import AuthenticationError, RetryableTransportError
from agent.model_runtime.provider import CodexProvider
from agent.model_runtime.transports.responses import CodexResponsesTransport, _responses_input
from agent.model_runtime.types import ModelCapabilities, ModelRequest
from bootstrap.providers import build_providers
from desktop_bridge.codex_service import CodexConnectionService

CONNECTION = "b0a4e185-d8dd-4908-8a80-08e26345f55e"
SECOND_CONNECTION = "8db3683d-bb57-4e1b-acbc-e788a5735fa2"


def credential(token="synthetic-access", refresh="synthetic-refresh"):
    return Credential("codex", token, refresh, "synthetic-account",
                      (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())


def id_token(nonce):
    body = base64.urlsafe_b64encode(json.dumps({"nonce": nonce, "https://api.openai.com/auth": {
        "chatgpt_account_id": "account-test"}}).encode()).decode().rstrip("=")
    return f"x.{body}.y"


@pytest_asyncio.fixture
async def service(tmp_path, monkeypatch):
    # 真实 TCP 回调使用临时端口，token 交换仍由各测试模拟。
    monkeypatch.setattr("desktop_bridge.codex_oauth.CODEX_CALLBACK_PORT", 0)
    instance = CodexConnectionService(tmp_path)
    try:
        yield instance
    finally:
        await instance.aclose()


def callback_url(state):
    redirect = parse_qs(urlsplit(state["authorization_url"]).query)["redirect_uri"][0]
    parsed = urlsplit(redirect)
    return f"http://127.0.0.1:{parsed.port}{parsed.path}"


async def callback(login_state, **params):
    expected_state = parse_qs(urlsplit(login_state["authorization_url"]).query)["state"][0]
    query = urlencode({"state": expected_state, "code": "synthetic-code", **params}, doseq=True)
    async with httpx.AsyncClient(trust_env=False) as client:
        return await client.get(f"{callback_url(login_state)}?{query}")


def test_store_roundtrip_isolated_and_encrypted_on_windows(tmp_path):
    store = workspace_store(tmp_path)
    assert store.metadata() == {}
    assert not store.path.exists()
    expected = credential()
    store.put(CONNECTION, expected)
    assert store.get(CONNECTION) == expected
    assert store.get(CONNECTION).refresh_token == "synthetic-refresh"
    assert store.metadata() == {CONNECTION: {"driver": "codex"}}
    if os.name == "nt":
        assert b"synthetic-access" not in store.path.read_bytes()
        assert b"synthetic-refresh" not in store.path.read_bytes()
    other = workspace_store(tmp_path / "other-workspace")
    with pytest.raises(AuthenticationError, match="尚未登录"):
        other.get(CONNECTION)
    store.put("second", credential("second-access"))
    assert store.get(CONNECTION).access_token == "synthetic-access"
    assert store.get("second").access_token == "second-access"


def test_browser_login_uses_fresh_pkce_and_keeps_verifier_private(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.model_runtime.auth.codex.httpx.post", lambda *a, **k: pytest.fail("login start must not send an API request"))
    auth = CodexAuthDriver(workspace_store(tmp_path), CONNECTION)
    login = auth.begin_browser_login("http://localhost:1455/auth/callback")
    another = auth.begin_browser_login(login.redirect_uri)
    url = urlsplit(login.authorization_url)
    params = parse_qs(url.query)
    assert url.scheme == "https" and url.netloc == "auth.openai.com" and url.path == "/oauth/authorize"
    assert params["client_id"] == [CODEX_CLIENT_ID]
    assert params["response_type"] == ["code"]
    assert params["redirect_uri"] == [login.redirect_uri]
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"] == [base64.urlsafe_b64encode(hashlib.sha256(login.code_verifier.encode()).digest()).decode().rstrip("=")]
    assert login.state != another.state and login.nonce != another.nonce
    assert login.code_verifier != another.code_verifier
    assert login.code_verifier not in login.authorization_url
    assert login.code_verifier not in repr(login)


def test_browser_exchange_does_not_persist_until_owner_commits(tmp_path, monkeypatch):
    calls = []
    auth = CodexAuthDriver(workspace_store(tmp_path), CONNECTION)
    login = auth.begin_browser_login("http://localhost:1455/auth/callback")

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={"access_token": "access-test", "refresh_token": "refresh-test", "id_token": id_token(login.nonce)})

    monkeypatch.setattr("agent.model_runtime.auth.codex.httpx.post", post)
    result = auth.complete_browser_login(login, "synthetic-code")
    assert result.account_id == "account-test"
    assert auth.store.metadata() == {}
    assert calls == [(CODEX_TOKEN_URL, {"data": {
        "grant_type": "authorization_code", "code": "synthetic-code",
        "redirect_uri": login.redirect_uri, "client_id": CODEX_CLIENT_ID,
        "code_verifier": login.code_verifier,
    }, "timeout": 20})]


@pytest.mark.parametrize("nonce", [None, "wrong-attempt"])
def test_browser_exchange_rejects_unrelated_identity(tmp_path, monkeypatch, nonce):
    auth = CodexAuthDriver(workspace_store(tmp_path), CONNECTION)
    login = auth.begin_browser_login("http://localhost:1455/auth/callback")
    monkeypatch.setattr("agent.model_runtime.auth.codex.httpx.post", lambda *a, **k: httpx.Response(200, json={
        "access_token": "synthetic-access", "refresh_token": "synthetic-refresh", "id_token": id_token(nonce),
    }))
    with pytest.raises(AuthenticationError, match="身份校验失败"):
        auth.complete_browser_login(login, "synthetic-code")
    assert auth.store.metadata() == {}


def test_refresh_uses_newer_rotation_without_duplicate_exchange(tmp_path, monkeypatch):
    store = workspace_store(tmp_path)
    store.put(CONNECTION, credential("rotated-access", "rotated-refresh"))
    monkeypatch.setattr("agent.model_runtime.auth.codex.httpx.post", lambda *a, **k: pytest.fail("duplicate refresh"))
    result = CodexAuthDriver(store, CONNECTION).refresh(expected_access_token="old-access")
    assert result.refresh_token == "rotated-refresh"


@pytest.mark.asyncio
async def test_login_rpc_returns_no_tokens_and_persists_success(service, monkeypatch):
    monkeypatch.setattr(CodexAuthDriver, "complete_browser_login", lambda *_: credential())
    payload = {"connection_id": CONNECTION}
    state = await service.handle("codex.login.start", payload)
    assert state["status"] == "waiting"
    assert "code_verifier" not in json.dumps(state)
    assert "user_code" not in state
    response = await callback(state)
    assert response.status_code == 200
    assert "synthetic-code" not in response.text
    assert response.headers["cache-control"] == "no-store"
    await service.tasks[CONNECTION]
    state = await service.handle("codex.login.status", {**payload, "login_id": state["login_id"]})
    assert state["status"] == "connected"
    assert "synthetic" not in json.dumps(state)
    assert await service.handle("codex.status", payload) == {"configured": True}


@pytest.mark.asyncio
async def test_cancelled_login_does_not_commit_inflight_exchange(service, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def exchange(*_):
        entered.set()
        release.wait(5)
        return credential()
    monkeypatch.setattr(CodexAuthDriver, "complete_browser_login", exchange)
    payload = {"connection_id": CONNECTION}
    state = await service.handle("codex.login.start", payload)
    try:
        assert (await callback(state)).status_code == 200
        assert await asyncio.to_thread(entered.wait, 2)
        result = await service.handle("codex.login.cancel", {**payload, "login_id": state["login_id"]})
        assert result["status"] == "cancelled"
    finally:
        release.set()
        await service.aclose()
    assert service.store.metadata() == {}


@pytest.mark.asyncio
async def test_failed_login_remains_visible_without_secret_details(service, monkeypatch):
    def fail(*_):
        raise RuntimeError("request with synthetic-secret-token")
    monkeypatch.setattr(CodexAuthDriver, "complete_browser_login", fail)
    state = await service.handle("codex.login.start", {"connection_id": CONNECTION})
    assert (await callback(state)).status_code == 200
    await service.tasks[CONNECTION]
    result = await service.handle("codex.login.status", {"connection_id": CONNECTION, "login_id": state["login_id"]})
    assert result["status"] == "failed"
    assert "synthetic-secret" not in result["error"]
    assert service.store.metadata() == {}


@pytest.mark.asyncio
async def test_wrong_state_cannot_finish_or_abort_login(service, monkeypatch):
    exchanges = []
    monkeypatch.setattr(CodexAuthDriver, "complete_browser_login", lambda *args: exchanges.append(args) or credential())
    state = await service.handle("codex.login.start", {"connection_id": CONNECTION})
    assert (await callback(state, state="wrong-state")).status_code == 400
    assert exchanges == []
    current = await service.handle("codex.login.status", {"connection_id": CONNECTION, "login_id": state["login_id"]})
    assert current["status"] == "waiting"
    assert (await callback(state)).status_code == 200
    await service.tasks[CONNECTION]
    assert len(exchanges) == 1


@pytest.mark.asyncio
async def test_cancel_releases_listener_and_stale_callback_cannot_authorize_retry(service, monkeypatch):
    monkeypatch.setattr(CodexAuthDriver, "complete_browser_login", lambda *_: credential())
    payload = {"connection_id": CONNECTION}
    state = await service.handle("codex.login.start", payload)
    old_state = parse_qs(urlsplit(state["authorization_url"]).query)["state"][0]
    result = await service.handle("codex.login.cancel", {**payload, "login_id": state["login_id"]})
    assert result["status"] == "cancelled"
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(callback_url(state))
    retry = await service.handle("codex.login.start", payload)
    assert retry["login_id"] != state["login_id"]
    assert (await callback(retry, state=old_state)).status_code == 400
    assert service.store.metadata() == {}
    assert (await callback(retry)).status_code == 200
    await service.tasks[CONNECTION]


@pytest.mark.asyncio
async def test_login_timeout_releases_listener_without_credentials(service, monkeypatch):
    monkeypatch.setattr("desktop_bridge.codex_service._LOGIN_TIMEOUT_SECONDS", 0.01)
    state = await service.handle("codex.login.start", {"connection_id": CONNECTION})
    await service.tasks[CONNECTION]
    current = await service.handle("codex.login.status", {"connection_id": CONNECTION, "login_id": state["login_id"]})
    assert current["status"] == "failed" and "超时" in current["error"]
    assert service.store.metadata() == {}
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(callback_url(state))


@pytest.mark.asyncio
async def test_occupied_callback_port_does_not_hijack_listener(service, monkeypatch):
    blocker = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    monkeypatch.setattr("desktop_bridge.codex_oauth.CODEX_CALLBACK_PORT", blocker.sockets[0].getsockname()[1])
    try:
        with pytest.raises(AuthenticationError, match="本地端口"):
            await service.handle("codex.login.start", {"connection_id": CONNECTION})
        assert service.logins == {} and service.store.metadata() == {}
        assert blocker.is_serving()
    finally:
        blocker.close()
        await blocker.wait_closed()


@pytest.mark.asyncio
async def test_parallel_logins_keep_credentials_separate(service, monkeypatch):
    monkeypatch.setattr(CodexAuthDriver, "complete_browser_login", lambda auth, login, code: credential(code))
    first = await service.handle("codex.login.start", {"connection_id": CONNECTION})
    repeated = await service.handle("codex.login.start", {"connection_id": CONNECTION})
    assert repeated == first
    second = await service.handle("codex.login.start", {"connection_id": SECOND_CONNECTION})
    assert callback_url(first) == callback_url(second)
    assert (await callback(second, code="second-token")).status_code == 200
    await service.tasks[SECOND_CONNECTION]
    assert CONNECTION not in service.store.metadata()
    assert service.store.get(SECOND_CONNECTION).access_token == "second-token"
    assert (await callback(first, code="first-token")).status_code == 200
    await service.tasks[CONNECTION]
    assert service.store.get(CONNECTION).access_token == "first-token"
    assert service.store.get(SECOND_CONNECTION).access_token == "second-token"


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"state": ["one", "two"]}, {"code": ["one", "two"]}, {"code": ""}, {"code": "code", "error": "access_denied"}])
async def test_malformed_callback_does_not_consume_login(service, monkeypatch, params):
    monkeypatch.setattr(CodexAuthDriver, "complete_browser_login", lambda *_: pytest.fail("invalid callback exchanged"))
    state = await service.handle("codex.login.start", {"connection_id": CONNECTION})
    assert (await callback(state, **params)).status_code == 400
    current = await service.handle("codex.login.status", {"connection_id": CONNECTION, "login_id": state["login_id"]})
    assert current["status"] == "waiting" and service.store.metadata() == {}


@pytest.mark.asyncio
async def test_concurrent_starts_share_one_cancellable_login(service, monkeypatch):
    monkeypatch.setattr(CodexAuthDriver, "complete_browser_login", lambda *_: pytest.fail("cancelled login exchanged"))
    payload = {"connection_id": CONNECTION}
    first, second = await asyncio.gather(
        service.handle("codex.login.start", payload),
        service.handle("codex.login.start", payload),
    )
    assert first == second
    assert len(service.callbacks._pending) == 1
    result = await service.handle("codex.login.cancel", {**payload, "login_id": first["login_id"]})
    assert result["status"] == "cancelled"
    assert service.callbacks._pending == {} and service.browser_logins == {}
    assert service.store.metadata() == {}


@pytest.mark.asyncio
async def test_browser_denial_is_visible_without_echoing_remote_error(service, monkeypatch):
    monkeypatch.setattr(CodexAuthDriver, "complete_browser_login", lambda *_: pytest.fail("denied login exchanged"))
    state = await service.handle("codex.login.start", {"connection_id": CONNECTION})
    response = await callback(state, code=[], error="access_denied", error_description="synthetic-secret")
    assert response.status_code == 400 and "synthetic-secret" not in response.text
    await service.tasks[CONNECTION]
    current = await service.handle("codex.login.status", {"connection_id": CONNECTION, "login_id": state["login_id"]})
    assert current["status"] == "failed" and "取消" in current["error"]
    assert "synthetic-secret" not in current["error"] and service.store.metadata() == {}


@pytest.mark.asyncio
async def test_duplicate_callback_exchanges_only_once(service, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    exchanges = []
    def exchange(*args):
        exchanges.append(args)
        entered.set()
        release.wait(5)
        return credential()
    monkeypatch.setattr(CodexAuthDriver, "complete_browser_login", exchange)
    state = await service.handle("codex.login.start", {"connection_id": CONNECTION})
    try:
        assert (await callback(state)).status_code == 200
        assert await asyncio.to_thread(entered.wait, 2)
        assert (await callback(state)).status_code == 400
    finally:
        release.set()
    await service.tasks[CONNECTION]
    assert len(exchanges) == 1


def test_provider_selection_and_fixed_credential_destination(tmp_path):
    registration = ModelRegistration(CONNECTION, "codex", "", "", "account-model")
    config = Config(provider="codex", model="account-model", api_key="", model_registrations=[registration])
    provider, _, _ = build_providers(config, tmp_path)
    assert isinstance(provider, CodexProvider)
    assert provider.auth.store.path == tmp_path / ".desktop" / "codex-auth.bin"
    assert not provider.auth.store.path.exists()
    with pytest.raises(ValueError, match="固定"):
        CodexProvider(workspace=tmp_path, registration=ModelRegistration(CONNECTION, "codex", "https://example.com", "", "model"))


@pytest.mark.asyncio
async def test_responses_stream_maps_text_tool_calls_and_usage_to_agent(tmp_path, monkeypatch):
    async def models(_):
        return [SimpleNamespace(slug="account-model", capabilities=ModelCapabilities(
            context_window=128000, max_output_tokens=8000, supported_reasoning_efforts=("low", "high"), default_reasoning_effort="low"))]
    monkeypatch.setattr("agent.model_runtime.provider.CodexModelCatalog.list_models", models)
    async def events():
        yield {"type": "response.output_text.delta", "delta": "正在查询"}
        yield {"type": "response.output_item.done", "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "lookup", "arguments": '{"query":"test"}'}}
        yield {"type": "response.completed", "response": {"usage": {"input_tokens": 20, "output_tokens": 5}}}
    async def send(transport, request):
        payload = transport._build_payload(request)
        assert payload["store"] is False
        assert payload["reasoning"]["effort"] == "low"
        assert payload["tools"][0]["name"] == "lookup"
        return await transport._consume_stream(events(), request)
    monkeypatch.setattr(CodexResponsesTransport, "send", send)
    deltas = []
    async def delta(value):
        deltas.append(value)
    provider = CodexProvider(workspace=tmp_path, registration=ModelRegistration(CONNECTION, "codex", "", "", "account-model"))
    result = await provider.chat([{ "role": "user", "content": "test"}], [{"type": "function", "function": {"name": "lookup"}}], "account-model", 8000, on_content_delta=delta)
    assert result.content == "正在查询"
    assert result.tool_calls[0].arguments == {"query": "test"}
    assert result.total_tokens == 25
    assert deltas == [{"content_delta": "正在查询"}]


@pytest.mark.asyncio
async def test_incomplete_stream_is_an_error_not_empty_success(tmp_path):
    transport = CodexResponsesTransport(CodexAuthDriver(workspace_store(tmp_path), CONNECTION), runtime_id=CONNECTION)
    async def events():
        yield {"type": "response.output_text.delta", "delta": "partial"}
    with pytest.raises(RetryableTransportError, match="断流"):
        await transport._consume_stream(events(), ModelRequest([], [], "model", 100))


def test_tool_results_replay_in_responses_format():
    rows, instructions = _responses_input([
        {"role": "system", "content": "system"},
        {"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "lookup", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
    ], "")
    assert instructions == "system"
    assert rows == [{"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{}"},
                    {"type": "function_call_output", "call_id": "call_1", "output": "result"}]
