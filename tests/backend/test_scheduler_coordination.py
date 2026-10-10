"""真实宿主验证调度推理隔离、投递提交、桌面协调与取消。"""

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from agent.config_models import Config
from agent.provider import ContentSafetyError, ContextLengthError, LLMResponse
from agent.scheduler import ScheduledJob
from agent.tools.message_push import DeliveryReceipt
from agent.turns.orchestrator import TurnOrchestrator, TurnOrchestratorDeps
from agent.turns.result import TurnOutbound, TurnResult
from agent.looping.ports import SessionServices
from bootstrap.proactive import _PushPort
from bootstrap.tools import build_core_runtime
from bus.events import InboundMessage
from core.net.http import SharedHttpResources
from desktop_bridge.session_service import DesktopBridgeService

KEY = "desktop:target"


@pytest_asyncio.fixture
async def desktop(tmp_path):
    http = SharedHttpResources()
    runtime = build_core_runtime(
        Config(
            provider="openai",
            model="fake",
            api_key="fake",
            memory_optimizer_enabled=False,
        ),
        tmp_path,
        http,
    )
    bridge = DesktopBridgeService(runtime)
    try:
        yield runtime, bridge
    finally:
        await bridge.aclose()
        await runtime.stop()
        await runtime.memory_runtime.aclose()
        runtime.session_manager._store.close()
        await http.aclose()


def job(tier="soft", **kwargs):
    return ScheduledJob(
        trigger="at",
        tier=tier,
        fire_at=datetime.now(timezone.utc),
        channel="desktop",
        chat_id=KEY,
        session_key=KEY,
        prompt="后台提示" if tier == "soft" else None,
        message="定时提醒" if tier == "instant" else None,
        **kwargs,
    )


def durable_contents(runtime, key):
    with closing(sqlite3.connect(runtime.session_manager.db_path)) as connection:
        return [
            row[0]
            for row in connection.execute(
                "SELECT content FROM messages WHERE session_key = ? ORDER BY seq",
                (key,),
            )
        ]


@pytest.mark.asyncio
async def test_process_direct_preserves_existing_positional_channel_arguments(desktop):
    runtime, _ = desktop
    runtime.provider.chat = AsyncMock(return_value=LLMResponse("普通回复"))
    reply = await runtime.loop.process_direct(
        "普通输入", KEY, "desktop", KEY, skip_post_memory=True
    )
    assert reply == "普通回复"
    assert durable_contents(runtime, KEY) == ["普通输入", "普通回复"]
    assert (
        runtime.session_manager.get_or_create(KEY).messages[-1]["metadata"][
            "source"
        ]
        == "desktop"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [False, True])
async def test_soft_inference_ignores_all_history_and_commits_only_delivered_output(
    desktop, retry
):
    runtime, bridge = desktop
    scheduled = job()
    legacy_key = f"scheduler:{scheduled.id}"
    for key, content in [(KEY, "目标历史秘密"), (legacy_key, "旧调度秘密")]:
        session = runtime.session_manager.get_or_create(key)
        session.add_message("user", content)
        await runtime.session_manager.save_async(session)
    captured = []

    async def chat(**kwargs):
        captured.append(json.dumps(kwargs["messages"], ensure_ascii=False))
        if retry and len(captured) == 1:
            raise ContextLengthError("context overflow")
        return LLMResponse("后台结果 §cited:[internal]§")

    notifications = []

    def listener(event):
        if event["method"] == "message.pushed":
            notifications.append((event, durable_contents(runtime, KEY)))

    bridge.add_event_listener(listener)
    runtime.provider.chat = chat
    await runtime.scheduler._execute(scheduled)
    assert all(
        "后台提示" in messages
        and "目标历史秘密" not in messages
        and "旧调度秘密" not in messages
        for messages in captured
    )
    assert len(captured) == (2 if retry else 1)
    assert durable_contents(runtime, legacy_key) == ["旧调度秘密"]
    assert durable_contents(runtime, KEY) == ["目标历史秘密", "后台结果"]
    assert len(notifications) == 1 and notifications[0][1] == [
        "目标历史秘密",
        "后台结果",
    ]
    message = runtime.session_manager.get_or_create(KEY).messages[-1]
    assert message["proactive"] and message["metadata"]["source"] == "scheduler"
    assert message["metadata"]["session_key_override"] == KEY
    assert message["metadata"]["request_id"] == scheduled.id
    assert not runtime.loop.processing_state.is_busy(KEY)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["empty", "raise", "safety", "overflow", "timeout", "send", "sender_raise"],
)
async def test_no_phantom_session_or_history_when_no_output_is_delivered(
    desktop, failure
):
    runtime, bridge = desktop
    scheduled = job()
    events = []
    bridge.add_event_listener(events.append)

    async def chat(**kwargs):
        if failure == "raise":
            raise RuntimeError("provider offline")
        if failure == "safety":
            raise ContentSafetyError("safety rejected")
        if failure == "overflow":
            raise ContextLengthError("overflow")
        if failure == "timeout":
            raise asyncio.TimeoutError("timeout")
        return LLMResponse("" if failure == "empty" else "后台结果")

    runtime.provider.chat = chat
    if failure in {"send", "sender_raise"}:
        sender = AsyncMock(
            return_value=DeliveryReceipt(
                False, "desktop", KEY, "offline", error="offline"
            )
        )
        if failure == "sender_raise":
            sender.side_effect = RuntimeError("offline")
        runtime.push_tool.register_channel("desktop", text=sender, pending_text=sender)
    if failure in {"raise", "safety", "overflow", "timeout"}:
        with pytest.raises(RuntimeError) as error:
            await runtime.scheduler._execute(scheduled)
        assert error.value.__cause__ is not None
    else:
        await runtime.scheduler._execute(scheduled)
    assert runtime.session_manager.list_sessions() == []
    assert not any(event["method"] == "message.pushed" for event in events)
    assert not runtime.loop.processing_state.is_busy(KEY)
    assert not runtime.bus.chat_lane._states


