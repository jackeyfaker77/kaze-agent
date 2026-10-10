"""真实队列、工具与发送窗口的优先级和取消边界。"""

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock

import pytest

from agent.tools.message_push import MessagePushTool
from bus.chat_lane import ChatLane
from bus.events import InboundMessage, OutboundMessage
from bus.queue import MessageBus


@pytest.mark.asyncio
async def test_queued_passive_reply_finishes_before_non_passive_send():
    bus = MessageBus()
    order = []
    started, release = asyncio.Event(), asyncio.Event()

    async def passive_send(message):
        started.set()
        await release.wait()
        order.append(message.content)

    async def proactive_send():
        order.append("主动")

    bus.subscribe_outbound("telegram", passive_send)
    inbound = InboundMessage(
        channel="telegram", chat_id="42", sender="user", content="你好"
    )
    await bus.publish_inbound(inbound)
    await bus.consume_inbound()
    push = asyncio.create_task(
        bus.chat_lane.run_non_passive("telegram", "42", proactive_send)
    )
    await asyncio.sleep(0)
    assert not push.done()
    await bus.publish_outbound(
        OutboundMessage(channel="telegram", chat_id="42", content="被动")
    )
    await bus.complete_inbound(inbound)
    dispatcher = asyncio.create_task(bus.dispatch_outbound())
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert not push.done()
        release.set()
        await asyncio.wait_for(push, 2)
        assert order == ["被动", "主动"]
        assert not bus.chat_lane._states
    finally:
        dispatcher.cancel()
        with suppress(asyncio.CancelledError):
            await dispatcher


@pytest.mark.asyncio
async def test_non_passive_fifo_skips_cancelled_middle_ticket():
    lane = ChatLane()
    order = []

    async def send(label):
        order.append(label)

    await lane.mark_passive_pending("desktop", "desktop:a")
    tasks = []
    for label in ["一", "取消", "三"]:
        tasks.append(
            asyncio.create_task(
                lane.run_non_passive(
                    "desktop", "desktop:a", lambda label=label: send(label)
                )
            )
        )
        await asyncio.sleep(0)
    tasks[1].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[1]
    await lane.mark_passive_done("desktop", "desktop:a")
    await asyncio.wait_for(asyncio.gather(tasks[0], tasks[2]), 2)
    assert order == ["一", "三"]
    assert not lane._states


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_send_releases_window(cancel):
    lane = ChatLane()
    started, release = asyncio.Event(), asyncio.Event()

    async def fail():
        started.set()
        await release.wait()
        raise RuntimeError("offline")

    first = asyncio.create_task(lane.run_non_passive("telegram", "42", fail))
    await asyncio.wait_for(started.wait(), 2)
    next_send = AsyncMock(return_value="成功")
    second = asyncio.create_task(lane.run_non_passive("telegram", "42", next_send))
    if cancel:
        first.cancel()
    else:
        release.set()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await first
    assert await asyncio.wait_for(second, 2) == "成功"
    next_send.assert_awaited_once()
    assert not lane._states


@pytest.mark.asyncio
async def test_cancelled_waiting_passive_send_does_not_clear_inflight_sender():
    lane = ChatLane()
    started, release = asyncio.Event(), asyncio.Event()

    async def send():
        started.set()
        await release.wait()

    first = asyncio.create_task(lane.run_non_passive("telegram", "42", send))
    await asyncio.wait_for(started.wait(), 2)
    await lane.mark_passive_send_pending("telegram", "42")
    unused = AsyncMock()
    passive = asyncio.create_task(lane.run_passive("telegram", "42", unused))
    await asyncio.sleep(0)
    passive.cancel()
    with pytest.raises(asyncio.CancelledError):
        await passive
    next_send = AsyncMock()
    second = asyncio.create_task(lane.run_non_passive("telegram", "42", next_send))
    await asyncio.sleep(0)
    next_send.assert_not_awaited()
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 2)
    unused.assert_not_awaited()
    assert not lane._states


