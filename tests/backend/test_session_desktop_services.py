"""End-to-end boundaries retained by the ordinary-session desktop."""
import asyncio
import io
import json
import zipfile
from datetime import datetime, timezone

import pytest_asyncio
import pytest
from PIL import Image, ImageDraw

from agent.config_models import Config
from agent.provider import LLMResponse
from agent.scheduler import ScheduledJob
from bootstrap.tools import build_core_runtime
from core.net.http import SharedHttpResources
from core.pets.packages import PetPackageService
from desktop_bridge.server import DesktopBridgeServer
from desktop_bridge.session_service import DesktopBridgeService


@pytest_asyncio.fixture
async def desktop(tmp_path):
    http = SharedHttpResources()
    runtime = build_core_runtime(Config(provider="openai", model="fake", api_key="fake"), tmp_path, http)
    bridge = DesktopBridgeService(runtime)
    yield runtime, bridge
    await bridge.aclose()
    await runtime.stop()
    await runtime.memory_runtime.aclose()
    runtime.session_manager._store.close()
    await http.aclose()


@pytest.mark.asyncio
async def test_draft_navigation_never_persists_and_first_send_titles_session(desktop):
    runtime, bridge = desktop
    events = []
    bridge.add_event_listener(events.append)
    for _ in range(3):
        result = await bridge.handle({"method": "session.draft", "payload": {"session_key": "desktop:draft"}})
        assert result.error is None
    assert runtime.session_manager.list_sessions() == []
    assert bridge.active_session_key == "desktop:draft"
    assert events[-1]["method"] == "session.selected"
    async def chat(**kwargs):
        return LLMResponse(content="已收到")
    runtime.provider.chat = chat
    sent = await bridge.handle({"method": "chat.send", "payload": {"session_key": "desktop:draft", "content": "第一条消息\n后续内容"}})
    assert sent.error is None
    assert sent.payload["session"]["title"] == "第一条消息"
    listed = await bridge.handle({"method": "sessions.list"})
    assert len(listed.payload["sessions"]) == 1
    assert listed.payload["sessions"][0]["message_count"] == 2
    await bridge.handle({"method": "session.draft", "payload": {"session_key": "desktop:next"}})
    assert len(runtime.session_manager.list_sessions()) == 1
    opened = await bridge.handle({"method": "session.get", "payload": {"session_key": "desktop:draft"}})
    assert len(opened.payload["messages"]) == 2
    assert bridge.active_session_key == "desktop:draft"


@pytest.mark.asyncio
async def test_workbench_filters_and_document_allowlist(desktop):
    runtime, bridge = desktop
    for key, text in [("desktop:a", "苹果"), ("desktop:b", "香蕉")]:
        session = runtime.session_manager.get_or_create(key)
        session.add_message("user", text)
        await runtime.session_manager.save_async(session)
    result = await bridge.handle({"method": "messages.list", "payload": {"session_key": "desktop:b", "q": "香蕉", "role": "user"}})
    assert result.error is None
    assert result.payload["total"] == 1
    assert result.payload["messages"][0]["content"] == "香蕉"
    empty = await bridge.handle({"method": "messages.list", "payload": {"session_key": "desktop:a", "q": "香蕉"}})
    assert empty.payload["total"] == 0
    catalog = await bridge.handle({"method": "runtime.catalog"})
    assert catalog.error is None
    assert len(catalog.payload["documents"]) == 3
    for doc in catalog.payload["documents"]:
        result = await bridge.handle({"method": "document.get", "payload": {"id": doc["id"]}})
        assert result.error is None
        assert result.payload["path"].startswith("memory/")
    for bad in ["../config.toml", "E:/config.toml", "VEDA.md"]:
        result = await bridge.handle({"method": "document.get", "payload": {"id": bad}})
        assert result.error is not None


@pytest.mark.asyncio
async def test_provider_failure_is_an_rpc_error_and_retry_does_not_duplicate_messages(desktop):
    runtime, bridge = desktop
    events = []
    bridge.add_event_listener(events.append)
    class AuthenticationFailure(Exception):
        status_code = 401

    async def fail(**kwargs):
        raise AuthenticationFailure("provider response with secret-test-value")

    async def succeed(**kwargs):
        return LLMResponse(content="成功回复")

    request = {"method": "chat.send", "payload": {"session_key": "desktop:retry", "content": "请保留我的消息"}}
    runtime.provider.chat = fail
    response = await bridge.handle(request)
    assert response.error is not None
    assert "认证失败" in response.error.message
    assert "secret-test-value" not in response.error.message
    assert not any(event["method"] == "chat.done" for event in events)
    assert runtime.session_manager.get_or_create("desktop:retry").messages == []
    assert not bridge._requests and not bridge._chat_tasks

    runtime.provider.chat = succeed
    response = await bridge.handle(request)
    assert response.error is None
    assert [m["content"] for m in response.payload["session"]["messages"]] == ["请保留我的消息", "成功回复"]

    # Failure in an existing session must retain committed history, too.
    events.clear()
    runtime.provider.chat = fail
    request["payload"]["content"] = "第二条消息"
    response = await bridge.handle(request)
    assert response.error is not None
    assert len(runtime.session_manager.get_or_create("desktop:retry").messages) == 2
    assert not any(event["method"] == "chat.done" for event in events)
    runtime.provider.chat = succeed
    response = await bridge.handle(request)
    assert response.error is None
    assert len(response.payload["session"]["messages"]) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", ["sk-...", "${UNCONFIGURED_API_KEY}"])