@pytest.mark.asyncio
async def test_soft_job_and_user_turn_use_separate_execution_keys_and_order_sends(
    desktop,
):
    runtime, bridge = desktop
    user_started, job_started = asyncio.Event(), asyncio.Event()
    release_user, release_job = asyncio.Event(), asyncio.Event()

    async def chat(**kwargs):
        if "后台提示" in str(kwargs["messages"][-1]["content"]):
            job_started.set()
            await release_job.wait()
            return LLMResponse("后台结果")
        user_started.set()
        await release_user.wait()
        return LLMResponse("用户回复")

    events = []
    bridge.add_event_listener(events.append)
    runtime.provider.chat = chat
    scheduled = job()
    scheduled_task = asyncio.create_task(runtime.scheduler._execute(scheduled))
    user_task = None
    try:
        await asyncio.wait_for(job_started.wait(), 2)
        assert runtime.loop.processing_state.is_busy(KEY)
        assert not runtime.loop.processing_state.is_busy(f"desktop:{KEY}")
        user_task = asyncio.create_task(
            bridge.handle(
                {
                    "method": "chat.send",
                    "payload": {"session_key": KEY, "content": "用户输入"},
                }
            )
        )
        await asyncio.wait_for(user_started.wait(), 2)
        assert (
            f"scheduler:{scheduled.id}" in runtime.loop._active_tasks
            and KEY in runtime.loop._active_tasks
        )
        release_job.set()
        await asyncio.sleep(0.03)
        assert not scheduled_task.done()
        assert not any(event["method"] == "message.pushed" for event in events)
        assert runtime.loop.processing_state.is_busy(KEY)
        release_user.set()
        response = await asyncio.wait_for(user_task, 2)
        assert response.error is None
        await asyncio.wait_for(scheduled_task, 2)
        methods = [
            event["method"]
            for event in events
            if event["method"] in {"chat.done", "message.pushed"}
        ]
        assert methods == ["chat.done", "message.pushed"]
        assert durable_contents(runtime, KEY) == ["用户输入", "用户回复", "后台结果"]
        assert not runtime.session_manager._store.session_exists(
            f"scheduler:{scheduled.id}"
        )
        assert not runtime.loop.processing_state.is_busy(KEY)
        assert not runtime.bus.chat_lane._states
    finally:
        release_job.set()
        release_user.set()
        for task in [scheduled_task, user_task]:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *[task for task in [scheduled_task, user_task] if task is not None],
            return_exceptions=True,
        )


@pytest.mark.asyncio
async def test_cancelled_soft_job_cleans_busy_state_and_normal_chat_still_works(
    desktop,
):
    runtime, bridge = desktop
    started = asyncio.Event()

    async def chat(**kwargs):
        if "后台提示" in str(kwargs["messages"][-1]["content"]):
            started.set()
            await asyncio.Event().wait()
        return LLMResponse("正常回复")

    runtime.provider.chat = chat
    scheduled = job()
    runtime.scheduler.add_job(scheduled)
    task = asyncio.create_task(runtime.scheduler._execute(scheduled))
    runtime.scheduler._active_tasks[scheduled.id] = task
    await asyncio.wait_for(started.wait(), 2)
    assert runtime.scheduler.cancel_job(scheduled.id)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.session_manager.list_sessions() == []
    assert not runtime.loop.processing_state.is_busy(KEY)
    assert not runtime.loop._active_tasks
    response = await bridge.handle(
        {"method": "chat.send", "payload": {"session_key": KEY, "content": "用户输入"}}
    )
    assert response.error is None and response.payload["reply"] == "正常回复"
    assert durable_contents(runtime, KEY) == ["用户输入", "正常回复"]


