"""End-to-end boundaries retained by the ordinary-session desktop."""
import asyncio
import io
import json
import sqlite3
import zipfile
from contextlib import closing
from datetime import datetime, timezone

import pytest_asyncio
import pytest
from PIL import Image, ImageDraw

from agent.config_models import Config
from agent.provider import LLMResponse, ToolCall
from agent.scheduler import ScheduledJob
from agent.tools.base import Tool
from bootstrap.tools import build_core_runtime
from core.net.http import SharedHttpResources
from core.pets.packages import PetPackageService
from desktop_bridge.server import DesktopBridgeServer
from desktop_bridge.session_service import DesktopBridgeService
from session.manager.models import INTERRUPTED_TURN_METADATA_KEY


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


@pytest.mark.asyncio
async def test_external_channel_commit_refreshes_desktop_without_switching_session(desktop):
    runtime, bridge = desktop
    events = []
    bridge.add_event_listener(events.append)
    await bridge.handle({"method": "session.draft", "payload": {"session_key": "desktop:editing"}})

    async def chat(**kwargs):
        return LLMResponse(content="external reply")

    runtime.provider.chat = chat
    await runtime.loop.process_direct("external input", channel="telegram", chat_id="42", session_key="telegram:42")
    pushed = [e for e in events if e["method"] == "message.pushed"]
    assert len(pushed) == 1
    assert pushed[0]["payload"]["session_key"] == "telegram:42"
    assert [m["content"] for m in pushed[0]["payload"]["session"]["messages"]] == ["external input", "external reply"]
    assert bridge.active_session_key == "desktop:editing"


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
        await kwargs["on_content_delta"]({"content_delta": "你好，"})
        await kwargs["on_content_delta"]({"content_delta": "普通会话"})
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
        if frame["method"] == "chat.done":
            # A separate SQLite connection must see the commit before UI completion.
            with closing(sqlite3.connect(runtime.session_manager.workspace / "sessions.db")) as db:
                assert db.execute(
                    "SELECT role, content FROM messages WHERE session_key = ? ORDER BY seq",
                    ("测试",),
                ).fetchall() == [("user", "你好"), ("assistant", "你好，普通会话")]
        frames.append(frame)
        if frame["type"] == "response":
            completed.set()
    await server.serve_streams(read_line=read, write_payload=write)
    response = next(f for f in frames if f["type"] == "response")
    assert response["error"] is None
    assert response["payload"]["reply"] == "你好，普通会话"
    assert response["payload"]["session_key"] == "测试"
    relevant = [frame for frame in frames if frame["method"] in {"chat.delta", "chat.done", "chat.send"}]
    assert [frame["method"] for frame in relevant] == ["chat.delta", "chat.delta", "chat.done", "chat.send"]
    assert all(frame["id"] == "1" for frame in relevant)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_history", [False, True])
