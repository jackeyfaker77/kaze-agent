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
from agent.tools.forget_memory import ForgetMemoryTool
from core.memory.engine import MemoryToolSpec
from memory2.store import MemoryStore2
from plugins.default_memory.engine import DefaultMemoryEngine
from bus.durable import TransportStore, Outbox


NOW = "2026-10-31T10:00:00+00:00"
KEY = "desktop:owner"


@pytest.mark.asyncio
async def test_unified_forget_retires_both_stores_without_unrelated_loss(rig):
    rig.add("activity")
    await rig.service.refresh()
    rig.add("interest")
    await rig.service.refresh()
    with correction_memory(rig.path) as (store, engine, tool):
        memory_id = store.upsert_item("profile", "activity", None).split(":")[1]
        unrelated = store.upsert_item("preference", "interest", None).split(":")[1]
        registry = ToolRegistry()
        registry.register(tool)
        plugin = AssociativeNotebookPlugin()
        plugin.context = cast(Any, SimpleNamespace(
            config=NotebookConfig(enabled=True, session_key=KEY), workspace=rig.path,
            session_manager=rig.service.sessions, light_provider=rig.provider,
            light_model="fake", tool_registry=registry, memory_engine=engine))
        import plugins.associative_notebook.plugin as plugin_module
        from unittest.mock import patch
        with patch.object(plugin_module, "NotebookService", return_value=rig.service):
            await plugin.initialize()
        try:
            target = rig.service.read("activity")["notes"][0]
            old_ref = target["evidence"][0]["ref"]
            rig.add("that activity was wrong")
            assert not rig.service.read()["ready"]
            assert rig.service.read()["correction_targets"] == []
            assert target["forget_id"] in [n["forget_id"] for n in rig.service.read(for_correction=True)["correction_targets"]]
            with scope(), pytest.raises(NotebookError, match="Proactive consumption"):
                await NotebookTool(rig.service).execute(for_correction=True)
            with scope(proactive=False):
                result = json.loads(await tool.execute(ids=[memory_id, target["forget_id"]], session_key=KEY))
            assert set(result["superseded_ids"]) == {memory_id, target["forget_id"]}
            assert result["missing_ids"] == []
            assert store.get_item_for_admin(memory_id)["status"] == "superseded"
            assert store.get_item_for_admin(unrelated)["status"] == "active"
            assert target["id"] not in rig.service.book.active_notes()
            assert any(n["text"] == "interest" for n in rig.service.book.active_notes().values())
            assert not rig.service.book.preview()
            from plugins.associative_notebook.core.contracts import Note
            with pytest.raises(NotebookError, match="Corrected evidence"):
                rig.service.book._validate_note(Note(topic="activity", title="activity", text="reworded",
                    kind="statement", evidence=[{"ref": old_ref, "quote": "activity"}], related=[], occurred_at=None),
                    {old_ref}, {"activity", "interest"})
            assert store.upsert_item("profile", "activity", None) == "skipped:" + memory_id
            with scope(proactive=False):
                assert json.loads(await tool.execute(ids=[target["forget_id"], memory_id], session_key=KEY))[
                    "superseded_ids"] == result["superseded_ids"]
        finally:
            await plugin.terminate()
        assert tool.notebook is None


@pytest.mark.asyncio
@pytest.mark.parametrize("after_write", [False, True])
async def test_unified_forget_recovers_interrupted_memory_write_after_reopen(rig, after_write):
    rig.add("activity")
    await rig.service.refresh()
    with correction_memory(rig.path) as (store, engine, tool):
        memory_id = store.upsert_item("profile", "activity", None).split(":")[1]
        target = rig.service.read()["notes"][0]["forget_id"]
        tool.notebook = rig.service
        original = engine.mutate
        async def fail(request):
            if after_write:
                await original(request)
            raise OSError("interrupted")
        engine.mutate = fail
        with scope(proactive=False), pytest.raises(OSError, match="interrupted"):
            await tool.execute(ids=[memory_id, target], session_key=KEY)
        assert not rig.service.read()["ready"] and not rig.service.candidates()
        reopened = rig.reopen()
        assert not reopened.ready()
        engine.mutate = original
        reopened.memory_engine = engine
        await reopened.refresh()
        assert store.get_item_for_admin(memory_id)["status"] == "superseded"
        assert not reopened.book.active_notes() and not reopened.pending_corrections()
        assert len(reopened.book.events("forget_completed")) == 1
        await reopened.refresh()
        assert len(reopened.book.events("forget_completed")) == 1


