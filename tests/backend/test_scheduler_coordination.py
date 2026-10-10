"""真实宿主验证调度推理隔离、投递提交、桌面协调与取消。"""

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from agent.config_models import Config
from agent.provider import ContentSafetyError, ContextLengthError, LLMResponse, ToolCall
from agent.scheduler import ScheduledJob, SchedulerService
from agent.tools.message_push import DeliveryReceipt
from agent.turns.orchestrator import TurnOrchestrator, TurnOrchestratorDeps
from agent.turns.result import TurnOutbound, TurnResult
from agent.looping.ports import SessionServices
from bootstrap.proactive import _PushPort
from bootstrap.app import AppRuntime, RuntimeFeatures
from bootstrap.tools import build_core_runtime
from bus.events import InboundMessage
from bus.events_lifecycle import ProactiveMessageCommitted
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


@pytest_asyncio.fixture
async def app_runtime(tmp_path):
    """Use the production shutdown path without starting network integrations."""
    app = AppRuntime(
        Config(
            provider="openai",
            model="fake",
            api_key="fake",
            memory_optimizer_enabled=False,
        ),
        tmp_path,
        features=RuntimeFeatures(enable_message_channels=False, enable_proactive=False),
    )
    core = build_core_runtime(app.config, tmp_path, app.http_resources)
    app.core = core
    app.agent_loop = core.loop
    app.bus = core.bus
    app.scheduler = core.scheduler
    app.memory_runtime = core.memory_runtime
    app.session_manager = core.session_manager
    app._background_tasks = [
        asyncio.create_task(core.loop.run()),
        asyncio.create_task(core.bus.dispatch_outbound()),
        asyncio.create_task(core.scheduler.run()),
    ]
    await asyncio.sleep(0)
    try:
        yield app, core
    finally:
        await app.shutdown()


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
async def test_app_shutdown_joins_delivery_commit_after_runtime_loops_stop(app_runtime):
    app, runtime = app_runtime
    entered, release, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def sender(key, content):
        entered.set()
        await release.wait()
        delivered.set()
        return DeliveryReceipt(True, "desktop", key, "sent", "1")

    runtime.push_tool.register_channel("desktop", text=sender, pending_text=sender)
    scheduled = job("instant")
    runtime.scheduler.add_job(scheduled)
    await runtime.scheduler._tick()
    task = runtime.scheduler._active_tasks[scheduled.id]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        # AppRuntime.run's cancelled gather may already have stopped all loops.
        for background in app._background_tasks:
            background.cancel()
        await asyncio.gather(*app._background_tasks, return_exceptions=True)
        release.set()
        await delivered.wait()
        await app.shutdown()
        assert task.done()
        assert durable_contents(runtime, KEY) == ["定时提醒"]
        assert not runtime.scheduler._active_tasks
        assert not runtime.bus.chat_lane._states
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("recurring", [False, True])
@pytest.mark.parametrize("user_cancel", [False, True])
async def test_committed_cancelled_occurrence_is_not_replayed_or_resurrected(
    desktop, recurring, user_cancel
):
    runtime, _ = desktop
    entered, release = asyncio.Event(), asyncio.Event()
    now = datetime.now(timezone.utc)
    runtime.scheduler._now = lambda: now
    scheduled = job("instant")
    scheduled.fire_at = now - timedelta(seconds=1)
    if recurring:
        scheduled.trigger = "every"
        scheduled.interval_seconds = 60

    async def held_completion(event):
        entered.set()
        await release.wait()

    runtime.event_bus.on(ProactiveMessageCommitted, held_completion)
    runtime.scheduler.add_job(scheduled)
    await runtime.scheduler._tick()
    task = runtime.scheduler._active_tasks[scheduled.id]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert durable_contents(runtime, KEY) == ["定时提醒"]
        if user_cancel:
            assert runtime.scheduler.cancel_job(scheduled.id)
        else:
            runtime.scheduler.stop()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert scheduled.run_count == 1
        persisted = runtime.scheduler.store.load()
        if recurring and not user_cancel:
            assert len(persisted) == 1
            assert persisted[0].fire_at > now and persisted[0].run_count == 1
        else:
            assert persisted == []
        recovered = SchedulerService(
            runtime.scheduler.store.path,
            runtime.push_tool,
            _now_fn=lambda: now,
            chat_lane=runtime.bus.chat_lane,
        )
        recovered.load_and_recover()
        await recovered._tick()
        await asyncio.gather(*recovered._active_tasks.values())
        assert durable_contents(runtime, KEY) == ["定时提醒"]
        assert not runtime.bus.chat_lane._states
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        runtime.event_bus.off(ProactiveMessageCommitted, held_completion)