@pytest.mark.parametrize("partial", ["", "部分回复", "thinking"])
@pytest.mark.parametrize("cancel_method", ["chat.cancel", "voice.turn.cancel"])
async def test_cancel_releases_session_and_retry_commits_once(desktop, with_history, partial, tmp_path, cancel_method):
    runtime, bridge = desktop
    key = "desktop:cancel-retry"
    history = []
    if with_history:
        session = runtime.session_manager.get_or_create(key)
        # RPC request IDs may be reused after a request has finished.
        session.add_message("user", "之前的问题", metadata={"request_id": "cancelled"})
        session.add_message("assistant", "之前的回复", metadata={"request_id": "cancelled"})
        await runtime.session_manager.save_async(session)
        history = [dict(message) for message in session.messages]
    attachment = tmp_path / "attachment.png"
    Image.new("RGB", (2, 2)).save(attachment)
    entered = asyncio.Event()
    events = []
    bridge.add_event_listener(events.append)
    async def wait_for_cancel(**kwargs):
        if partial:
            await kwargs["on_content_delta"]({"thinking_delta": "尚未完成的思考"})
        if partial and partial != "thinking":
            await kwargs["on_content_delta"]({"content_delta": "部分"})
            await kwargs["on_content_delta"]({"content_delta": "回复"})
        entered.set()
        await asyncio.Event().wait()
    runtime.provider.chat = wait_for_cancel
    request = {"id": "cancelled", "method": "chat.send", "payload": {"session_key": key, "content": "请回复", "media": [str(attachment)]}}
    request["payload"]["voice_turn_id"] = "cancel-test-voice"
    pending = asyncio.create_task(bridge.handle(request))
    await asyncio.wait_for(entered.wait(), 2)
    cancelled = await bridge.handle({"method": cancel_method, "payload": {"session_key": key, "voice_turn_id": "cancel-test-voice"}})
    assert cancelled.payload["cancelled"] is True
    result = await asyncio.wait_for(pending, 2)
    assert result.error is None
    assert result.payload["cancelled"] is True
    assert not bridge._requests and not bridge._chat_tasks
    assert not runtime.loop._active_tasks and not runtime.loop._active_turn_states
    assert not runtime.loop._interrupt_states
    assert not any(event["method"] == "chat.done" for event in events)
    expected = [(message["role"], message["content"]) for message in history]
    partial_reply = "" if partial == "thinking" else partial
    expected += [("user", "请回复")]
    if partial:
        expected += [("assistant", partial_reply)]
    messages = result.payload["session"]["messages"]
    assert [(m["role"], m["content"]) for m in messages] == expected
    assert messages[:len(history)] == history
    with closing(sqlite3.connect(runtime.session_manager.workspace / "sessions.db")) as db:
        assert db.execute("SELECT role, content FROM messages WHERE session_key = ? ORDER BY seq", (key,)).fetchall() == expected

    # Reopening must use durable records, including reasoning, attachments and interruption context.
    runtime.session_manager.invalidate(key)
    reopened = await bridge.handle({"method": "session.get", "payload": {"session_key": key}})
    for stored, original in zip(reopened.payload["messages"], messages, strict=True):
        # SQLite omits the optional tool_chain field when it is NULL.
        assert {**stored, "tool_chain": stored.get("tool_chain")} == {**original, "tool_chain": original.get("tool_chain")}
    assert messages[len(history)]["media"] == [str(attachment)]
    if partial:
        assert messages[-2]["media"] == [str(attachment)]
        assert messages[-1]["reasoning_content"] == "尚未完成的思考"
        assert messages[-1]["metadata"]["interrupted_reply"] is True
        assert INTERRUPTED_TURN_METADATA_KEY in reopened.payload["metadata"]

    async def succeed(**kwargs):
        if partial:
            assert sum(message.get("content") == partial_reply and message["role"] == "assistant" for message in kwargs["messages"]) == 1
            assert "上一轮助手回复因用户主动中断而未完成" in json.dumps(kwargs["messages"], ensure_ascii=False)
        return LLMResponse(content="重试成功")
    runtime.provider.chat = succeed
    request["id"] = "retry"
    request["payload"]["content"] = "请继续"
    result = await asyncio.wait_for(bridge.handle(request), 2)
    assert result.error is None
    assert INTERRUPTED_TURN_METADATA_KEY not in result.payload["session"]["metadata"]
    done = [event for event in events if event["method"] == "chat.done"]
    assert len(done) == 1 and done[0]["id"] == "retry"
    expected += [("user", "请继续"), ("assistant", "重试成功")]
    with closing(sqlite3.connect(runtime.session_manager.workspace / "sessions.db")) as db:
        assert db.execute("SELECT role, content FROM messages WHERE session_key = ? ORDER BY seq", (key,)).fetchall() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_reply", ["", "查询后的部分回复"])
