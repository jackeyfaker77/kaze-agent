"""Release-boundary tests. All providers/transports are local doubles."""

from contextlib import contextmanager
from datetime import datetime
import json
from types import SimpleNamespace
from typing import Any, cast
from pathlib import Path

import pytest

from agent.tools.registry import ToolRegistry
from core.execution.scope import current_execution
from plugins.associative_notebook.core.contracts import NotebookError, encode
from plugins.associative_notebook.plugin import (
    AssociativeNotebookPlugin,
    NotebookConfig,
    NotebookTool,
)
from plugins.associative_notebook.service import NotebookService
from proactive_v2.mcp_sources import (
    SharedMcpGateway,
    fetch_source_strict_async,
    validate_alert_async,
)
from agent.plugins.specs import RegisteredProactiveSource
from session.store import SessionStore
from bus.durable import TransportStore, Outbox


NOW = "2026-10-31T10:00:00+00:00"
KEY = "desktop:owner"


class Provider:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.correct = False
        self.invalid = False

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise TimeoutError("uncertain")
        request = json.loads(kwargs["messages"][1]["content"])["input"]
        notes, findings, corrections = [], [], []
        if request["role"] == "exploration":
            for ref in request["source_refs"]:
                text = request["sources"][ref]["text"]
                topic = "interest" if "interest" in text else "activity"
                notes.append(
                    {
                        "topic": topic,
                        "title": topic,
                        "text": text,
                        "kind": "statement",
                        "evidence": [{"ref": ref, "quote": text}],
                        "related": [],
                        "occurred_at": None,
                    }
                )
                if self.correct:
                    target = next(
                        k
                        for k, n in request["notes"].items()
                        if n["topic"] == "activity"
                    )
                    corrections.append(
                        {
                            "source_ref": ref,
                            "target_id": target,
                            "replacement_index": len(notes) - 1,
                            "action": "cancel_occurrence",
                        }
                    )
        elif request["notes"]:
            target = next(iter(request["notes"]))
            findings = [
                {
                    "topic": request["notes"][target]["topic"],
                    "text": "A conditional suggestion",
                    "note_ids": ["bad-id" if self.invalid else target],
                    "expires_at": "2026-11-01T10:00:00+00:00",
                }
            ]
        return SimpleNamespace(
            content=encode(
                {
                    "draft": {
                        "notes": notes,
                        "findings": findings,
                        "withheld": [],
                        "skipped": {},
                    },
                    "corrections": corrections,
                }
            )
        )


