from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from bus.chat_lane import ChatLane
from bus.durable import TransportStore, Outbox, RetryDelivery, DeliveryStopped
from bus.events import InboundMessage, OutboundMessage
from bus.queue import MessageBus
from agent.tools.message_push import MessagePushTool
from core.execution.journal import ExecutionJournal


@pytest.fixture
def queue(tmp_path):
    store = TransportStore(tmp_path)
    box = Outbox(store, ChatLane())
    yield box
    store.close()


@pytest.mark.asyncio
async def test_outbox_retry_skips_confirmed_parts_and_preserves_receipt(queue):
    calls = []

    async def handler(payload):
        async def first():
            calls.append("first")
            return SimpleNamespace(message_id=1)

        async def second():
            calls.append("second")
            if calls.count("second") == 1:
                raise RetryDelivery(0)
            return SimpleNamespace(message_id=2)

        one = await queue.part("1", {"text": "one"}, first)
        two = await queue.part("2", {"text": "two"}, second)
        return {"ids": [str(one.message_id), str(two.message_id)]}

    queue.handlers["test"] = handler
    key = queue.enqueue("test", {"channel": "test", "chat_id": "1"}, "stable")
    assert await queue.deliver(key) == {"ids": ["1", "2"]}
    assert await queue.deliver(key) == {"ids": ["1", "2"]}
    assert calls == ["first", "second", "second"]
    assert queue.store.get(key)["attempts"] == 2
    with pytest.raises(ValueError, match="different content"):
        queue.enqueue("test", {"channel": "test", "chat_id": "2"}, "stable")


@pytest.mark.asyncio
async def test_uncertain_send_is_not_retried_or_replaced_with_error_message(queue):
    calls = []

    async def handler(payload):
        async def physical():
            calls.append("server accepted, receipt lost")
            raise TimeoutError()

        await queue.part("text", payload, physical)

    queue.handlers["test"] = handler
    key = queue.enqueue("test", {"channel": "test", "chat_id": "1"})
    with pytest.raises(TimeoutError):
        await queue.deliver(key)
    assert queue.store.get(key)["state"] == "needs_review"
    with pytest.raises(DeliveryStopped):
        await queue.deliver(key)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_retry_budget_and_account_change_are_fenced(queue, monkeypatch):
    count = 0
    original = queue.store.state

    def immediate(key, state, **kwargs):
        return original(key, state, **{**kwargs, "delay": 0})

    monkeypatch.setattr(queue.store, "state", immediate)

    async def handler(payload):
        async def physical():
            nonlocal count
            count += 1
            raise RetryDelivery(0)

        await queue.part("text", payload, physical)

    queue.handlers["test"] = handler
    queue.accounts["telegram"] = "bot1"
    key = queue.enqueue("test", {"channel": "telegram", "chat_id": "1"})
    with pytest.raises(DeliveryStopped):
        await queue.deliver(key)
    assert count == 5 and queue.store.get(key)["state"] == "failed"
    key = queue.enqueue("test", {"channel": "telegram", "chat_id": "2"})
    queue.accounts["telegram"] = "bot2"
    with pytest.raises(DeliveryStopped):
        await queue.deliver(key)
    assert count == 5 and queue.store.get(key)["state"] == "needs_review"


@pytest.mark.asyncio
async def test_receipt_saved_but_projection_failed_retries_without_resending(queue):
    calls = 0
    projects = 0

    async def handler(payload):
        async def physical():
            nonlocal calls
            calls += 1
            return SimpleNamespace(message_id=1)

        await queue.part("text", payload, physical)
        nonlocal projects
        projects += 1
        if projects == 1:
            raise OSError("conversation database temporarily unavailable")
        return {"ok": True}

    queue.handlers["test"] = handler
    key = queue.enqueue("test", {"channel": "fake", "chat_id": "1"})
    with pytest.raises(OSError):
        await queue.deliver(key)
    assert queue.store.get(key)["state"] == "pending"
    assert await queue.deliver(key) == {"ok": True}
    assert calls == 1 and projects == 2