async def test_cancel_preserves_completed_tool_results_for_next_turn(desktop, partial_reply):
    runtime, bridge = desktop
    key = "desktop:cancel-tools"
    entered = asyncio.Event()
    calls = 0

    class LookupTool(Tool):
        name = "lookup_for_cancel_test"
        description = "Look up a fixture value"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return "查询结果"

    runtime.tools.register(LookupTool(), always_on=True)

    async def tool_then_wait(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return LLMResponse(content=None, tool_calls=[ToolCall(
                id="lookup-1", name=LookupTool.name, arguments={"description": "查询测试数据"},
            )])
        if partial_reply:
            await kwargs["on_content_delta"]({"content_delta": partial_reply})
        entered.set()
        await asyncio.Event().wait()

    runtime.provider.chat = tool_then_wait
    pending = asyncio.create_task(bridge.handle({"id": "tools", "method": "chat.send", "payload": {"session_key": key, "content": "查一下"}}))
    await asyncio.wait_for(entered.wait(), 2)
    await bridge.handle({"method": "chat.cancel", "payload": {"session_key": key}})
    result = await asyncio.wait_for(pending, 2)
    assert result.error is None and result.payload["cancelled"] is True
    runtime.session_manager.invalidate(key)
    saved = runtime.session_manager.get_or_create(key)
    assert [(m["role"], m["content"]) for m in saved.messages] == [("user", "查一下"), ("assistant", partial_reply)]
    assistant = saved.messages[-1]
    assert assistant["tools_used"] == [LookupTool.name]
    call = assistant["tool_chain"][0]["calls"][0]
    assert (call["call_id"], call["name"], call["result"]) == ("lookup-1", LookupTool.name, "查询结果")

    async def follow_up(**kwargs):
        tool_messages = [message for message in kwargs["messages"] if message["role"] == "tool"]
        assert tool_messages == [{"role": "tool", "tool_call_id": "lookup-1", "content": "查询结果"}]
        return LLMResponse(content="继续完成")

    runtime.provider.chat = follow_up
    result = await bridge.handle({"id": "follow-up", "method": "chat.send", "payload": {"session_key": key, "content": "继续"}})
    assert result.error is None
    assert len(result.payload["session"]["messages"]) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", ["", "保存中的部分回复"])
@pytest.mark.parametrize("cancel_method", ["chat.cancel", "voice.turn.cancel"])
async def test_repeated_cancel_during_snapshot_write_preserves_reply_once(desktop, monkeypatch, partial, cancel_method):
    runtime, bridge = desktop
    key = "desktop:cancel-write"
    streamed = asyncio.Event()
    writing = asyncio.Event()
    pushed = asyncio.Event()
    append_messages = runtime.session_manager.append_messages

    async def record_write(session, messages):
        writing.set()
        await append_messages(session, messages)

    async def stream_then_wait(**kwargs):
        if partial:
            await kwargs["on_content_delta"]({"content_delta": partial})
        streamed.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime.session_manager, "append_messages", record_write)
    runtime.provider.chat = stream_then_wait
    request = {"id": "cancel-write", "method": "chat.send", "payload": {"session_key": key, "content": "请回复", "voice_turn_id": "write-voice"}}
    cancel = {"method": cancel_method, "payload": {"session_key": key, "voice_turn_id": "write-voice"}}
    async with runtime.session_manager._lock(key):
        pending = asyncio.create_task(bridge.handle(request))
        await asyncio.wait_for(streamed.wait(), 2)
        assert (await bridge.handle(cancel)).payload["cancelled"] is True
        await asyncio.wait_for(writing.wait(), 2)
        assert (await bridge.handle(cancel)).payload["cancelled"] is True
        assert (await bridge.handle({"method": "health"})).payload["ok"] is True
        async def push():
            pushed.set()
        proactive = asyncio.create_task(runtime.bus.chat_lane.run_non_passive("desktop", key, push))
        await asyncio.sleep(0)
        assert not pushed.is_set(), "proactive sends must wait until the cancelled input is durable"
        busy = await bridge.handle({**request, "id": "too-early"})
        assert busy.error is not None and "正在回复" in busy.error.message
        assert not pending.done()

    result = await asyncio.wait_for(pending, 2)
    await asyncio.wait_for(proactive, 2)
    assert pushed.is_set()
    assert result.error is None and result.payload["cancelled"] is True
    assert not bridge._chat_interrupts and not runtime.loop._interrupt_states
    assert (await bridge.handle(cancel)).payload["cancelled"] is False
    with closing(sqlite3.connect(runtime.session_manager.db_path)) as db:
        expected = [("user", "请回复")] + ([("assistant", partial)] if partial else [])
        assert db.execute("SELECT role, content FROM messages WHERE session_key = ? ORDER BY seq", (key,)).fetchall() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_cancel_after_reasoning_keeps_completed_reply_once(desktop, monkeypatch, committed):
    runtime, bridge = desktop
    key = "desktop:cancel-complete"
    reached_commit = asyncio.Event()
    append_messages = runtime.session_manager.append_messages
    first_append = True

    async def pause_first_append(session, messages):
        nonlocal first_append
        if not first_append:
            await append_messages(session, messages)
            return
        first_append = False
        if committed:
            await append_messages(session, messages)
        reached_commit.set()
        await asyncio.Event().wait()

    async def complete(**kwargs):
        await kwargs["on_content_delta"]({"content_delta": "部分回复"})
        return LLMResponse(content="完整回复")

    monkeypatch.setattr(runtime.session_manager, "append_messages", pause_first_append)
    runtime.provider.chat = complete
    pending = asyncio.create_task(bridge.handle({"id": "complete", "method": "chat.send", "payload": {"session_key": key, "content": "请回复"}}))
    await asyncio.wait_for(reached_commit.wait(), 2)
    assert (await bridge.handle({"method": "chat.cancel", "payload": {"session_key": key}})).payload["cancelled"] is True
    result = await asyncio.wait_for(pending, 2)
    assert result.error is None and result.payload["cancelled"] is True
    assert result.payload["session"]["messages"][-1]["content"] == "完整回复"
    assert not result.payload["session"]["messages"][-1]["metadata"].get("interrupted_reply")
    assert INTERRUPTED_TURN_METADATA_KEY not in result.payload["session"]["metadata"]
    with closing(sqlite3.connect(runtime.session_manager.db_path)) as db:
        assert db.execute("SELECT role, content FROM messages WHERE session_key = ? ORDER BY seq", (key,)).fetchall() == [
            ("user", "请回复"), ("assistant", "完整回复"),
        ]
    assert not runtime.loop._interrupt_states