@pytest.mark.asyncio
async def test_unified_forget_rejects_foreign_execution_and_reports_missing_targets(rig):
    rig.add("activity")
    await rig.service.refresh()
    with correction_memory(rig.path) as (store, engine, tool):
        memory_id = store.upsert_item("profile", "activity", None).split(":")[1]
        target = rig.service.read()["notes"][0]["forget_id"]
        tool.notebook = rig.service
        with scope(key="desktop:other", proactive=False), pytest.raises(NotebookError, match="recorded"):
            await tool.execute(ids=[memory_id, target], session_key=KEY)
        with scope(), pytest.raises(NotebookError, match="recorded"):
            await tool.execute(ids=[memory_id, target], session_key=KEY)
        assert store.get_item_for_admin(memory_id)["status"] == "active"
        assert not rig.service.book.events("correction")
        with scope(proactive=False):
            result = json.loads(await tool.execute(ids=["missing", "notebook:missing"], session_key=KEY))
        assert result["superseded_ids"] == []
        assert set(result["missing_ids"]) == {"missing", "notebook:missing"}


@contextmanager
def correction_memory(path):
    store = MemoryStore2(path / "memory2.db")
    engine = DefaultMemoryEngine.__new__(DefaultMemoryEngine)
    engine._v2_store = store
    tool = ForgetMemoryTool(engine, MemoryToolSpec(description="forget", parameters=ForgetMemoryTool.parameters))
    try:
        yield store, engine, tool
    finally:
        store.close()


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
    assert all("extra_body" not in c for c in rig.provider.calls)
    assert await rig.service.refresh() == candidates
    assert rig.reopen().read() == rig.service.read()
    assert len(rig.provider.calls) == 2


def test_exploration_policy_rejects_unreviewed_values():
    assert NotebookConfig().exploration_reasoning_effort == "inherit"
    assert NotebookConfig(exploration_reasoning_effort="low").exploration_reasoning_effort == "low"
    for value in ("medium", "high", "max", "", None):
        with pytest.raises(ValueError):
            NotebookConfig.model_validate({"exploration_reasoning_effort": value})