@pytest.mark.asyncio
async def test_cancel_before_send_can_resume_after_send_requires_review(queue):
    started = asyncio.Event()

    async def handler(payload):
        async def physical():
            started.set()
            await asyncio.Future()

        await queue.part("text", payload, physical)

    queue.handlers["test"] = handler
    key = queue.enqueue("test", {"channel": "test", "chat_id": "1"})
    task = asyncio.create_task(queue.deliver(key))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert queue.store.get(key)["state"] == "needs_review"


@pytest.mark.asyncio
async def test_push_and_bus_share_persistent_outbox_and_snapshot_files(tmp_path):
    journal = ExecutionJournal(tmp_path)
    bus = MessageBus()
    bus.enable_durability(tmp_path, journal)
    push = MessagePushTool(chat_lane=bus.chat_lane)
    push.outbox = bus.outbox
    calls = []

    async def text(chat, content):
        calls.append(content)
        return SimpleNamespace(message_id=123)

    async def file(chat, path, name):
        assert name == "source.txt"
        calls.append(Path(path).read_text())
        return SimpleNamespace(message_id=124)

    push.register_channel("fake", text=text, file=file)

    async def passive(msg):
        calls.append(msg.content)

    bus.subscribe_outbound("fake", passive)
    try:
        source = tmp_path / "source.txt"
        source.write_text("attachment")
        result = await push.send(
            channel="fake",
            chat_id="1",
            message="reminder",
            file=str(source),
            delivery_id="reminder-1",
        )
        assert result.ok and result.message_id == "123"
        source.unlink()
        payload = bus.transport.get("reminder-1")["payload"]
        assert Path(payload["file"]).read_text() == "attachment"
        await bus.publish_outbound(OutboundMessage("fake", "1", "reply"))
        rows = bus.transport.pending("bus:fake")
        assert len(rows) == 1
        await bus.outbox.deliver(rows[0]["id"])
        assert calls == ["reminder", "attachment", "reply"]
        assert {
            r[0] for r in bus.transport.db.execute("SELECT DISTINCT kind FROM jobs")
        } == {"push:fake", "bus:fake"}
    finally:
        bus.transport.close()
        journal.close()


@pytest.mark.asyncio
async def test_inbox_survives_restart_and_repeated_update_only_admits_once(tmp_path):
    journal = ExecutionJournal(tmp_path)
    bus = MessageBus()
    bus.enable_durability(tmp_path, journal)
    message = InboundMessage(
        "telegram",
        "1",
        "1",
        "hello",
        metadata={"_transport_id": "in:1", "execution_request_id": "in:1"},
    )
    await bus.publish_inbound(message)
    await bus.publish_inbound(message)
    assert len(bus.transport.pending("inbound")) == 1
    bus.transport.close()
    bus = MessageBus()
    bus.enable_durability(tmp_path, journal)
    try:
        recovered = await asyncio.wait_for(bus.consume_inbound(), 1)
        assert recovered.content == "hello"
        # An interrupted recorded execution requires review, never automatic rerun.
        record, _ = journal.admit(message.session_key, message.channel, "in:1", {})
        journal.finish(record["turn_id"], "interrupted")
        await bus.complete_inbound(recovered)
        assert bus.transport.get("in:1")["state"] == "needs_review"
        await bus.publish_inbound(message)
        assert bus.transport.pending("inbound") == []
    finally:
        bus.transport.close()
        journal.close()