@pytest.mark.asyncio
async def test_provider_finishing_during_cancel_discards_stale_snapshot(desktop):
    runtime, bridge = desktop
    key = "desktop:cancel-finished"
    streamed = asyncio.Event()

    async def finish_on_cancel(**kwargs):
        await kwargs["on_content_delta"]({"content_delta": "部分回复"})
        streamed.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return LLMResponse(content="完整回复")

    runtime.provider.chat = finish_on_cancel
    pending = asyncio.create_task(bridge.handle({"id": "finished", "method": "chat.send", "payload": {"session_key": key, "content": "请回复"}}))
    await asyncio.wait_for(streamed.wait(), 2)
    assert (await bridge.handle({"method": "chat.cancel", "payload": {"session_key": key}})).payload["cancelled"] is True
    result = await asyncio.wait_for(pending, 2)
    assert result.error is None and result.payload["reply"] == "完整回复"
    assert not bridge._chat_interrupts and not runtime.loop._interrupt_states
    with closing(sqlite3.connect(runtime.session_manager.db_path)) as db:
        assert db.execute("SELECT role, content FROM messages WHERE session_key = ? ORDER BY seq", (key,)).fetchall() == [
            ("user", "请回复"), ("assistant", "完整回复"),
        ]


@pytest.mark.asyncio
async def test_cancel_queued_desktop_request_does_not_interrupt_another_turn(desktop):
    runtime, bridge = desktop
    key = "desktop:shared"
    started = asyncio.Event()
    release = asyncio.Event()

    async def complete_later(**kwargs):
        started.set()
        await release.wait()
        return LLMResponse(content="先前任务完成")

    runtime.provider.chat = complete_later
    active = asyncio.create_task(runtime.loop.process_direct("先前任务", session_key=key))
    try:
        await asyncio.wait_for(started.wait(), 2)
        queued = asyncio.create_task(bridge.handle({"id": "queued", "method": "chat.send", "payload": {"session_key": key, "content": "等待执行"}}))
        await asyncio.sleep(0)
        assert key in bridge._requests
        assert (await bridge.handle({"method": "chat.cancel", "payload": {"session_key": key}})).payload["cancelled"] is True
        result = await asyncio.wait_for(queued, 2)
        assert result.error is None and result.payload["cancelled"] is True
        assert not active.done()
    finally:
        release.set()
        assert await asyncio.wait_for(active, 2) == "先前任务完成"
    with closing(sqlite3.connect(runtime.session_manager.db_path)) as db:
        assert db.execute("SELECT role, content FROM messages WHERE session_key = ? ORDER BY seq", (key,)).fetchall() == [
            ("user", "等待执行"),
            ("user", "先前任务"), ("assistant", "先前任务完成"),
        ]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_lane", ["integration", "global"])
@pytest.mark.parametrize("cancel_method", ["chat.cancel", "voice.turn.cancel"])
async def test_server_cancels_chat_before_dispatch_slot_and_preserves_input(desktop, monkeypatch, blocked_lane, cancel_method):
    from desktop_bridge.request_dispatcher import BridgeRequestDispatcher

    runtime, old_bridge = desktop
    await old_bridge.aclose()
    dispatcher = BridgeRequestDispatcher(max_concurrency=1, integration_concurrency=1)
    monkeypatch.setattr("desktop_bridge.server.BridgeRequestDispatcher", lambda: dispatcher)
    server = DesktopBridgeServer(runtime)
    incoming = asyncio.Queue()
    responses = asyncio.Queue()
    frames = []
    calls = 0
    key = "desktop:dispatch-queued"

    async def complete(**kwargs):
        nonlocal calls
        calls += 1
        return LLMResponse(content="retry complete")

    async def write(frame):
        frames.append(frame)
        if frame["type"] == "response":
            responses.put_nowait(frame)

    async def rpc(request):
        incoming.put_nowait(json.dumps(request))
        return await asyncio.wait_for(responses.get(), 2)

    runtime.provider.chat = complete
    serving = asyncio.create_task(server.serve_streams(read_line=incoming.get, write_payload=write))
    slot = dispatcher._integration_semaphore if blocked_lane == "integration" else dispatcher._semaphore
    await slot.acquire()
    slot_held = True
    try:
        incoming.put_nowait(json.dumps({"id": "queued", "method": "chat.send", "payload": {
            "session_key": key, "content": "keep this input", "media": ["queued.png"],
            "voice_turn_id": "queued-voice", "input_method": "voice",
        }}))
        # This control RPC is a barrier: the preceding chat has entered the dispatcher.
        cancel = await rpc({"id": "cancel", "method": cancel_method, "payload": {
            "session_key": key, "voice_turn_id": "queued-voice",
        }})
        assert cancel["id"] == "cancel" and cancel["payload"]["cancelled"] is True
        stopped = await asyncio.wait_for(responses.get(), 2)
        assert stopped["id"] == "queued" and stopped["payload"]["cancelled"] is True
        assert calls == 0
        assert not server.service._requests and not server.service._chat_tasks
        assert not server.service._voice_turns and not server.service._chat_interrupts
        runtime.session_manager.invalidate(key)
        saved = runtime.session_manager.get_or_create(key)
        assert [(m["role"], m["content"]) for m in saved.messages] == [("user", "keep this input")]
        assert saved.messages[0]["media"] == ["queued.png"]
        assert saved.messages[0]["metadata"]["input_method"] == "voice"
        assert not any(f["method"] == "chat.done" for f in frames)
        stale = await rpc({"id": "stale", "method": cancel_method, "payload": {
            "session_key": key, "voice_turn_id": "queued-voice",
        }})
        assert stale["payload"]["cancelled"] is False
        slot.release()
        slot_held = False
        retry = await rpc({"id": "retry", "method": "chat.send", "payload": {
            "session_key": key, "content": "retry",
        }})
        assert retry["error"] is None and retry["payload"]["reply"] == "retry complete"
        assert calls == 1
        runtime.session_manager.invalidate(key)
        assert [(m["role"], m["content"]) for m in runtime.session_manager.get_or_create(key).messages] == [
            ("user", "keep this input"), ("user", "retry"), ("assistant", "retry complete"),
        ]
    finally:
        if slot_held:
            slot.release()
        incoming.put_nowait(None)
        await asyncio.wait_for(serving, 2)
    assert calls == 1