@pytest.mark.asyncio
@pytest.mark.parametrize("policy,inherited,disabled", [
    ("inherit", "high", False), ("low", "high", False),
    ("inherit", "max", False), ("low", "max", False), ("low", "high", True),
])
async def test_plugin_exploration_policy_is_local_on_real_sdk_wire(rig, policy, inherited, disabled):
    import httpx
    from agent.provider import LLMProvider

    wire = []
    async def handle(request):
        body = json.loads(request.content)
        wire.append(body)
        if body["messages"][-1]["content"] == "main conversation":
            content = "main answer"
        else:
            content = (await rig.provider.chat(messages=body["messages"])).content
        return httpx.Response(200, json={"id": "offline", "object": "chat.completion", "created": 0,
            "model": "offline", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": content}}]})

    shared = LLMProvider(api_key="offline", provider_name="deepseek", max_retries=1,
                         extra_body={"reasoning_effort": inherited}, force_disable_thinking=disabled)
    await shared._client._client.aclose()
    shared._client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    registry = ToolRegistry()
    plugin = AssociativeNotebookPlugin()
    plugin.context = cast(Any, SimpleNamespace(
        config=NotebookConfig.model_validate({"enabled": True, "session_key": KEY,
                                             "exploration_reasoning_effort": policy}),
        workspace=rig.path, session_manager=rig.service.sessions,
        light_provider=shared, light_model="deepseek-v4-flash", tool_registry=registry))
    try:
        await plugin.initialize()
        service = cast(Any, plugin.service)
        service.book.clock = lambda: NOW
        rig.add("activity")
        assert len(await service.refresh()) == 1
        assert await shared.chat(messages=[{"role": "user", "content": "main conversation"}],
                                 tools=[], model="deepseek-v4-flash", max_tokens=777)
        assert len(wire) == 3
        assert [json.loads(w["messages"][-1]["content"])["input"]["role"] for w in wire[:2]] == ["exploration", "judgement"]
        assert [w["max_tokens"] for w in wire] == [4096, 4096, 777]
        if disabled:
            assert all(w["thinking"] == {"type": "disabled"} and "reasoning_effort" not in w for w in wire)
        else:
            assert [w["reasoning_effort"] for w in wire] == [
                "low" if policy == "low" else inherited, inherited, inherited]
        assert shared._extra_body == {"reasoning_effort": inherited}
        assert shared._max_retries == 1 and service.provider._max_retries == 0
        assert service.provider._client is shared._client
        await service.refresh()
        assert len(wire) == 3  # No model call without new material.
    finally:
        await plugin.terminate()
        await shared._client.close()


@pytest.mark.asyncio
async def test_switching_exploration_policy_does_not_resubmit_unknown_run(rig):
    rig.add("activity")
    rig.provider.fail = True
    assert (await rig.service.fetch())["learning"] == "blocked"
    old = rig.service.pending_runs()
    service = NotebookService(directory=rig.service.book.directory, session_key=KEY,
        sessions=rig.service.sessions, provider=rig.provider, model="offline-fake",
        exploration_reasoning_effort="low", clock=lambda: NOW)
    try:
        rig.provider.fail = False
        assert (await service.fetch())["learning"] == "blocked"
        assert service.pending_runs() == old and len(rig.provider.calls) == 1
        assert not service.book.coverage() and not service.book.events("response")
    finally:
        service.close()


@pytest.mark.asyncio
async def test_new_input_withdraws_cached_candidate_and_correction_preserves_interest(
    rig,
):
    rig.add("My interest continues")
    rig.add("I have an activity")
    await rig.service.refresh()
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
async def test_optional_timeout_is_blocked_without_poll_retry_or_stale_notes(rig):
    rig.add("activity")
    rig.provider.fail = True
    result = await rig.service.fetch()
    assert result["learning"] == "blocked" and result["events"] == []
    assert result["error_type"] == "TimeoutError"
    rig.provider.fail = False
    result = await rig.reopen().fetch()
    assert result["learning"] == "blocked"
    assert len(rig.provider.calls) == 1
    with scope() as execution:
        value = json.loads(await NotebookTool(rig.service).execute())
        assert not value["ready"] and value["notes"] == []
        assert not hasattr(execution, "proactive_host_guard")


@pytest.mark.asyncio
async def test_recorded_length_failure_does_not_poison_feed_and_can_be_reviewed(rig):
    from agent.tools.base import Tool
    from core.execution.journal import ExecutionJournal
    from core.execution.proactive import run_proactive_recorded
    from plugins.associative_notebook.recovery import review_length_failure
    from proactive_v2.mcp_sources import fetch_sources_async
    from agent.plugins.specs import ProactiveSourceSpec
    from tests.backend.test_model_context_projection import provider_with_wire

    class Feed(Tool):
        name = "feed_fetch"
        description = "offline feed"
        parameters = {"type": "object", "properties": {}}
        retry_policy = "read_only"

        async def execute(self, **kwargs):
            return encode([{"kind": "content", "id": "article", "title": "Other content"}])

    journal = ExecutionJournal(rig.path)
    provider, wire = await provider_with_wire([{"content": '{"draft":', "finish_reason": "length"}])
    rig.service.provider = provider
    rig.add("first activity")
    rig.add("second interest")
    registry = ToolRegistry()
    registry.register(NotebookTool(rig.service, "fetch"), risk="write", source_type="plugin", source_name="associative_notebook")
    registry.register(Feed(), source_type="plugin", source_name="other_feed")
    gateway = SharedMcpGateway(rig.path, registry)
    sources = [RegisteredProactiveSource(name, ProactiveSourceSpec(id=name, channels=("content",),
                server="plugin:" + name, fetch_tool=tool)) for name, tool in (
                ("associative_notebook", "notebook_fetch"), ("other_feed", "feed_fetch"))]

    async def operation():
        channels = await fetch_sources_async(gateway, sources)
        assert len(channels["content"]) == 1
        assert channels["content"][0]["id"] == "article"
        return 0

    try:
        for key in ("first", "poll"):
            await run_proactive_recorded(journal, KEY, key, {}, operation)
            record = journal.find(KEY, "proactive", key)
            assert record["status"] == "completed"
            assert journal.proactive_blocker(KEY) is None
        assert len(wire) == 1
        pending = rig.service.pending_runs()[0]["id"]
        failure = rig.service.book.events("failure")[0]["payload"]
        assert failure["diagnostics"]["reason"] == "output_truncated"
        assert not rig.service.book.coverage()
        first = journal.find(KEY, "proactive", "first")
        args = (rig.service.book.directory, journal.directory / "events.sqlite3", first["turn_id"], pending)
        preview = review_length_failure(*args, reason="offline review")
        assert not preview["applied"] and rig.service.book.run(pending)["state"] == "awaiting_result"
        review_length_failure(*args, reason="offline review", apply=True)
        assert rig.service.book.run(pending)["state"] == "discarded"
        events = rig.service.book.events()
        review_length_failure(*args, reason="offline review", apply=True)
        assert rig.service.book.events() == events
        assert not rig.service.book.coverage() and len(wire) == 1
        rig.service.provider = rig.provider  # only the local draft double may resume
        await rig.service.refresh()
        assert len(rig.service.book.coverage()) == 1
        assert len(rig.provider.calls) == 2
        assert rig.service.book.run(pending)["state"] == "discarded"
        assert not rig.service.ready()  # remaining material still uncovered
        await rig.service.refresh()
        assert len(rig.service.book.coverage()) == 2 and rig.service.ready()
    finally:
        await provider._client.close()
        journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["timeout", "identity", "request", "replies", "live_host"])
async def test_failure_review_rejects_unknown_mismatch_and_live_host(rig, case):
    import httpx
    from bootstrap.host_state import WorkspaceOwner, WorkspaceInUse
    from core.execution.journal import ExecutionJournal
    from core.execution.proactive import run_proactive_recorded
    from plugins.associative_notebook.recovery import review_length_failure
    from tests.backend.test_model_context_projection import provider_with_wire

    journal = ExecutionJournal(rig.path)
    provider, wire = await provider_with_wire([{"content": "partial", "finish_reason": "length"}])
    if case == "timeout":
        def timeout(request):
            raise httpx.ReadTimeout("offline unknown outcome", request=request)
        await provider._client._client.aclose()
        provider._client._client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
    rig.service.provider = provider
    rig.add("activity")
    owner = None
    try:
        async def operation():
            assert (await rig.service.fetch())["learning"] == "blocked"
            return 0
        await run_proactive_recorded(journal, KEY, "review-rejection", {}, operation)
        row = journal.find(KEY, "proactive", "review-rejection")
        book = rig.service.book
        pending = rig.service.pending_runs()[0]["id"]
        if case == "identity":
            marker = book.directory / "sandbox.json"
            value = json.loads(marker.read_text())
            marker.write_text(encode({**value, "owner": "another-runtime"}))
        elif case == "request":
            value = book.run(pending)["request"]
            value["source_refs"] = []
            book.db.execute("UPDATE runs SET request=? WHERE id=?", (encode(value), pending))
        elif case == "replies":
            capture = {"receipts": {"job": {"cited_note_ids": ["wrong"]}}, "reply_jobs": {"ref": "job"}}
            book.db.execute("UPDATE events SET payload=? WHERE id=?", (encode(capture), "capture:" + pending))
        elif case == "live_host":
            # No status file: the OS lock, not a cached status, must stop the write.
            owner = WorkspaceOwner(rig.path, "host")
        with pytest.raises(WorkspaceInUse if case == "live_host" else NotebookError):
            review_length_failure(book.directory, journal.directory / "events.sqlite3",
                                  row["turn_id"], pending, reason="offline rejection", apply=True)
        assert book.run(pending)["state"] == "awaiting_result"
        assert not book.coverage() and not book.events("failure_review")
        assert len(wire) == (0 if case == "timeout" else 1)
    finally:
        if owner:
            owner.close()
        await provider._client.close()
        journal.close()


@pytest.mark.asyncio
async def test_invalid_material_disables_only_notebook_consumption(rig):
    message = rig.add("activity")
    candidate = (await rig.service.refresh())[0]
    rig.store.update_message(message["id"], content="rewritten activity")
    with scope() as execution:
        read = json.loads(await NotebookTool(rig.service).execute())
        assert not read["ready"] and read["notes"] == []
        assert not hasattr(execution, "proactive_host_guard")
        validation = json.loads(await NotebookTool(rig.service, "validate").execute(event_ids=[candidate["id"]]))
        assert validation == {"valid_event_ids": []}
        assert (await rig.service.fetch())["learning"] == "blocked"
    assert len(rig.provider.calls) == 2


@pytest.mark.asyncio
async def test_previously_admitted_backlog_is_learned_one_material_per_tick(rig):
    for i in range(10):
        rig.add(f"activity {i}")
    sources, _ = rig.service.materials()
    rig.service.book.import_sources(list(sources.values()))
    assert len(rig.service.book.sources()) == 10
    await rig.service.refresh()
    request = json.loads(rig.provider.calls[0]["messages"][1]["content"])["input"]
    assert request["source_refs"] == [next(iter(sources))]
    assert len(rig.service.book.coverage()) == 1
    assert not rig.service.ready() and len(rig.provider.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["journal", "sqlite", "cancelled"])
async def test_optional_learning_never_hides_persistence_or_cancellation(rig, monkeypatch, kind):
    import asyncio
    import sqlite3
    from core.execution.contracts import JournalWriteError
    error = {"journal": JournalWriteError, "sqlite": sqlite3.OperationalError,
             "cancelled": asyncio.CancelledError}[kind]
    async def broken():
        raise error("offline failure")
    monkeypatch.setattr(rig.service, "refresh", broken)
    with pytest.raises(error):
        await rig.service.fetch()


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
@pytest.mark.parametrize("change", ["new_material", "expiry", "user_activity"])
async def test_feed_wake_uses_notebook_in_both_phases_and_fences_queued_send(rig, change):
    from unittest.mock import AsyncMock
    from agent.provider import LLMResponse, ToolCall
    from bus.durable import DeliveryStopped
    from core.execution.journal import ExecutionJournal
    from core.execution.proactive import run_proactive_recorded, proactive_scope
    from plugins.wake_proactive.runtime import WakeRuntime
    from proactive_v2.frame import new_proactive_frame
    from tests.backend.proactive_v2.akashic_wake.test_runtime import (
        _scope, _source, FixedClock, FakeGateway, FakeWebFetch, FailingOrchestrator,
    )

    rig.add("interest in open hardware")
    await rig.service.refresh()
    note = rig.service.read()["notes"][0]
    learning_calls = len(rig.provider.calls)
    provider = SimpleNamespace(chat=AsyncMock(side_effect=[
        LLMResponse(content=None, tool_calls=[ToolCall("s", "scratchpad", {
            "items": [{"item_id": "candidate_1", "initial_interest": "likely_interesting"}],
        })]),
        LLMResponse(content=None, tool_calls=[ToolCall("f", "share_content", {
            "items": [{"item_id": "candidate_1", "summary": "A relevant release",
                       "why_it_matters": "Matches the recorded interest"}],
        })]),
    ]))
    registry = ToolRegistry()
    registry.register(NotebookTool(rig.service))
    registry.register(cast(Any, FakeWebFetch()))
    runtime_scope = _scope(rig.path, FakeGateway([]), provider, FailingOrchestrator(), _source("content"))
    runtime_scope.shared_tools = registry
    runtime = WakeRuntime(runtime_scope, clock=FixedClock(datetime.fromisoformat(NOW)))
    state = runtime.begin(new_proactive_frame(KEY))
    # The only content candidate is a Feed article, not a notebook finding.
    state.ctx.content_events = [{"id": "article", "event_id": "article", "source": "feed",
                                 "title": "Hardware release", "url": "https://example.com/release"}]
    transport = TransportStore(rig.path)
    journal = ExecutionJournal(rig.path)
    outbox = Outbox(transport, None)
    sent = []
    user_idle = [True]

    async def physical():
        sent.append(True)
        return "external-id"

    async def handler(payload):
        if change == "new_material":
            rig.add("Cancel that interest")
        elif change == "expiry":
            rig.clock[0] = "2026-11-02T10:00:00+00:00"
        else:
            user_idle[0] = False
        return await outbox.part("text", {"body": "cached"}, physical)

    async def operation():
        execution = cast(Any, proactive_scope())
        async def previous_guard():
            return user_idle[0]
        execution.proactive_host_guard = previous_guard
        await runtime._run_content_tools(state.ctx)
        assert state.ctx.terminal_action == "reply"
        assert provider.chat.await_count == 2
        for call in provider.chat.await_args_list:
            prompt = "\n".join(m["content"] for m in call.kwargs["messages"])
            assert note["id"] in prompt and note["text"] in prompt
            assert note["evidence"][0]["ref"] in prompt
            assert "Hardware release" in prompt
        assert len(rig.provider.calls) == learning_calls
        assert await execution.proactive_host_guard()
        outbox.handlers["push:telegram"] = handler
        key = outbox.enqueue("push:telegram", {"channel": "telegram", "chat_id": "42"})
        with pytest.raises(DeliveryStopped, match="proactive_revalidation_failed"):
            await outbox.deliver(key)
        return 0

    try:
        await run_proactive_recorded(journal, KEY, "wake-notebook", {}, operation)
        assert not sent
    finally:
        runtime.close()
        transport.close()
        journal.close()


@pytest.mark.asyncio
async def test_wake_notebook_read_disabled_unready_and_wrong_scope(rig):
    from unittest.mock import AsyncMock
    from plugins.wake_proactive.runtime import WakeRuntime
    from tests.backend.proactive_v2.akashic_wake.test_runtime import (
        _scope, _source, FakeGateway, FailingOrchestrator,
    )
    registry = ToolRegistry()
    runtime_scope = _scope(rig.path, FakeGateway([]), SimpleNamespace(chat=AsyncMock()),
                           FailingOrchestrator(), _source("content"))
    runtime_scope.shared_tools = registry
    runtime = WakeRuntime(runtime_scope)
    try:
        assert await runtime._read_notebook() == ""
        rig.add("interest not learned yet")
        registry.register(NotebookTool(rig.service))
        with scope():
            assert await runtime._read_notebook() == ""
        with scope("another:user"):
            with pytest.raises(NotebookError, match="scope mismatch"):
                await runtime._read_notebook()
        assert not rig.provider.calls
        runtime_scope.provider.chat.assert_not_awaited()
    finally:
        runtime.close()


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
        plugin_configs={"associative_notebook": {
            "enabled": True, "session_key": KEY, "exploration_reasoning_effort": "low",
        }},
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
        assert rig.provider.calls[0]["extra_body"] == {"reasoning_effort": "low"}
        assert "extra_body" not in rig.provider.calls[1]
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