@pytest.mark.parametrize(
    "stage,expected_calls,expected_state",
    [
        ("queued", 1, "done"),
        ("sending", 1, "needs_review"),
        ("receipt", 1, "done"),
        ("done", 1, "done"),
    ],
)
def test_hard_process_exit_recovery(tmp_path, stage, expected_calls, expected_state):
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(root / "apps/backend")}
    probe = root / "tests/e2e/transport_crash_backend.py"
    crashed = subprocess.run(
        [sys.executable, str(probe), str(tmp_path), stage],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert crashed.returncode == 73, crashed.stderr
    resumed = subprocess.run(
        [sys.executable, str(probe), str(tmp_path), "recover"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["state"] == expected_state
    assert len((tmp_path / "wire.jsonl").read_text().splitlines()) == expected_calls
    again = subprocess.run(
        [sys.executable, str(probe), str(tmp_path), "recover"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert again.returncode == 0, again.stderr
    assert len((tmp_path / "wire.jsonl").read_text().splitlines()) == expected_calls


def test_sdk_ingress_and_segmented_delivery(tmp_path):
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(root / "apps/backend")}
    result = subprocess.run(
        [sys.executable, str(root / "tests/e2e/transport_ingress.py"), str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_completed_input_waits_for_missing_reply_intent(tmp_path):
    journal = ExecutionJournal(tmp_path)
    bus = MessageBus()
    bus.enable_durability(tmp_path, journal)
    message = InboundMessage('telegram', '1', '1', 'hello', metadata={
        '_transport_id': 'in:completed', 'execution_request_id': 'in:completed',
    })
    reply = OutboundMessage('telegram', '1', 'saved result', metadata={'_transport_id':'in:completed'})
    row, _ = journal.admit(message.session_key, 'telegram', 'in:completed', {})
    journal.finish(row['turn_id'], 'completed', asdict(reply))
    try:
        await bus.publish_inbound(message)
        await bus.complete_inbound(await bus.consume_inbound())
        assert bus.transport.get('in:completed')['state'] == 'pending'
        bus.transport.state('in:completed', 'pending')
        recovered = await bus.consume_inbound()
        await bus.publish_outbound(reply)
        await bus.complete_inbound(recovered)
        assert bus.transport.get('in:completed')['state'] == 'done'
        assert len(bus.transport.pending('bus:telegram')) == 1
    finally:
        bus.transport.close()
        journal.close()


@pytest.mark.asyncio
async def test_confirmed_retry_does_not_poison_execution_journal(queue, tmp_path):
    from core.execution.runner import run_recorded

    journal = ExecutionJournal(tmp_path)
    push = MessagePushTool(chat_lane=queue.chat_lane)
    push.outbox, push.execution_journal = queue, journal
    calls = 0

    async def sender(chat, content):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RetryDelivery(0)
        return SimpleNamespace(message_id=7)

    push.register_channel("fake", text=sender)

    async def operation():
        receipt = await push.send(channel="fake", chat_id="1", message="hello")
        assert receipt.ok
        return OutboundMessage("fake", "1", "done")

    try:
        item = InboundMessage(
            "fake", "1", "1", "send", metadata={"execution_request_id": "retry"}
        )
        await run_recorded(journal, item, item.session_key, operation)
        assert calls == 2
        row = journal.find(item.session_key, "fake", "retry")
        assert row["status"] == "completed"
        events = journal.events(row["turn_id"])["events"]
        returned = [event for event in events if event["kind"] == "delivery.returned"]
        assert [event["payload"]["ok"] for event in returned] == [False, True]
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_delayed_receipt_updates_original_reply_only(tmp_path):
    from session.manager import SessionManager
    from core.channels.hub import ChannelHub

    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("telegram:1")
    try:
        session.add_message("assistant", "first")
        await sessions.append_messages(session, session.messages[-1:])
        first = session.messages[-1]["id"]
        session.add_message("assistant", "second")
        await sessions.append_messages(session, session.messages[-1:])
        hub = ChannelHub(sessions)
        hub.mark_delivery(
            OutboundMessage(
                "telegram", "1", "first", metadata={"_delivery_message_id": first}
            ),
            default_channel="telegram",
            delivery_status="sent",
            external_message_id="7",
        )
        rows = sessions._store.fetch_session_messages(session.key)
        assert rows[0]["external_message_id"] == "7"
        assert not rows[1].get("external_message_id")
        hub.mark_delivery(
            OutboundMessage(
                "telegram", "1", "deleted", metadata={"_delivery_message_id": "missing"}
            ),
            default_channel="telegram",
            delivery_status="sent",
            external_message_id="8",
        )
        assert not sessions._store.fetch_session_messages(session.key)[1].get(
            "external_message_id"
        )
    finally:
        sessions._store.close()