@pytest.mark.asyncio
async def test_voice_without_tts_emits_reply_events_before_rpc_response(desktop):
    runtime, old_bridge = desktop
    await old_bridge.aclose()
    server = DesktopBridgeServer(runtime)
    incoming = asyncio.Queue()
    frames = []
    finished = asyncio.Event()

    async def provider(**kwargs):
        return LLMResponse(content="text reply")

    async def write(frame):
        frames.append(frame)
        if frame["type"] == "response":
            finished.set()

    runtime.provider.chat = provider
    assert not runtime.config.voice.tts.enabled
    serving = asyncio.create_task(server.serve_streams(read_line=incoming.get, write_payload=write))
    try:
        incoming.put_nowait(json.dumps({"id": "voice-contract", "method": "chat.send", "payload": {
            "session_key": "desktop:voice-contract", "content": "hello", "input_method": "voice",
            "voice_turn_id": "voice-contract",
        }}))
        await asyncio.wait_for(finished.wait(), 2)
        assert [(frame["type"], frame["method"]) for frame in frames] == [
            ("event", "chat.done"), ("event", "voice.reply.started"),
            ("event", "voice.tts.finished"), ("response", "chat.send"),
        ]
        assert frames[1]["payload"]["has_voice"] is False
        assert frames[1]["payload"]["voice_turn_id"] == frames[2]["payload"]["voice_turn_id"] == "voice-contract"
        assert frames[-1]["error"] is None
        runtime.session_manager.invalidate("desktop:voice-contract")
        assert [(m["role"], m["content"]) for m in runtime.session_manager.get_or_create("desktop:voice-contract").messages] == [
            ("user", "hello"), ("assistant", "text reply"),
        ]
    finally:
        incoming.put_nowait(None)
        await asyncio.wait_for(serving, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["before_append", "second_message", "after_commit", "after_append"])