@pytest.fixture
def rig(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    provider = Provider()
    clock = [NOW]
    services = []

    def open_service():
        service = NotebookService(
            directory=tmp_path / "notebook",
            session_key=KEY,
            sessions=SimpleNamespace(_store=store),
            provider=provider,
            model="offline-fake",
            clock=lambda: clock[0],
        )
        # The real provider clone shares its network client, but not retry config.
        service.provider = provider
        services.append(service)
        return service

    def add(text, *, key=KEY, metadata=None):
        return store.insert_message(
            key,
            role="user",
            content=text,
            ts=NOW,
            seq=store.next_seq(key),
            extra={"metadata": metadata or {}},
        )

    service = open_service()
    yield SimpleNamespace(
        service=service,
        provider=provider,
        clock=clock,
        store=store,
        add=add,
        reopen=open_service,
        path=tmp_path,
    )
    for service in services:
        service.close()
    store.close()


@contextmanager
def scope(key=KEY, proactive=True):
    value = SimpleNamespace(
        active=True,
        broken=False,
        review_required=False,
        proactive=proactive,
        turn_id="test",
        journal=SimpleNamespace(get=lambda _: {"session_key": key}),
    )
    token = current_execution.set(cast(Any, value))
    try:
        yield value
    finally:
        current_execution.reset(token)


@pytest.mark.asyncio
async def test_disabled_plugin_has_no_storage_tools_or_provider_calls(tmp_path):
    plugin = AssociativeNotebookPlugin()
    registry = ToolRegistry()
    plugin.context = cast(
        Any,
        SimpleNamespace(
            config=NotebookConfig(), workspace=tmp_path, tool_registry=registry
        ),
    )
    await plugin.initialize()
    assert plugin.proactive_sources() == []
    assert registry.get_tool("notebook_read") is None
    assert not list(tmp_path.iterdir())
    await plugin.terminate()
    with pytest.raises(ValueError, match="session_key"):
        NotebookConfig(enabled=True)


@pytest.mark.asyncio
async def test_real_canonical_materials_two_phase_and_no_poll_relearning(rig):
    rig.add("I have an activity")
    rig.add("Other owner secret", key="desktop:other")
    # A UI cache never enters the adapter; the canonical event snapshot is read.
    rig.service.sessions._cache = {
        KEY: SimpleNamespace(messages=[{"role": "user", "content": "fabricated"}])
    }
    candidates = await rig.service.refresh()
    assert len(candidates) == 1
    assert [s["text"] for s in rig.service.book.sources().values()] == [
        "I have an activity"
    ]
    assert len(rig.provider.calls) == 2
    assert all(c["tools"] == [] and c["max_tokens"] == 4096 for c in rig.provider.calls)
    assert await rig.service.refresh() == candidates
    assert rig.reopen().read() == rig.service.read()
    assert len(rig.provider.calls) == 2


@pytest.mark.asyncio
async def test_new_input_withdraws_cached_candidate_and_correction_preserves_interest(
    rig,
):
    rig.add("My interest continues")
    rig.add("I have an activity")
    await rig.service.refresh()
    before = rig.service.book.active_notes()
    interest = {k: n for k, n in before.items() if n["topic"] == "interest"}
    cached = rig.service.candidates()
    rig.add("Cancel this activity")
    assert rig.service.candidates() == []
    assert rig.service.read()["notes"] == []
    rig.provider.correct = True
    await rig.service.refresh()
    after = rig.service.book.active_notes()
    assert all(after[k] == n for k, n in interest.items())
    assert not any(n["text"] == "I have an activity" for n in after.values())
    assert not {c["id"] for c in cached} & {c["id"] for c in rig.service.candidates()}
    assert len(rig.service.book.events("correction")) == 1
    assert len(rig.service.book.coverage()) == 3
    assert rig.reopen().read() == rig.service.read()


@pytest.mark.asyncio
async def test_unknown_response_and_invalid_reference_stop_without_retry_after_reopen(
    rig,
):
    rig.add("activity")
    rig.provider.fail = True
    with pytest.raises(TimeoutError):
        await rig.service.refresh()
    rig.provider.fail = False
    with pytest.raises(NotebookError, match="Unknown model outcome"):
        await rig.reopen().refresh()
    assert len(rig.provider.calls) == 1
    assert rig.service.candidates() == []


@pytest.mark.asyncio
async def test_invalid_draft_preserved_and_no_repeated_model_call(rig):
    rig.add("activity")
    rig.provider.invalid = True
    with pytest.raises(NotebookError, match="unknown notes"):
        await rig.service.refresh()
    response = rig.service.book.events("response")[-1]
    with pytest.raises(NotebookError):
        await rig.reopen().refresh()
    assert rig.service.book.events("response")[-1] == response
    assert len(rig.provider.calls) == 2
    assert rig.service.candidates() == []


@pytest.mark.asyncio
async def test_restart_between_exploration_and_judgement_no_duplicate_learning(rig):
    rig.add("activity")
    original = rig.service.phase

    async def crash_at_judgement(run_id, role, refs, rows):
        if role == "judgement":
            raise RuntimeError("restart boundary")
        await original(run_id, role, refs, rows)

    rig.service.phase = crash_at_judgement
    with pytest.raises(RuntimeError):
        await rig.service.refresh()
    assert len(rig.provider.calls) == 1
    recovered = rig.reopen()
    assert len(await recovered.refresh()) == 1
    assert len(rig.provider.calls) == 2
    assert len(recovered.book.events("note")) == 1


@pytest.mark.asyncio
async def test_reply_quotes_are_not_admitted_as_user_facts(rig):
    rig.add(
        "quoted assistant promise + current statement",
        metadata={"reply_to_message_id": 5, "user_text": "current statement"},
    )
    rig.add("legacy merged unknown", metadata={"reply_to_message_id": 6})
    await rig.service.refresh()
    assert [s["text"] for s in rig.service.book.sources().values()] == [
        "current statement"
    ]


@pytest.mark.asyncio
async def test_real_plugin_gateway_scope_and_expiry(rig):
    registry = ToolRegistry()
    plugin = AssociativeNotebookPlugin()
    plugin.context = cast(
        Any,
        SimpleNamespace(
            config=NotebookConfig(enabled=True, session_key=KEY),
            workspace=rig.path,
            session_manager=rig.service.sessions,
            light_provider=rig.provider,
            light_model="offline-fake",
            tool_registry=registry,
        ),
    )
    await plugin.initialize()
    try:
        rig.add("activity")
        service = plugin.service
        assert service is not None
        service.clock = lambda: rig.clock[0]
        service.book.clock = service.clock
        service.provider = rig.provider
        gateway = SharedMcpGateway(rig.path, registry)
        source = RegisteredProactiveSource(
            "associative_notebook", plugin.proactive_sources()[0]
        )
        # ToolRegistry execution logging needs a real journal, so exercise native
        # lookup with its normal no-scope path and scope enforcement separately.
        with pytest.raises(NotebookError, match="recorded execution"):
            await registry.get_tool("notebook_read").execute()
        with scope("desktop:other"):
            with pytest.raises(NotebookError, match="scope mismatch"):
                await registry.get_tool("notebook_read").execute(session_key=KEY)
        with scope():
            # Gateway uses registry.execute, bypass only its execution journaling
            # in this test; native source lookup and JSON contracts remain real.
            async def execute(name, args, **kwargs):
                return await registry.get_tool(name).execute(**args)

            registry.execute = execute
            channels = await fetch_source_strict_async(gateway, source)
            event = channels["content"][0]
            assert (
                event["requires_validation"]
                and event["ack_server"] == "associative_notebook:notebook"
            )
            assert (
                await validate_alert_async(
                    gateway, [source], event, now=datetime.fromisoformat(NOW)
                )
                == "valid"
            )
            rig.clock[0] = "2026-11-02T10:00:00+00:00"
            assert (
                await validate_alert_async(
                    gateway, [source], event, now=datetime.fromisoformat(rig.clock[0])
                )
                == "invalid"
            )
    finally:
        await plugin.terminate()
    assert registry.get_tool("notebook_read") is None


@pytest.mark.asyncio
async def test_drift_read_composes_existing_host_guard_and_revalidates_before_send(rig):
    rig.add("activity")
    await rig.service.refresh()
    tool = NotebookTool(rig.service)
    with scope() as execution:
        checks = []

        async def existing_guard():
            checks.append(True)
            return True

        execution.proactive_host_guard = existing_guard
        await tool.execute()
        assert await execution.proactive_host_guard()
        rig.add("Cancel activity")
        assert not await execution.proactive_host_guard()
        assert len(checks) == 2


@pytest.mark.asyncio
async def test_real_outbox_confirmed_part_attribution_not_aggregate_job(rig):
    rig.add("activity")
    candidate = (await rig.service.refresh())[0]
    transport = TransportStore(rig.path)
    try:
        outbox = Outbox(transport, None)
        rig.service.outbox = outbox
        payload = {
            "channel": "telegram",
            "chat_id": "42",
            "_outbound_metadata": {
                "session_key_override": KEY,
                "_delivery_projection": encode(
                    {"evidence": ["associative_notebook:notebook:" + candidate["id"]]}
                ),
            },
        }
        job = outbox.enqueue("push:telegram", payload, "delivery-test")
        with transport.db:
            transport.db.execute(
                "INSERT INTO parts VALUES (?,?,?,'done',?)",
                (
                    job,
                    "0:text:0",
                    encode({"chat_id": "42", "text": "sent"}),
                    encode({"message_id": "99"}),
                ),
            )
        rig.add(
            "Cancel activity",
            metadata={
                "transport_channel": "telegram",
                "transport_chat_id": "42",
                "reply_to_message_id": 99,
                "user_text": "Cancel activity",
            },
        )
        _, rows = rig.service.materials()
        receipts, replies = rig.service.receipts(rows)
        assert receipts[job]["message_id"] == "99"
        assert list(replies.values()) == [job]
        assert transport.get(job)["state"] != "done"
        rig.provider.correct = True
        await rig.service.refresh()
        assert (
            rig.service.book.events("feedback_resolution")[0]["payload"]["attribution"]
            == "delivery_feedback"
        )
        # Same platform message id in another chat is not attribution evidence.
        for row in rows.values():
            row["metadata"]["transport_chat_id"] = "different"
        assert not rig.service.receipts(rows)[0]
    finally:
        transport.close()


@pytest.mark.asyncio
async def test_real_outbox_final_guard_rejects_change_after_enqueue(rig):
    from bus.durable import DeliveryStopped
    from core.execution.journal import ExecutionJournal
    from core.execution.proactive import run_proactive_recorded

    rig.add("activity")
    await rig.service.refresh()
    transport = TransportStore(rig.path)
    journal = ExecutionJournal(rig.path)
    outbox = Outbox(transport, None)
    sent = []

    async def physical():
        sent.append("sent")
        return "external-id"

    async def handler(payload):
        rig.add("Cancel activity after queue admission")
        return await outbox.part("text", {"body": "cached"}, physical)

    async def operation():
        await NotebookTool(rig.service).execute()
        outbox.handlers["push:telegram"] = handler
        key = outbox.enqueue("push:telegram", {"channel": "telegram", "chat_id": "42"})
        with pytest.raises(DeliveryStopped, match="proactive_revalidation_failed"):
            await outbox.deliver(key)
        return 0

    try:
        await run_proactive_recorded(journal, KEY, "queue-test", {}, operation)
        assert not sent
        assert not transport.db.execute(
            "SELECT 1 FROM parts WHERE state IN ('sending','done')"
        ).fetchone()
    finally:
        transport.close()
        journal.close()


@pytest.mark.asyncio
async def test_wake_cached_content_guard_survives_reservoir_roundtrip(rig):
    from tests.backend.proactive_v2.akashic_wake.test_runtime import (
        _scope,
        FixedClock,
        FailingOrchestrator,
    )
    from plugins.wake_proactive.runtime import WakeRuntime
    from plugins.wake_proactive.state import WakeStateStore
    from proactive_v2.frame import new_proactive_frame
    from agent.plugins.specs import ProactiveSourceSpec
    from unittest.mock import AsyncMock

    rig.add("activity")
    candidate = (await rig.service.refresh())[0]
    spec = ProactiveSourceSpec(
        id="notebook",
        channels=("content",),
        server="plugin:associative_notebook",
        fetch_tool="notebook_fetch",
        validate_tool="notebook_validate",
    )
    registered = RegisteredProactiveSource("associative_notebook", spec)

    async def call(server, tool_name, args, **kwargs):
        ids = {c["id"] for c in rig.service.candidates()}
        return {"valid_event_ids": [i for i in args["event_ids"] if i in ids]}

    gateway = SimpleNamespace(call=call)
    orchestrator = FailingOrchestrator()
    runtime_scope = _scope(
        rig.path, gateway, SimpleNamespace(chat=AsyncMock()), orchestrator, registered
    )
    clock = FixedClock(datetime.fromisoformat(NOW))
    db = rig.path / "wake.db"
    store = WakeStateStore(db)
    store.ingest(
        "content",
        [{**candidate, "ack_server": "associative_notebook:notebook"}],
        clock.now(),
    )
    store.close()
    runtime = WakeRuntime(runtime_scope, state_store=WakeStateStore(db), clock=clock)
    try:
        state = runtime.begin(new_proactive_frame(KEY))
        state.contents = await runtime._current_contents()
        assert len(state.contents) == 1
        state.ctx.cited_item_ids = [state.contents[0]["id"]]
        state.ctx.final_message = "cached suggestion"
        state.ctx.terminal_action = "reply"
        await runtime._commit_content_decision(state)
        guard = orchestrator.results[-1].delivery_guard
        assert await guard()
        rig.add("Cancel activity while queued")
        assert not await guard()
        assert await runtime._current_contents() == []
    finally:
        runtime.close()


@pytest.mark.asyncio
async def test_host_loader_and_recorded_native_source_entrypoint(rig):
    from agent.plugins.manager import PluginManager
    from bus.event_bus import EventBus
    from core.execution.journal import ExecutionJournal
    from core.execution.proactive import run_proactive_recorded

    registry = ToolRegistry()
    plugin_root = Path(__file__).resolve().parents[3] / "apps/backend/plugins"
    manager = PluginManager(
        [plugin_root],
        event_bus=EventBus(),
        tool_registry=registry,
        workspace=rig.path,
        session_manager=rig.service.sessions,
        light_provider=rig.provider,
        light_model="offline-fake",
        plugin_configs={"associative_notebook": {"enabled": True, "session_key": KEY}},
    )
    journal = ExecutionJournal(rig.path)
    try:
        descriptor = next(
            d for d in manager.discover() if d["name"] == "associative_notebook"
        )
        await manager._load_one(descriptor)
        assert manager.loaded_count == 1 and len(manager.proactive_sources) == 1
        rig.add("activity")
        # Keep generated expiry relative to the test's deterministic clock.
        tool = cast(Any, registry.get_tool("notebook_read"))
        tool.service.book.clock = lambda: NOW
        gateway = SharedMcpGateway(rig.path, registry)

        async def operation():
            channels = await fetch_source_strict_async(
                gateway, manager.proactive_sources[0]
            )
            assert len(channels["content"]) == 1
            return 0

        await run_proactive_recorded(journal, KEY, "native-entry", {}, operation)
        record = journal.find(KEY, "proactive", "native-entry")
        assert record["status"] == "completed"
        assert any(e["kind"] == "tool" for e in record["effects"])
        assert len(rig.provider.calls) == 2
    finally:
        await manager.terminate_all()
        journal.close()
    assert registry.get_tool("notebook_read") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", ["content", "attribution"])
async def test_projection_rewrite_never_backfills_or_invalidates_unchanged_facts(
    rig, edit
):
    rig.add("old interest before activation")
    service = NotebookService(
        directory=rig.path / "late-book",
        session_key=KEY,
        sessions=rig.service.sessions,
        provider=rig.provider,
        model="offline-fake",
        clock=lambda: NOW,
    )
    service.provider = rig.provider

    def rewrite():
        rows = rig.store.fetch_session_messages(KEY)
        rig.store.replace_session_messages(
            KEY,
            rows=[
                {**row, "extra": {"metadata": row.get("metadata", {})}} for row in rows
            ],
            updated_at=NOW,
            last_consolidated=0,
            next_seq=rig.store.next_seq(KEY),
        )

    try:
        rewrite()
        assert service.materials()[0] == {}
        assert await service.refresh() == []
        assert not rig.provider.calls
        new = rig.add("new activity after activation")
        await service.refresh()
        before = service.read()
        sources = service.book.sources()
        rig.store.update_message(
            new["id"], extra={"metadata": {"display_label": "UI-only change"}}
        )
        rewrite()
        assert service.read() == before
        assert service.materials()[0] == sources
        await service.refresh()
        assert len(rig.provider.calls) == 2
        if edit == "content":
            rig.store.update_message(new["id"], content="different activity")
        else:
            rig.store.update_message(
                new["id"],
                extra={
                    "metadata": {
                        "transport_channel": "telegram",
                        "transport_chat_id": "different",
                    }
                },
            )
        with pytest.raises(NotebookError, match="edited or deleted"):
            service.read()
        assert len(rig.provider.calls) == 2
    finally:
        service.close()