@pytest.mark.asyncio
async def test_tool_send_inside_passive_turn_resolves_target_without_self_deadlock():
    lane = ChatLane()
    push = MessagePushTool(chat_lane=lane)
    sender = AsyncMock()
    push.register_channel("telegram", text=sender, target_resolver=lambda _: "42")
    external = AsyncMock()
    async with lane.passive_turn("telegram", "42"):
        blocked = asyncio.create_task(lane.run_non_passive("telegram", "42", external))
        receipt = await asyncio.wait_for(
            push.send(channel="telegram", chat_id="alias", message="工具回复"), 2
        )
        assert receipt.ok and receipt.chat_id == "42"
        sender.assert_awaited_once_with("42", "工具回复")
        external.assert_not_awaited()
    await asyncio.wait_for(blocked, 2)
    assert not lane._states


@pytest.mark.asyncio
async def test_outer_delivery_scope_groups_multipart_send_and_commit():
    lane = ChatLane()
    push = MessagePushTool(chat_lane=lane)
    order = []

    async def text(chat_id, content):
        order.append(content)

    async def image(chat_id, path):
        order.append(path)

    async def next_send():
        order.append("下一条")

    push.register_channel("telegram", text=text, image=image)
    async with lane.non_passive_send("telegram", "42"):
        waiter = asyncio.create_task(lane.run_non_passive("telegram", "42", next_send))
        await asyncio.wait_for(
            push.send(channel="telegram", chat_id="42", message="正文", image="一.png"),
            2,
        )
        await asyncio.wait_for(
            push.send(channel="telegram", chat_id="42", image="二.png"), 2
        )
        await asyncio.sleep(0)
        assert order == ["正文", "一.png", "二.png"]
        order.append("提交历史")
    await asyncio.wait_for(waiter, 2)
    assert order == ["正文", "一.png", "二.png", "提交历史", "下一条"]


@pytest.mark.asyncio
async def test_completed_parent_scope_cannot_bypass_new_passive_turn():
    lane = ChatLane()
    ready = asyncio.Event()
    sender = AsyncMock()

    async def inherited_task():
        await ready.wait()
        await lane.run_send("desktop", "desktop:a", sender)

    async with lane.passive_turn("desktop", "desktop:a"):
        child = asyncio.create_task(inherited_task())
    async with lane.passive_turn("desktop", "desktop:a"):
        ready.set()
        await asyncio.sleep(0)
        sender.assert_not_awaited()
    await asyncio.wait_for(child, 2)
    sender.assert_awaited_once()
    assert not lane._states


@pytest.mark.asyncio
async def test_other_target_is_independent_of_busy_lane():
    lane = ChatLane()
    sender = AsyncMock(return_value="成功")
    async with lane.passive_turn("telegram", "42"):
        assert (
            await asyncio.wait_for(lane.run_non_passive("telegram", "99", sender), 2)
            == "成功"
        )
    assert not lane._states


@pytest.mark.asyncio
async def test_user_tools_can_send_to_each_others_busy_targets():
    lane = ChatLane()
    push = MessagePushTool(chat_lane=lane)
    sender = AsyncMock()
    push.register_channel("desktop", text=sender)
    ready = asyncio.Event()
    admitted = 0

    async def user_turn(origin, target):
        nonlocal admitted
        async with lane.passive_turn("desktop", origin):
            admitted += 1
            if admitted == 2:
                ready.set()
            await ready.wait()
            return await push.send(channel="desktop", chat_id=target, message=origin)

    receipts = await asyncio.wait_for(
        asyncio.gather(
            user_turn("desktop:a", "desktop:b"),
            user_turn("desktop:b", "desktop:a"),
        ),
        2,
    )
    assert all(receipt.ok for receipt in receipts)
    assert sender.await_count == 2
    assert not lane._states