@pytest.mark.parametrize("invalidate", [False, True])
async def test_retry_recovers_failed_cancel_write_before_model_and_survives_reload(desktop, monkeypatch, tmp_path, failure_point, invalidate):
    from session.manager import SessionManager

    runtime, bridge = desktop
    manager = runtime.session_manager
    key = "desktop:failed-cancel-write"
    attachment = tmp_path / "retained.png"
    Image.new("RGB", (2, 2)).save(attachment)
    started = asyncio.Event()
    append = manager.append_messages
    insert = manager._store.insert_message
    attempts = 0

    async def partial(**kwargs):
        await kwargs["on_content_delta"]({"content_delta": "partial", "thinking_delta": "reasoning"})
        started.set()
        await asyncio.Event().wait()

    async def failing_append(session, messages):
        if failure_point == "before_append":
            raise OSError("injected write failure")
        await append(session, messages)
        if failure_point == "after_append":
            raise OSError("injected write failure")

    def failing_insert(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if failure_point == "second_message" and attempts == 2:
            raise OSError("injected write failure")
        result = insert(*args, **kwargs)
        if failure_point == "after_commit" and attempts == 1:
            raise OSError("injected write failure")
        return result

    runtime.provider.chat = partial
    request = {"id": "reused-id", "method": "chat.send", "payload": {
        "session_key": key, "content": "original", "media": [str(attachment)],
    }}
    pending = asyncio.create_task(bridge.handle(request))
    await asyncio.wait_for(started.wait(), 2)
    monkeypatch.setattr(manager, "append_messages", failing_append)
    monkeypatch.setattr(manager._store, "insert_message", failing_insert)
    await bridge.handle({"method": "chat.cancel", "payload": {"session_key": key}})
    failed = await asyncio.wait_for(pending, 2)
    assert failed.error is not None and "injected write failure" in failed.error.message
    assert not bridge._requests and not bridge._chat_tasks
    monkeypatch.setattr(manager, "append_messages", append)
    monkeypatch.setattr(manager._store, "insert_message", insert)
    if invalidate:
        manager.invalidate(key)

    async def complete(**kwargs):
        # Check durable recovery before the next provider can produce side effects.
        rows = manager._store.fetch_session_messages(key)
        assert [(m["role"], m["content"]) for m in rows] == [("user", "original"), ("assistant", "partial")]
        assert rows[0]["media"] == [str(attachment)]
        assert rows[1]["reasoning_content"] == "reasoning"
        assert rows[1]["metadata"]["interrupted_reply"] is True
        assert sum(m.get("content") == "partial" for m in kwargs["messages"]) == 1
        return LLMResponse(content="recovered")

    runtime.provider.chat = complete
    retry = await bridge.handle({"id": "reused-id", "method": "chat.send", "payload": {"session_key": key, "content": "continue"}})
    assert retry.error is None
    assert not runtime.loop._interrupt_states
    expected = [("user", "original"), ("assistant", "partial"), ("user", "continue"), ("assistant", "recovered")]
    # Reopen the real SQLite store independently of the original session cache.
    reopened = SessionManager(manager.workspace)
    try:
        saved = reopened.get_or_create(key)
        assert [(m["role"], m["content"]) for m in saved.messages] == expected
        assert len({m["id"] for m in saved.messages}) == 4
        assert saved.messages[0]["media"] == [str(attachment)]
        assert saved.messages[1]["reasoning_content"] == "reasoning"
        assert INTERRUPTED_TURN_METADATA_KEY not in saved.metadata
    finally:
        reopened._store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", ["", "retained partial"])
@pytest.mark.parametrize("cancel_method", ["chat.cancel", "voice.turn.cancel"])
async def test_failed_recovery_blocks_provider_until_storage_recovers(desktop, monkeypatch, partial, cancel_method):
    runtime, bridge = desktop
    key = "desktop:persistent-failure"
    manager = runtime.session_manager
    started = asyncio.Event()
    calls = 0
    append = manager.append_messages

    async def blocked(**kwargs):
        nonlocal calls
        calls += 1
        if partial:
            await kwargs["on_content_delta"]({"content_delta": partial})
        started.set()
        await asyncio.Event().wait()

    async def fail_target(session, messages):
        if session.key == key:
            raise OSError("database unavailable")
        await append(session, messages)

    async def complete(**kwargs):
        nonlocal calls
        calls += 1
        return LLMResponse(content="complete")

    runtime.provider.chat = blocked
    request = {"id": "same-id", "method": "chat.send", "payload": {"session_key": key, "content": "original", "voice_turn_id": "failed-voice"}}
    pending = asyncio.create_task(bridge.handle(request))
    await asyncio.wait_for(started.wait(), 2)
    monkeypatch.setattr(manager, "append_messages", fail_target)
    await bridge.handle({"method": cancel_method, "payload": {"session_key": key, "voice_turn_id": "failed-voice"}})
    failed = await asyncio.wait_for(pending, 2)
    assert failed.error is not None
    runtime.provider.chat = complete
    retry = {"id": "same-id", "method": "chat.send", "payload": {"session_key": key, "content": "continue"}}
    for _ in range(3):
        manager.invalidate(key)
        result = await bridge.handle(retry)
        assert result.error is not None and "database unavailable" in result.error.message
        assert calls == 1
        assert not bridge._requests and not bridge._chat_tasks
    deletion = await bridge.handle({"method": "session.delete", "payload": {"session_key": key}})
    assert deletion.error is not None
    unrelated = await bridge.handle({"method": "chat.send", "payload": {"session_key": "desktop:healthy", "content": "hello"}})
    assert unrelated.error is None and calls == 2
    monkeypatch.setattr(manager, "append_messages", append)
    result = await bridge.handle(retry)
    assert result.error is None and calls == 3
    assert not bridge._pending_chat_writes and not runtime.loop._interrupt_states
    expected = [("user", "original")] + ([("assistant", partial)] if partial else []) + [("user", "continue"), ("assistant", "complete")]
    again = await bridge.handle({**retry, "id": "next", "payload": {"session_key": key, "content": "next"}})
    assert again.error is None
    expected += [("user", "next"), ("assistant", "complete")]
    manager.invalidate(key)
    assert [(m["role"], m["content"]) for m in manager.get_or_create(key).messages] == expected


@pytest.mark.asyncio
async def test_cancelling_retry_during_recovery_keeps_both_inputs_once(desktop, monkeypatch):
    runtime, bridge = desktop
    manager = runtime.session_manager
    key = "desktop:cancel-recovery"
    started = asyncio.Event()
    writing = asyncio.Event()
    append = manager.append_messages
    calls = 0

    async def partial(**kwargs):
        nonlocal calls
        calls += 1
        await kwargs["on_content_delta"]({"content_delta": "partial"})
        started.set()
        await asyncio.Event().wait()

    async def fail(session, messages):
        raise OSError("temporary failure")

    async def record_write(session, messages):
        writing.set()
        await append(session, messages)

    runtime.provider.chat = partial
    request = {"id": "reused", "method": "chat.send", "payload": {"session_key": key, "content": "original"}}
    pending = asyncio.create_task(bridge.handle(request))
    await asyncio.wait_for(started.wait(), 2)
    monkeypatch.setattr(manager, "append_messages", fail)
    cancel = {"method": "chat.cancel", "payload": {"session_key": key}}
    await bridge.handle(cancel)
    assert (await asyncio.wait_for(pending, 2)).error is not None
    manager.invalidate(key)
    monkeypatch.setattr(manager, "append_messages", record_write)
    async with manager._lock(key):
        retry = asyncio.create_task(bridge.handle({**request, "payload": {"session_key": key, "content": "new input"}}))
        await asyncio.wait_for(writing.wait(), 2)
        assert (await bridge.handle(cancel)).payload["cancelled"] is True
        await asyncio.sleep(0)
        assert (await bridge.handle(cancel)).payload["cancelled"] is True
        assert not retry.done()
    result = await asyncio.wait_for(retry, 2)
    assert result.error is None and result.payload["cancelled"] is True
    assert calls == 1
    assert not bridge._pending_chat_writes and not runtime.loop._interrupt_states
    manager.invalidate(key)
    assert [(m["role"], m["content"]) for m in manager.get_or_create(key).messages] == [
        ("user", "original"), ("assistant", "partial"), ("user", "new input"),
    ]

@pytest.mark.asyncio
@pytest.mark.parametrize('failure_point', ['before_append', 'second_message', 'after_commit', 'journal_cleanup'])
async def test_cancel_journal_recovers_in_fresh_runtime_without_replaying_model(desktop, monkeypatch, failure_point):
    runtime, bridge = desktop
    manager = runtime.session_manager
    key = 'desktop:durable-recovery'
    started = asyncio.Event()
    append = manager.append_messages
    insert = manager._store.insert_message
    attempts = 0

    async def partial(**kwargs):
        await kwargs['on_content_delta']({'content_delta': 'retained partial', 'thinking_delta': 'retained thinking'})
        started.set()
        await asyncio.Event().wait()

    async def fail_append(session, messages):
        raise OSError('database unavailable')

    def fail_insert(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if failure_point == 'second_message' and attempts == 2:
            raise OSError('database unavailable')
        row = insert(*args, **kwargs)
        if failure_point == 'after_commit' and attempts == 1:
            raise OSError('commit acknowledgement lost')
        return row

    def fail_remove(key):
        raise OSError('journal cleanup failed')

    runtime.provider.chat = partial
    pending = asyncio.create_task(bridge.handle({'id': 'original', 'method': 'chat.send', 'payload': {
        'session_key': key, 'content': 'original input', 'media': ['retained.txt'],
    }}))
    await asyncio.wait_for(started.wait(), 2)
    if failure_point == 'before_append':
        monkeypatch.setattr(manager, 'append_messages', fail_append)
    elif failure_point == 'journal_cleanup':
        monkeypatch.setattr(bridge._chat_journal, 'remove', fail_remove)
    else:
        monkeypatch.setattr(manager._store, 'insert_message', fail_insert)
    await bridge.handle({'method': 'chat.cancel', 'payload': {'session_key': key}})
    assert (await pending).error is not None
    assert key in bridge._chat_journal.load()
    monkeypatch.setattr(manager, 'append_messages', append)
    monkeypatch.setattr(manager._store, 'insert_message', insert)

    # A real restart relinquishes the transport owner's lease before its successor starts.
    runtime.bus.transport.close()
    http = SharedHttpResources()
    restarted = build_core_runtime(Config(provider='openai', model='fake', api_key='fake'), manager.workspace, http)
    recovered = DesktopBridgeService(restarted)
    async def forbidden(**kwargs):
        pytest.fail('journal recovery must never invoke the provider')
    restarted.provider.chat = forbidden
    try:
        await recovered.recover_pending_chat_writes()
        rows = restarted.session_manager._store.fetch_session_messages(key)
        assert [(m['role'], m['content']) for m in rows] == [('user', 'original input'), ('assistant', 'retained partial')]
        assert rows[0]['media'] == ['retained.txt']
        assert rows[1]['reasoning_content'] == 'retained thinking'
        assert rows[1]['metadata']['interrupted_reply'] is True
        assert not recovered._chat_journal.load()
        assert not recovered._pending_chat_writes
        ids = [m['id'] for m in rows]
        await recovered.recover_pending_chat_writes()
        assert [m['id'] for m in restarted.session_manager._store.fetch_session_messages(key)] == ids
    finally:
        await recovered.aclose()
        await restarted.stop()
        await restarted.memory_runtime.aclose()
        restarted.session_manager._store.close()
        await http.aclose()


@pytest.mark.asyncio
async def test_journal_survives_failed_startup_recovery_and_later_retry(desktop, monkeypatch):
    runtime, bridge = desktop
    key = 'desktop:startup-recovery'
    message = {'role': 'user', 'content': 'saved input', 'desktop_write_id': 'stable-input'}
    bridge._chat_journal.save(key, [message], 'original title', None)
    recovered = DesktopBridgeService(runtime)
    append = runtime.session_manager.append_messages
    async def unavailable(session, messages):
        raise OSError('still unavailable')
    monkeypatch.setattr(runtime.session_manager, 'append_messages', unavailable)
    try:
        await recovered.recover_pending_chat_writes()
        assert key in recovered._chat_journal.load()
        health = await recovered.handle({'method': 'health'})
        assert health.payload['pending_chat_writes'] == 1
        assert (await recovered.handle({'method': 'session.delete', 'payload': {'session_key': key}})).error
        monkeypatch.setattr(runtime.session_manager, 'append_messages', append)
        async def provider(**kwargs):
            assert len(runtime.session_manager._store.fetch_session_messages(key)) == 1
            return LLMResponse(content='complete')
        runtime.provider.chat = provider
        result = await recovered.handle({'method': 'chat.send', 'payload': {'session_key': key, 'content': 'continue'}})
        assert result.error is None
        assert [m['content'] for m in result.payload['session']['messages']] == ['saved input', 'continue', 'complete']
        assert not recovered._chat_journal.load()
    finally:
        await recovered.aclose()


@pytest.mark.asyncio
async def test_execution_request_replay_and_conflict_do_not_call_model_again(desktop):
    runtime, bridge = desktop
    calls = 0
    async def provider(**kwargs):
        nonlocal calls
        calls += 1
        return LLMResponse(content="durable result")
    runtime.provider.chat = provider
    payload = {"session_key": "desktop:contract", "content": "original", "execution_request_id": "logical-request"}
    first = await bridge.handle({"id": "rpc-a", "method": "chat.send", "payload": payload})
    second = await bridge.handle({"id": "rpc-b", "method": "chat.send", "payload": payload})
    assert first.error is None and second.error is None
    assert first.payload["reply"] == second.payload["reply"] == "durable result"
    assert calls == 1
    assert len(second.payload["session"]["messages"]) == 2
    conflict = await bridge.handle({"method": "chat.send", "payload": {**payload, "content": "different"}})
    assert conflict.error is not None and "不同内容" in conflict.error.message
    assert calls == 1
    status = await bridge.handle({"method": "execution.get", "payload": payload})
    assert status.error is None and status.payload["status"] == "completed"
    events = await bridge.handle({"method": "execution.events", "payload": {"turn_id": status.payload["turn_id"]}})
    kinds = [e["kind"] for e in events.payload["events"]]
    assert kinds[0:2] == ["request.accepted", "run.started"]
    assert "messages.committed" in kinds and kinds[-1] == "run.ended"
    listed = await bridge.handle({"method": "executions.list", "payload": payload})
    assert listed.payload["executions"][0]["turn_id"] == status.payload["turn_id"]


@pytest.mark.asyncio
async def test_execution_records_provider_error_without_claiming_success(desktop):
    runtime, bridge = desktop
    async def fail(**kwargs):
        raise RuntimeError("private upstream detail")
    runtime.provider.chat = fail
    payload = {"session_key": "desktop:failed-contract", "content": "input", "execution_request_id": "failure"}
    response = await bridge.handle({"method": "chat.send", "payload": payload})
    assert response.error is not None
    row = (await bridge.handle({"method": "execution.get", "payload": payload})).payload
    assert row["status"] == "failed"
    events = runtime.session_manager.execution_journal.events(row["turn_id"])["events"]
    assert "private upstream detail" not in json.dumps(events)
    assert any(e["kind"] == "run.error" for e in events)


@pytest.mark.asyncio
async def test_standalone_proactive_push_has_durable_dispatch_and_receipt(desktop):
    runtime, _ = desktop
    journal = runtime.session_manager.execution_journal
    async def sender(chat_id, text):
        rows = journal.list_executions("probe:42")["executions"]
        kinds = [e["kind"] for e in journal.events(rows[0]["turn_id"])["events"]]
        assert kinds[-1] == "delivery.dispatched"
        return type("Receipt", (), {"message_id": 123})()
    runtime.push_tool.register_channel("probe", text=sender)
    receipt = await runtime.push_tool.send(channel="probe", chat_id="42", message="test reminder")
    assert receipt.ok
    row = journal.list_executions("probe:42")["executions"][0]
    assert row["status"] == "completed"
    returned = [e["payload"] for e in journal.events(row["turn_id"])["events"] if e["kind"] == "delivery.returned"]
    assert returned[0]["message_id"] == "123" and returned[0]["certainty"] == "confirmed"