@pytest.mark.asyncio
@pytest.mark.parametrize("proactive", [False, True])
async def test_cancel_after_delivery_finishes_history_commit_before_next_send(
    desktop, proactive
):
    runtime, bridge = desktop
    entered, release = asyncio.Event(), asyncio.Event()
    original_append = runtime.session_manager.append_messages

    async def held_append(session, messages):
        if any(message["content"] == "第一条" for message in messages):
            entered.set()
            await release.wait()
        await original_append(session, messages)

    runtime.session_manager.append_messages = held_append
    if proactive:
        port = _PushPort(
            runtime,
            SimpleNamespace(
                can_send=lambda: True, _target_session_key=lambda: KEY, delivered=False
            ),
        )
        orchestrator = TurnOrchestrator(
            TurnOrchestratorDeps(
                session=SessionServices(
                    session_manager=runtime.session_manager, presence=runtime.presence
                ),
                outbound=port,
                event_bus=runtime.event_bus,
                delivery_scope=port.delivery_scope,
            )
        )
        first = asyncio.create_task(
            orchestrator.handle_proactive_turn(
                result=TurnResult("reply", TurnOutbound(KEY, "第一条")),
                session_key=KEY,
                channel="desktop",
                chat_id=KEY,
            )
        )
    else:
        scheduled = job("instant")
        scheduled.message = "第一条"
        first = asyncio.create_task(runtime.scheduler._execute(scheduled))
    await asyncio.wait_for(entered.wait(), 2)
    first.cancel()
    second = job("instant")
    second.message = "第二条"
    next_task = asyncio.create_task(runtime.scheduler._execute(second))
    try:
        await asyncio.sleep(0.03)
        assert not first.done() and not next_task.done()
        assert durable_contents(runtime, KEY) == []
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(next_task, 2)
        assert durable_contents(runtime, KEY) == ["第一条", "第二条"]
        assert not runtime.bus.chat_lane._states
    finally:
        release.set()
        await asyncio.gather(first, next_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_runner", [False, True])
async def test_channel_loop_cancellation_releases_admitted_turn(desktop, cancel_runner):
    runtime, _ = desktop
    channel_key = "telegram:42"
    runtime.push_tool.register_channel("telegram", text=AsyncMock())
    started = asyncio.Event()

    async def chat(**kwargs):
        started.set()
        await asyncio.Event().wait()

    runtime.provider.chat = chat
    runner = asyncio.create_task(runtime.loop.run())
    await runtime.bus.publish_inbound(
        InboundMessage(
            channel="telegram",
            chat_id="42",
            sender="user",
            content="用户输入",
        )
    )
    scheduled = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        scheduled = asyncio.create_task(
            runtime.scheduler._execute(
                ScheduledJob(
                    trigger="at",
                    tier="instant",
                    fire_at=datetime.now(timezone.utc),
                    channel="telegram",
                    chat_id="42",
                    session_key=channel_key,
                    message="定时提醒",
                )
            )
        )
        await asyncio.sleep(0)
        assert not scheduled.done()
        if cancel_runner:
            runner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(runner, 2)
        else:
            turn = runtime.loop._active_tasks[channel_key]
            turn.cancel()
        await asyncio.wait_for(scheduled, 2)
        assert durable_contents(runtime, channel_key) == ["定时提醒"]
        assert not runtime.loop.processing_state.is_busy(channel_key)
        assert not runtime.bus.chat_lane._states
        assert runner.done() is cancel_runner
    finally:
        runner.cancel()
        await asyncio.gather(
            runner, *([scheduled] if scheduled else []), return_exceptions=True
        )


@pytest.mark.asyncio
async def test_channel_alias_joins_canonical_lane_and_commits_to_stored_session(
    desktop,
):
    runtime, _ = desktop
    sender = AsyncMock(return_value=SimpleNamespace(message_id=7))
    runtime.push_tool.register_channel(
        "telegram", text=sender, target_resolver=lambda _: "42"
    )
    scheduled = ScheduledJob(
        trigger="at",
        tier="instant",
        fire_at=datetime.now(timezone.utc),
        channel="telegram",
        chat_id="alias",
        session_key="conversation:bound",
        message="定时提醒",
    )
    async with runtime.bus.chat_lane.passive_turn("telegram", "42"):
        task = asyncio.create_task(runtime.scheduler._execute(scheduled))
        await asyncio.sleep(0)
        sender.assert_not_awaited()
    await asyncio.wait_for(task, 2)
    sender.assert_awaited_once_with("42", "定时提醒")
    assert durable_contents(runtime, "conversation:bound") == ["定时提醒"]
    assert {item["key"] for item in runtime.session_manager.list_sessions()} == {
        "conversation:bound"
    }
    metadata = runtime.session_manager.get_or_create("conversation:bound").messages[-1][
        "metadata"
    ]
    assert (
        metadata["transport_chat_id"] == "42"
        and metadata["delivery_ref"] == "telegram:7"
    )
    assert not runtime.bus.chat_lane._states