async def test_example_credentials_fail_before_contacting_provider(desktop, credential):
    runtime, bridge = desktop
    runtime.config.api_key = credential
    called = False
    async def chat(**kwargs):
        nonlocal called
        called = True
        return LLMResponse(content="不应调用")
    runtime.provider.chat = chat
    result = await bridge.handle({"method": "chat.send", "payload": {"session_key": "desktop:missing", "content": "你好"}})
    assert result.error is not None
    assert "尚未配置" in result.error.message
    assert not called
    assert runtime.session_manager.list_sessions() == []


@pytest.mark.asyncio
async def test_other_channels_keep_their_error_reply(desktop):
    runtime, _ = desktop
    async def fail(**kwargs):
        raise RuntimeError("provider unavailable")
    runtime.provider.chat = fail
    assert await runtime.loop.process_direct("你好", session_key="cli:failure") == "处理消息时出错，请稍后再试。"


def test_config_template_does_not_register_an_unrequested_vision_model():
    import tomllib
    from pathlib import Path
    template = Path(__file__).resolve().parents[2] / "config" / "examples" / "config.example.toml"
    registrations = tomllib.loads(template.read_text(encoding="utf-8"))["llm"]["registrations"]
    assert [item["model"] for item in registrations] == ["deepseek-v4-flash"]


@pytest.mark.asyncio
async def test_cancel_during_chat_does_not_block_other_requests(desktop):
    runtime, bridge = desktop
    started = asyncio.Event()
    async def chat(**kwargs):
        started.set()
        await asyncio.Event().wait()
    runtime.provider.chat = chat
    task = asyncio.create_task(bridge.handle({"method": "chat.send", "payload": {"chat_id": "plain", "content": "等待"}}))
    await asyncio.wait_for(started.wait(), 2)
    assert (await bridge.handle({"method": "health"})).payload["ok"]
    response = await bridge.handle({"method": "chat.cancel", "payload": {"session_key": "plain"}})
    assert response.payload["cancelled"]
    assert (await asyncio.wait_for(task, 2)).payload["cancelled"]
    assert not bridge._chat_tasks


@pytest.mark.asyncio
async def test_scheduled_push_persists_once_and_tasks_are_json_serializable(desktop):
    runtime, bridge = desktop
    events = []
    bridge.add_event_listener(events.append)
    job = ScheduledJob(trigger="at", tier="instant", fire_at=datetime.now(timezone.utc),
                       channel="desktop", chat_id="plain", session_key="plain", message="提醒")
    runtime.scheduler.add_job(job)
    await runtime.scheduler._execute(job)
    snapshot = runtime.session_manager.get_or_create("plain")
    assert [message["content"] for message in snapshot.messages] == ["提醒"]
    assert events[-1]["method"] == "message.pushed"
    result = await bridge.handle({"method": "tasks.list", "payload": {"session_key": "plain"}})
    assert result.error is None
    assert json.loads(json.dumps(result.to_dict()))["payload"]["tasks"][0]["session_key"] == "plain"


@pytest.mark.asyncio
async def test_json_lines_server_accepts_plain_sessions_and_utf8(desktop):
    runtime, _ = desktop
    server = DesktopBridgeServer(runtime)
    async def chat(**kwargs):
        return LLMResponse(content="你好，普通会话")
    runtime.provider.chat = chat
    requests = iter([
        json.dumps({"id": "1", "method": "chat.send", "payload": {"session_key": "测试", "content": "你好"}}),
        None,
    ])
    frames = []
    completed = asyncio.Event()
    async def read():
        request = next(requests)
        if request is None:
            await asyncio.wait_for(completed.wait(), 3)
        return request
    async def write(frame):
        json.dumps(frame, ensure_ascii=False)
        frames.append(frame)
        if frame["type"] == "response":
            completed.set()
    await server.serve_streams(read_line=read, write_payload=write)
    response = next(f for f in frames if f["type"] == "response")
    assert response["error"] is None
    assert response["payload"]["reply"] == "你好，普通会话"
    assert response["payload"]["session_key"] == "测试"


def test_pet_import_is_global_and_validates_actual_atlas(tmp_path):
    sprite = Image.new("RGBA", (1536, 1872))
    draw = ImageDraw.Draw(sprite)
    from core.pets.packages import _USED_CELLS, _CELL_SIZE
    for row, count in enumerate(_USED_CELLS):
        for col in range(count):
            x, y = col * _CELL_SIZE[0], row * _CELL_SIZE[1]
            draw.rectangle((x + 2, y + 2, x + 8, y + 8), fill="red")
    data = io.BytesIO()
    sprite.save(data, "WEBP", lossless=True)
    archive = tmp_path / "pet.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("pet.json", json.dumps({"id": "pet", "displayName": "桌宠", "description": "fixture", "spritesheetPath": "sprite.webp", "actions": {"hello": "waving"}}))
        output.writestr("sprite.webp", data.getvalue())
    service = PetPackageService(tmp_path / "workspace")
    result = service.import_package(archive)
    assert result["selected_package_id"] == "pet"
    assert (service.root / "pet" / "spritesheet.webp").exists()
    assert not (tmp_path / "workspace" / "roles").exists()
    with pytest.raises(ValueError, match="已存在"):
        service.import_package(archive)
    with zipfile.ZipFile(tmp_path / "unsafe.zip", "w") as output:
        output.writestr("../pet.json", "{}")
    with pytest.raises(ValueError):
        service.import_package(tmp_path / "unsafe.zip")

