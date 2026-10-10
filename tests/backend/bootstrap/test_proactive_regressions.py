"""跨模块回归：可选主动启动、真实图片同步和投递后的历史提交。"""

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.config_models import Config
from agent.looping.ports import SessionServices
from agent.provider import LLMResponse
from agent.tools.message_push import DeliveryReceipt, MessagePushTool
from agent.turns.orchestrator import TurnOrchestrator, TurnOrchestratorDeps
from agent.turns.result import TurnOutbound, TurnResult
from bootstrap.app import AppRuntime, RuntimeFeatures
from bootstrap.proactive import KazeProactiveLoop, _PushPort
from bus.event_bus import EventBus
from bus.queue import MessageBus
from bus.events_lifecycle import ProactiveMessageCommitted
from conversation.push_sync import ExternalImageSyncService
from proactive_v2.config_loader import load_proactive_config
from session.manager import SessionManager


def offline_runtime(tmp_path, monkeypatch, lifecycle):
    import bootstrap.tools as bootstrap_tools

    provider = SimpleNamespace(chat=AsyncMock(return_value=LLMResponse("普通聊天正常")))
    monkeypatch.setattr(bootstrap_tools, "build_providers", lambda *args: (provider, None, None))
    monkeypatch.setattr(bootstrap_tools, "_resolve_plugin_dirs", lambda *args: [])
    real_build = bootstrap_tools.build_core_runtime

    def build(*args):
        core = real_build(*args)
        core.plugin_manager = None
        return core

    monkeypatch.setattr("bootstrap.app.build_core_runtime", build)
    config = Config(provider="openai", model="offline", api_key="offline", memory_optimizer_enabled=False)
    config.proactive = load_proactive_config({
        "enabled": lifecycle is not None, "lifecycle": lifecycle or "wake",
        "session_key": "desktop:review", "default_channel": "desktop",
        "anyaction_enabled": False, "message_dedupe_enabled": False,
    })
    return AppRuntime(config, tmp_path, features=RuntimeFeatures(enable_message_channels=False))


async def assert_normal_chat(app):
    assert app._started and not app._shutdown
    reply = await app.agent_loop.process_direct(
        "普通聊天测试", session_key="desktop:review", channel="desktop",
        chat_id="desktop:review", skip_post_memory=True, raise_on_error=True,
    )
    assert reply == "普通聊天正常"
    messages = app.session_manager.get_or_create("desktop:review").messages
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert not messages[-1].get("proactive")


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", [None, "wake", "default"])
async def test_optional_proactive_modes_keep_normal_chat_working(tmp_path, monkeypatch, lifecycle):
    app = offline_runtime(tmp_path, monkeypatch, lifecycle)
    try:
        await app.start()
        await assert_normal_chat(app)
    finally:
        await app.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", ["wake", "default"])
async def test_bad_proactive_sources_do_not_close_normal_services(tmp_path, monkeypatch, caplog, lifecycle):
    app = offline_runtime(tmp_path, monkeypatch, lifecycle)
    (tmp_path / "proactive_sources.json").write_text('{"sources": [', encoding="utf-8")
    try:
        await app.start()
        assert app.core.proactive_loop is None
        assert "初始化失败" in caplog.text
        await assert_normal_chat(app)
        assert not any(task.done() for task in app._background_tasks)
    finally:
        await app.shutdown()


@pytest.mark.asyncio
async def test_failed_proactive_kernel_start_closes_owned_state(tmp_path, monkeypatch):
    app = offline_runtime(tmp_path, monkeypatch, "wake")
    failed_loops = []

    async def fail_start(loop):
        failed_loops.append(loop)
        raise RuntimeError("optional lifecycle unavailable")

    monkeypatch.setattr(KazeProactiveLoop, "_start_current_snapshot", fail_start)
    try:
        await app.start()
        assert app.core.proactive_loop is None
        assert len(failed_loops) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            failed_loops[0]._state._db.execute("SELECT 1")
        await assert_normal_chat(app)
    finally:
        await app.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("images", [["one.png"], ["one.png", "two.png"]])
@pytest.mark.parametrize("failure", [False, True])
async def test_proactive_media_commits_once_only_after_delivery(tmp_path, images, failure):
    sessions, bus = SessionManager(tmp_path), EventBus()
    ExternalImageSyncService(session_manager=sessions, event_bus=bus)
    committed = []
    bus.on(ProactiveMessageCommitted, lambda event: committed.append(event))
    text_sender = AsyncMock(return_value=None)
    image_sender = AsyncMock(return_value=DeliveryReceipt(
        not failure, "telegram", "42", "offline" if failure else "sent",
        error="offline" if failure else None,
    ))
    message_bus = MessageBus()
    push = MessagePushTool(bus, chat_lane=message_bus.chat_lane)
    push.register_channel("telegram", text=text_sender, image=image_sender)
    loop = SimpleNamespace(can_send=lambda: True, _target_session_key=lambda: "telegram:42", delivered=False)
    orchestrator = TurnOrchestrator(TurnOrchestratorDeps(
        session=SessionServices(session_manager=sessions, presence=None),
        outbound=_PushPort(SimpleNamespace(push_tool=push, bus=message_bus), loop), event_bus=bus,
    ))
    success, failed = AsyncMock(), AsyncMock()
    result = TurnResult(
        decision="reply", outbound=TurnOutbound(session_key="telegram:42", content="测试", media=images),
        success_side_effects=[SimpleNamespace(run=success)],
        failure_side_effects=[SimpleNamespace(run=failed)],
    )
    try:
        delivered = await orchestrator.handle_proactive_turn(
            result=result, session_key="telegram:42", channel="telegram", chat_id="42",
        )
        assert delivered is (not failure)
        text_sender.assert_awaited_once()
        messages = sessions.get_or_create("telegram:42").messages
        if failure:
            assert messages == [] and committed == []
            success.assert_not_awaited()
            failed.assert_awaited_once()
            image_sender.assert_awaited_once()
        else:
            assert image_sender.await_count == len(images)
            assert len(messages) == 1 and messages[0]["media"] == images
            assert messages[0]["content"] == "测试"
            assert len(committed) == 1
            success.assert_awaited_once()
            failed.assert_not_awaited()
            # 从磁盘重读，验证同步服务没有写入额外的图片消息。
            sessions.invalidate("telegram:42")
            assert len(sessions.get_or_create("telegram:42").messages) == 1
    finally:
        await bus.aclose()
        sessions._store.close()