@pytest.mark.asyncio
async def test_soft_schedule_tools_use_original_target_and_ownership(desktop):
    runtime, _ = desktop
    existing = job("instant", name="existing")
    existing.fire_at += timedelta(hours=1)
    runtime.scheduler.add_job(existing)
    scheduled = job(
        thread_id="thread-7", session_config_version="v7", delivery_key="route-7"
    )
    calls = [
        ToolCall("list", "list_schedules", {}),
        ToolCall("cancel", "cancel_schedule", {"name": "existing"}),
        ToolCall(
            "create",
            "schedule",
            {
                "name": "child",
                "tier": "instant",
                "trigger": "after",
                "when": "1h",
                "message": "子提醒",
            },
        ),
    ]
    contexts, prompts, prompt_targets = [], [], []

    async def chat(**kwargs):
        contexts.append(runtime.tools.get_context())
        prompts.append(json.dumps(kwargs["messages"], ensure_ascii=False))
        prompt_lines = "\n".join(
            message["content"]
            for message in kwargs["messages"]
            if isinstance(message.get("content"), str)
        ).splitlines()
        channel = next(
            line.removeprefix("Channel: ")
            for line in prompt_lines
            if line.startswith("Channel: ")
        )
        chat_id = next(
            line.removeprefix("Chat ID: ")
            for line in prompt_lines
            if line.startswith("Chat ID: ")
        )
        prompt_targets.append((channel, chat_id))
        if calls:
            call = calls.pop(0)
            if call.name == "schedule":
                call.arguments.update(channel=channel, chat_id=chat_id)
            return LLMResponse("", tool_calls=[call])
        return LLMResponse("已处理提醒")

    runtime.provider.chat = chat
    await runtime.scheduler._execute(scheduled)
    assert "existing" in prompts[1]
    assert "没有找到" not in prompts[2]
    assert all(target == ("desktop", KEY) for target in prompt_targets)
    assert all(
        context["session_key"] == KEY
        and context["channel"] == "desktop"
        and context["chat_id"] == KEY
        for context in contexts
    )
    tasks = runtime.scheduler.list_jobs()
    assert len(tasks) == 1 and tasks[0].name == "child"
    assert tasks[0].session_key == KEY
    assert (tasks[0].channel, tasks[0].chat_id) == ("desktop", KEY)
    assert tasks[0].thread_id == "thread-7"
    assert tasks[0].session_config_version == "v7"
    assert tasks[0].delivery_key == "route-7"
    assert durable_contents(runtime, f"scheduler:{scheduled.id}") == []


@pytest.mark.asyncio
async def test_soft_background_spawn_returns_to_desktop_without_internal_history(
    desktop,
):
    runtime, bridge = desktop
    started, release, notified = asyncio.Event(), asyncio.Event(), asyncio.Event()
    contexts, notifications = [], []
    main_calls = 0

    async def chat(**kwargs):
        nonlocal main_calls
        current = asyncio.current_task()
        if current and current.get_name().startswith("spawn:"):
            started.set()
            await release.wait()
            return LLMResponse("后台研究结果")
        messages = json.dumps(kwargs["messages"], ensure_ascii=False)
        if "后台任务回传" in messages:
            contexts.append(runtime.tools.get_context())
            return LLMResponse("后台结果已完成")
        main_calls += 1
        if main_calls == 1:
            return LLMResponse(
                "",
                tool_calls=[
                    ToolCall(
                        "spawn",
                        "spawn",
                        {
                            "task": "任务目标：调研测试材料。关键约束：只读。期望输出格式：文本报告。",
                            "run_in_background": True,
                        },
                    )
                ],
            )
        return LLMResponse("已开始后台任务")

    def listener(event):
        if event["method"] == "message.pushed":
            notifications.append(event["payload"])
            if event["payload"]["content"] == "后台结果已完成":
                notified.set()

    runtime.provider.chat = chat
    bridge.add_event_listener(listener)
    runner = asyncio.create_task(runtime.loop.run())
    dispatcher = asyncio.create_task(runtime.bus.dispatch_outbound())
    scheduled = job()
    try:
        await runtime.scheduler._execute(scheduled)
        await asyncio.wait_for(started.wait(), 2)
        release.set()
        await asyncio.wait_for(notified.wait(), 2)
        assert notifications[-1]["session_key"] == KEY
        assert contexts[-1]["session_key"] == KEY
        assert durable_contents(runtime, f"scheduler:{scheduled.id}") == []
        assert durable_contents(runtime, f"desktop:{KEY}") == []
        assert durable_contents(runtime, KEY)[-1] == "后台结果已完成"
        assert runtime.session_manager.list_sessions()[0]["key"] == KEY
    finally:
        release.set()
        runner.cancel()
        dispatcher.cancel()
        await asyncio.gather(runner, dispatcher, return_exceptions=True)


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
        runtime.session_manager.get_or_create(KEY).messages[-1]["metadata"]["source"]
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
