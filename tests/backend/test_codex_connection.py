"""验证 Codex 登录隔离、取消提交及 Agent 工具调用边界，不访问外网。"""
import asyncio
import base64
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from agent.config_models import Config, ModelRegistration
from agent.model_runtime.auth.codex import CodexAuthDriver, DeviceCode
from agent.model_runtime.auth.store import Credential, workspace_store
from agent.model_runtime.errors import AuthenticationError, RetryableTransportError
from agent.model_runtime.provider import CodexProvider
from agent.model_runtime.transports.responses import CodexResponsesTransport, _responses_input
from agent.model_runtime.types import ModelCapabilities, ModelRequest
from bootstrap.providers import build_providers
from desktop_bridge.codex_service import CodexConnectionService

CONNECTION = "b0a4e185-d8dd-4908-8a80-08e26345f55e"


def credential(token="synthetic-access", refresh="synthetic-refresh"):
    return Credential("codex", token, refresh, "synthetic-account",
                      (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())


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


def test_device_exchange_does_not_persist_until_owner_commits(tmp_path, monkeypatch):
    jwt_body = base64.urlsafe_b64encode(json.dumps({"https://api.openai.com/auth": {
        "chatgpt_account_id": "account-test"}}).encode()).decode().rstrip("=")
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("usercode"):
            return httpx.Response(200, json={"user_code": "ABCD-EFGH", "device_auth_id": "device", "interval": 3})
        if url.endswith("deviceauth/token"):
            return httpx.Response(200, json={"authorization_code": "code", "code_verifier": "verifier"})
        return httpx.Response(200, json={"access_token": "access-test", "refresh_token": "refresh-test", "id_token": f"x.{jwt_body}.y"})

    monkeypatch.setattr("agent.model_runtime.auth.codex.httpx.post", post)
    auth = CodexAuthDriver(workspace_store(tmp_path), CONNECTION)
    code = auth.begin_device_login()
    assert code.verification_uri == "https://auth.openai.com/codex/device"
    result = auth.poll_device_login(code)
    assert result.account_id == "account-test"
    assert auth.store.metadata() == {}
    assert calls[-1][1]["data"]["grant_type"] == "authorization_code"


def test_refresh_uses_newer_rotation_without_duplicate_exchange(tmp_path, monkeypatch):
    store = workspace_store(tmp_path)
    store.put(CONNECTION, credential("rotated-access", "rotated-refresh"))
    monkeypatch.setattr("agent.model_runtime.auth.codex.httpx.post", lambda *a, **k: pytest.fail("duplicate refresh"))
    result = CodexAuthDriver(store, CONNECTION).refresh(expected_access_token="old-access")
    assert result.refresh_token == "rotated-refresh"


@pytest.mark.asyncio
async def test_login_rpc_returns_no_tokens_and_persists_success(tmp_path, monkeypatch):
    service = CodexConnectionService(tmp_path)
    monkeypatch.setattr(CodexAuthDriver, "begin_device_login", lambda _: DeviceCode("CODE", "device", "https://auth.openai.com/codex/device", 0))
    monkeypatch.setattr(CodexAuthDriver, "poll_device_login", lambda *_: credential())
    payload = {"connection_id": CONNECTION}
    state = await service.handle("codex.login.start", payload)
    await service.tasks[CONNECTION]
    state = await service.handle("codex.login.status", {**payload, "login_id": state["login_id"]})
    assert state["status"] == "connected"
    assert "synthetic" not in json.dumps(state)
    assert await service.handle("codex.status", payload) == {"configured": True}
    await service.aclose()


@pytest.mark.asyncio
async def test_cancelled_login_does_not_commit_inflight_exchange(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def poll(*_):
        entered.set()
        release.wait(5)
        return credential()
    monkeypatch.setattr(CodexAuthDriver, "begin_device_login", lambda _: DeviceCode("CODE", "device", "https://auth.openai.com/codex/device", 0))
    monkeypatch.setattr(CodexAuthDriver, "poll_device_login", poll)
    service = CodexConnectionService(tmp_path)
    payload = {"connection_id": CONNECTION}
    state = await service.handle("codex.login.start", payload)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        result = await service.handle("codex.login.cancel", {**payload, "login_id": state["login_id"]})
        assert result["status"] == "cancelled"
    finally:
        release.set()
        await service.aclose()
    assert service.store.metadata() == {}


@pytest.mark.asyncio
async def test_failed_login_remains_visible_without_secret_details(tmp_path, monkeypatch):
    monkeypatch.setattr(CodexAuthDriver, "begin_device_login", lambda _: DeviceCode("CODE", "device", "url", 0))
    def fail(*_):
        raise RuntimeError("request with synthetic-secret-token")
    monkeypatch.setattr(CodexAuthDriver, "poll_device_login", fail)
    service = CodexConnectionService(tmp_path)
    state = await service.handle("codex.login.start", {"connection_id": CONNECTION})
    await service.tasks[CONNECTION]
    result = await service.handle("codex.login.status", {"connection_id": CONNECTION, "login_id": state["login_id"]})
    assert result["status"] == "failed"
    assert "synthetic-secret" not in result["error"]
    assert service.store.metadata() == {}
    await service.aclose()


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
