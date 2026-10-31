"""P1 source intake: real SQLite and process death, fake inference, no channels."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.scheduler import SchedulerService
from core.execution.journal import ExecutionJournal
from core.goals.cli import demo_authorization, demo_definition
from core.goals.contracts import GoalConflict
from core.goals.service import DemoExecutor, GoalService
from core.goals.store import GoalStore
from tests.backend.conftest import drain_tasks, make_job


NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def setup(path, auth=None):
    path.mkdir(exist_ok=True)
    inputs = path / "trusted"
    inputs.mkdir(exist_ok=True)
    journal = ExecutionJournal(path)
    store = GoalStore(journal, path)
    store.create("g", demo_definition(inputs), auth or demo_authorization())
    return journal, store, GoalService(store, DemoExecutor())


def subscribe(service, **kwargs):
    return service.inputs.subscribe(
        "dep",
        "g",
        kind="wake.content",
        source_id="feed_plugin:main",
        fields=["release"],
        owner_session="telegram:1",
        expected_version=service.store.get("g")["version"],
        **kwargs,
    )


def publish(service, version=2, **payload):
    return service.inputs.publish(
        "wake.content",
        "feed_plugin:main",
        "dependency",
        str(version),
        version,
        {"release": version, **payload},
        owner_session="telegram:1",
    )


async def converge(store, service):
    for _ in range(10):
        await service.tick(NOW)
        state = store.get("g")
        if state["status"] in {"succeeded", "cancelled", "failed", "needs_review"}:
            return state
    raise AssertionError("No bounded convergence")


async def test_duplicate_out_of_order_and_unrelated_fields_infer_once(tmp_path):
    journal, store, service = setup(tmp_path)
    try:
        publish(service, 1)
        subscribe(service)
        stable = store.get("g")["version"]
        for _ in range(4):
            publish(service, 1, rank=999)
            await service.tick(NOW)
        assert store.get("g")["version"] == stable
        assert store.get("g")["budget"]["used"]["model"] == 0
        service.inputs.publish(
            "wake.content",
            "feed_plugin:main",
            "dependency",
            "metadata",
            2,
            {"release": 1, "rank": 5},
            owner_session="telegram:1",
        )
        await service.tick(NOW)
        assert store.get("g")["status"] == "waiting"
        publish(service, 4)
        publish(service, 3)
        publish(service, 4)
        await service.tick(NOW)
        state = store.get("g")
        assert state["budget"]["used"]["model"] == 1
        assert state["source_inputs"]["input:dep"]["fields"]["release"]["value"] == 4
        assert len(state["attempts"]) == 1
        outcomes = [r["outcome"] for r in service.inputs.receipts("dep")]
        assert outcomes == ["unchanged", "applied", "stale"]
        assert service.inputs.consume() == 0
    finally:
        journal.close()


def test_receipt_and_goal_observation_roll_back_together(tmp_path):
    journal, store, service = setup(tmp_path)
    try:
        subscribe(service)
        before = store.get("g")
        publish(service)
        journal._conn.execute(
            "CREATE TRIGGER reject_input_receipt BEFORE INSERT ON goal_input_receipts "
            "BEGIN SELECT RAISE(ABORT,'crash before receipt'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            service.inputs.consume()
        assert store.get("g") == before
        assert service.inputs.get("dep")["cursor"] == 0
        journal._conn.execute("DROP TRIGGER reject_input_receipt")
        assert service.inputs.consume() == 1
        assert service.inputs.consume() == 0
        assert store.get("g")["status"] == "ready"
    finally:
        journal.close()


def test_peer_consumers_share_atomic_cursor_and_receipt(tmp_path):
    journal, store, service = setup(tmp_path)
    peer_journal = ExecutionJournal(tmp_path)
    peer = GoalService(GoalStore(peer_journal, tmp_path), DemoExecutor())
    try:
        subscribe(service)
        publish(service)
        with ThreadPoolExecutor(2) as pool:
            counts = list(pool.map(lambda s: s.inputs.consume(), [service, peer]))
        assert sum(counts) == 1
        assert len(service.inputs.receipts("dep")) == 1
        assert store.get("g")["source_inputs"]["input:dep"]["version"] == "2"
    finally:
        peer_journal.close()
        journal.close()


async def test_offline_capture_replays_after_restart_and_rebuild(tmp_path):
    journal, _, service = setup(tmp_path)
    subscribe(service)
    journal.close()
    writer = ExecutionJournal(tmp_path)
    captured = GoalService(GoalStore(writer, tmp_path), DemoExecutor())
    publish(captured)
    writer.close()
    journal = ExecutionJournal(tmp_path)
    try:
        store = GoalStore(journal, tmp_path)
        service = GoalService(store, DemoExecutor())
        journal.rebuild_projection()
        store.rebuild()
        await service.tick(NOW)
        assert store.get("g")["budget"]["used"]["model"] == 1
        assert len(service.inputs.receipts("dep")) == 1
    finally:
        journal.close()


async def test_cancellation_keeps_tombstone_and_late_input_never_reopens(tmp_path):
    journal, store, service = setup(tmp_path)
    try:
        subscribe(service)
        publish(service)
        service.inputs.cancel("dep")
        service.inputs.cancel("dep")
        assert service.inputs.owns_input(
            "telegram:1",
            "wake.content",
            "feed_plugin:main",
            "dependency",
            "2",
            {"release": 2},
        )
        assert not service.inputs.owns_input(
            "telegram:1",
            "wake.content",
            "feed_plugin:main",
            "dependency",
            "3",
            {"release": 3},
        )
        with pytest.raises(GoalConflict):
            subscribe(service)
        state = store.get("g")
        store.cancel("g", expected_version=state["version"])
        publish(service, 3)
        for _ in range(3):
            await service.tick(NOW)
        assert store.get("g")["status"] == "cancelled"
        assert not store.get("g")["reservations"]
        assert service.inputs.receipts("dep") == []
    finally:
        journal.close()


async def test_new_subscription_baselines_backlog_and_requires_new_change(tmp_path):
    journal, store, service = setup(tmp_path)
    try:
        subscribe(service)
        publish(service)
        service.inputs.cancel("dep")
        # Host explicitly replaces the idle wait, preserving the original goal.
        state = store.get("g")
        store.revise("g", state["definition"], expected_version=state["version"])
        service.inputs.subscribe(
            "fresh",
            "g",
            kind="wake.content",
            source_id="feed_plugin:main",
            fields=["release"],
            owner_session="telegram:1",
            expected_version=store.get("g")["version"],
        )
        await service.tick(NOW)
        assert store.get("g")["status"] == "waiting"
        assert not store.get("g")["reservations"]
        publish(service, 3)
        await service.tick(NOW)
        assert store.get("g")["budget"]["used"]["model"] == 1
    finally:
        journal.close()


async def test_event_failure_revision_restart_and_success_use_p0_budget(tmp_path):
    journal, store, service = setup(tmp_path)
    subscribe(service)
    await service.tick(NOW)
    assert store.get("g")["budget"]["used"]["model"] == 0
    publish(service)
    await service.tick(NOW)
    await service.tick(NOW)
    assert store.get("g")["step"] == "replan"
    assert store.get("g")["evidence"][0]["checks"][1]["returncode"] == 1
    journal.close()
    journal = ExecutionJournal(tmp_path)
    try:
        store = GoalStore(journal, tmp_path)
        service = GoalService(store, DemoExecutor())
        result = await converge(store, service)
        assert result["status"] == "succeeded"
        assert result["budget"]["used"] == {
            "total": 9,
            "model": 2,
            "tool": 2,
            "validation": 4,
            "replan": 1,
        }
        before = store.get("g")
        for _ in range(4):
            publish(service)
            await service.tick(NOW)
        assert store.get("g") == before
        assert len(service.inputs.receipts("dep")) == 1
    finally:
        journal.close()


async def test_event_cannot_bypass_p0_budget_or_expiry(tmp_path):
    auth = demo_authorization()
    auth["limits"]["model"] = 0
    journal, store, service = setup(tmp_path, auth)
    try:
        subscribe(service)
        publish(service)
        await service.tick(NOW)
        state = store.get("g")
        assert state["status"] == "failed" and state["reason"] == "budget_exhausted"
        publish(service, 3)
        await service.tick(NOW)
        assert store.get("g")["status"] == "failed"
        assert service.inputs.receipts("dep")[-1]["outcome"] == "goal_terminal"
    finally:
        journal.close()


@pytest.mark.parametrize("tier", ["instant", "soft"])
async def test_goal_owned_timer_has_no_parallel_model_or_delivery(tmp_path, tier):
    journal, store, service = setup(tmp_path)
    push = SimpleNamespace(send=AsyncMock())
    loop = SimpleNamespace(process_direct=AsyncMock())
    scheduler = SchedulerService(
        tmp_path / "schedules.json", push, agent_loop=loop, _now_fn=lambda: NOW
    )
    service.bind_scheduler(scheduler)
    job = make_job(tier=tier, fire_at=NOW, session_key="telegram:1")
    scheduler.add_job(job)
    service.inputs.subscribe(
        "timer",
        "g",
        kind="timer",
        source_id=job.id,
        fields=["scheduled_at"],
        owner_session=job.session_key,
        expected_version=store.get("g")["version"],
    )
    try:
        await scheduler._tick()
        await drain_tasks()
        assert store.get("g")["budget"]["used"]["model"] == 1
        assert len(service.inputs.receipts("timer")) == 1
        push.send.assert_not_awaited()
        loop.process_direct.assert_not_awaited()
    finally:
        await scheduler.aclose()
        journal.close()


async def test_missed_goal_timer_captured_before_ordinary_misfire_discard(tmp_path):
    journal, store, service = setup(tmp_path)
    scheduler = SchedulerService(tmp_path / "schedules.json", None, _now_fn=lambda: NOW)
    job = make_job(fire_at=NOW - timedelta(days=1), session_key="telegram:1")
    scheduler.add_job(job)
    service.inputs.subscribe(
        "timer",
        "g",
        kind="timer",
        source_id=job.id,
        fields=["scheduled_at"],
        owner_session=job.session_key,
        expected_version=store.get("g")["version"],
    )
    scheduler = SchedulerService(tmp_path / "schedules.json", None, _now_fn=lambda: NOW)
    service.bind_scheduler(scheduler)
    try:
        scheduler.load_and_recover()
        assert scheduler.list_jobs() == []
        await service.tick(NOW)
        assert store.get("g")["budget"]["used"]["model"] == 1
        assert len(service.inputs.receipts("timer")) == 1
    finally:
        await scheduler.aclose()
        journal.close()


async def test_fast_hint_starts_existing_internal_tick_without_waiting_for_poll(
    tmp_path,
):
    journal, store, service = setup(tmp_path)
    scheduler = SchedulerService(tmp_path / "schedules.json", None, _now_fn=lambda: NOW)
    service.bind_scheduler(scheduler)
    subscribe(service)
    scheduler._running = True
    try:
        publish(service)
        publish(service)
        assert len(scheduler._active_tasks) == 1
        await drain_tasks()
        assert store.get("g")["budget"]["used"]["model"] == 1
        assert len(service.inputs.receipts("dep")) == 1
    finally:
        await scheduler.aclose()
        journal.close()


async def test_committed_child_result_maps_without_touching_completion_ack(tmp_path):
    from tests.backend.test_spawn_handoff import parent, completed_child

    journal, store, service = setup(tmp_path)
    try:
        service.inputs.subscribe(
            "child",
            "g",
            kind="result",
            source_id="child",
            fields=["result"],
            expected_version=store.get("g")["version"],
        )
        root = parent(journal)
        child, _ = completed_child(journal, root)
        journal.finish(root, "completed")
        await service.tick(NOW)
        assert store.get("g")["budget"]["used"]["model"] == 1
        assert journal.spawn_completion("child")["acked"] == 0
        snapshot = store.get("g")["source_inputs"]["input:child"]
        assert snapshot["item_id"] == child
        assert len(service.inputs.receipts("child")) == 1
        service.input_sources.poll_results()
        assert service.inputs.consume() == 0
    finally:
        journal.close()


async def test_unresolved_child_is_not_treated_as_successful_input(tmp_path):
    from tests.backend.test_spawn_handoff import parent, completed_child

    journal, store, service = setup(tmp_path)
    try:
        service.inputs.subscribe(
            "child",
            "g",
            kind="result",
            source_id="child",
            fields=["result"],
            expected_version=store.get("g")["version"],
        )
        root = parent(journal)
        completed_child(journal, root, reason="needs_review")
        journal.finish(root, "completed")
        await service.tick(NOW)
        assert store.get("g")["status"] == "waiting"
        assert not store.get("g")["reservations"]
        assert service.inputs.receipts("child")[0]["outcome"] == "result_unresolved"
    finally:
        journal.close()


async def test_wake_input_ownership_uses_existing_ack_and_never_notifies(tmp_path):
    from tests.backend.proactive_v2.akashic_wake.test_runtime import (
        _scope,
        _source,
        FakeGateway,
        FakeOrchestrator,
        FixedClock,
    )
    from plugins.wake_proactive.runtime import WakeRuntime
    from proactive_v2.frame import new_proactive_frame

    journal, store, service = setup(tmp_path)
    subscribe(service)
    event = {
        "kind": "content",
        "event_id": "dependency",
        "revision": 2,
        "release": 2,
        "title": "release",
    }
    gateway = FakeGateway([event])
    provider = SimpleNamespace(chat=AsyncMock())
    orchestrator = FakeOrchestrator()
    scope = _scope(tmp_path, gateway, provider, orchestrator, _source("content"))
    scope.goal_input_sources = service.input_sources
    runtime = WakeRuntime(scope, clock=FixedClock(NOW))
    try:
        state = runtime.begin(new_proactive_frame("telegram:1"))
        await runtime.ingest(state)
        assert state.contents == []
        assert len(gateway.acks) == 1
        assert gateway.acks[0]["event_ids"] == ["dependency"]
        assert not await runtime.decide_content(state)
        assert orchestrator.results == []
        provider.chat.assert_not_awaited()
        await service.tick(NOW)
        assert store.get("g")["budget"]["used"]["model"] == 1
        assert len(service.inputs.receipts("dep")) == 1
    finally:
        runtime.close()
        journal.close()


@pytest.mark.parametrize("point", ["captured", "consumed", "executed"])
def test_real_process_exit_resumes_one_input_and_one_execution(tmp_path, point):
    code = r"""
import asyncio,os,sys
from pathlib import Path
from core.execution.journal import ExecutionJournal
from core.goals.store import GoalStore
from core.goals.service import GoalService,DemoExecutor
from core.goals.cli import demo_definition,demo_authorization
p=Path(sys.argv[1]); (p/'trusted').mkdir(parents=True)
j=ExecutionJournal(p); s=GoalStore(j,p); s.create('g',demo_definition(p/'trusted'),demo_authorization())
v=GoalService(s,DemoExecutor()); v.inputs.subscribe('dep','g',kind='wake.content',source_id='feed_plugin:main',fields=['release'],owner_session='telegram:1',expected_version=s.get('g')['version'])
v.inputs.publish('wake.content','feed_plugin:main','dependency','2',2,{'release':2},owner_session='telegram:1')
if sys.argv[2]!='captured': v.inputs.consume()
if sys.argv[2]=='executed': asyncio.run(v.tick())
os._exit(74)
"""
    env = {**os.environ, "PYTHONPATH": str(Path("apps/backend").resolve())}
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), point],
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 74, result.stderr.decode(errors="replace")

    async def recover():
        journal = ExecutionJournal(tmp_path)
        try:
            store = GoalStore(journal, tmp_path)
            service = GoalService(store, DemoExecutor())
            await service.tick(NOW)
            assert store.get("g")["budget"]["used"]["model"] == 1
            assert len(store.get("g")["attempts"]) == 1
            assert len(service.inputs.receipts("dep")) == 1
        finally:
            journal.close()

    asyncio.run(recover())


async def test_new_wake_subscription_never_replays_uncaptured_historical_snapshot(
    tmp_path,
):
    from plugins.wake_proactive.state import WakeStateStore

    journal, store, service = setup(tmp_path)
    wake = WakeStateStore(tmp_path / "wake_proactive.db")
    try:
        old = {
            "event_id": "dependency",
            "ack_server": "feed_plugin:main",
            "revision": 2,
            "release": 2,
        }
        wake.ingest("content", [old], NOW)
        service.input_sources.subscribe(
            "dep",
            "g",
            kind="wake.content",
            source_id="feed_plugin:main",
            fields=["release"],
            owner_session="telegram:1",
            expected_version=store.get("g")["version"],
        )
        service.input_sources.bind_wake_validator(
            "telegram:1", AsyncMock(return_value=True)
        )
        await service.tick(NOW)
        assert store.get("g")["status"] == "waiting"
        assert not store.get("g")["reservations"]
        assert service.inputs.receipts("dep")[0]["outcome"] == "baseline"
        wake.ingest("content", [{**old, "revision": 3, "release": 3}], NOW)
        await service.tick(NOW)
        assert store.get("g")["budget"]["used"]["model"] == 1
    finally:
        wake.close()
        journal.close()


async def test_wake_capture_gap_recovers_consumed_reservoir_and_validation_failure_waits(
    tmp_path,
):
    from plugins.wake_proactive.state import WakeStateStore

    journal, store, service = setup(tmp_path)
    subscribe(service)
    wake = WakeStateStore(tmp_path / "wake_proactive.db")
    wake.ingest(
        "content",
        [
            {
                "id": "dependency",
                "ack_server": "feed_plugin:main",
                "revision": 2,
                "release": 2,
            }
        ],
        NOW,
    )
    wake.consume(["feed_plugin:main:dependency"], NOW)
    wake.close()
    journal.close()
    journal = ExecutionJournal(tmp_path)
    try:
        store = GoalStore(journal, tmp_path)
        service = GoalService(store, DemoExecutor())
        validator = AsyncMock(return_value=False)
        service.input_sources.bind_wake_validator("telegram:1", validator)
        await service.tick(NOW)
        assert store.get("g")["status"] == "waiting"
        assert service.inputs.receipts("dep") == []
        validator.return_value = True
        await service.tick(NOW)
        assert store.get("g")["budget"]["used"]["model"] == 1
        assert len(service.inputs.receipts("dep")) == 1
        assert service.inputs.receipts("dep")[0]["outcome"] == "applied"
    finally:
        journal.close()


@pytest.mark.parametrize("kind", ["alert", "context"])
async def test_wake_alert_and_context_take_only_goal_path(tmp_path, kind):
    from tests.backend.proactive_v2.akashic_wake.test_runtime import (
        _scope,
        _source,
        FakeGateway,
        FakeContextGateway,
        FakeOrchestrator,
        FixedClock,
    )
    from plugins.wake_proactive.runtime import WakeRuntime
    from proactive_v2.frame import new_proactive_frame

    journal, store, service = setup(tmp_path)
    service.inputs.subscribe(
        "dep",
        "g",
        kind="wake." + kind,
        source_id="feed_plugin:main",
        fields=["release"],
        owner_session="telegram:1",
        expected_version=store.get("g")["version"],
    )
    event = {
        "kind": kind,
        "event_id": "dependency",
        "revision": 2,
        "release": 2,
        "observed_at": NOW.isoformat(),
        "transition": "away->active",
        "presence": "active",
        "confidence": 0.9,
        "interruptibility": 0.8,
    }
    gateway = FakeContextGateway(event) if kind == "context" else FakeGateway([event])
    provider = SimpleNamespace(chat=AsyncMock())
    orchestrator = FakeOrchestrator()
    scope = _scope(tmp_path, gateway, provider, orchestrator, _source(kind))
    scope.goal_input_sources = service.input_sources
    runtime = WakeRuntime(scope, clock=FixedClock(NOW))
    try:
        state = runtime.begin(new_proactive_frame("telegram:1"))
        await runtime.ingest(state)
        assert state.alerts == []
        assert not state.context_reevaluate
        if kind == "context":
            assert runtime._state.context_reevaluation_state() is None
        assert not await runtime.decide_content(state)
        provider.chat.assert_not_awaited()
        assert orchestrator.results == []
        await service.tick(NOW)
        assert store.get("g")["budget"]["used"]["model"] == 1
    finally:
        runtime.close()
        assert service.input_sources._wake_validators == {}
        journal.close()


async def test_expired_input_and_unrelated_filter_cannot_grant_execution(tmp_path):
    journal, store, service = setup(tmp_path)
    try:
        subscribe(service, filters={"package": "chosen"})
        publish(service, package="other")
        publish(
            service,
            3,
            package="chosen",
            expires_at=(datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
        )
        await service.tick(NOW)
        assert store.get("g")["status"] == "waiting"
        assert not store.get("g")["reservations"]
        assert [r["outcome"] for r in service.inputs.receipts("dep")] == [
            "filtered",
            "expired",
        ]
    finally:
        journal.close()


async def test_real_kaze_subagent_receives_committed_input_snapshot(tmp_path):
    from agent.provider import LLMResponse, ToolCall
    from core.goals.executor import GoalSubagentExecutor

    class Provider:
        calls = 0

        async def chat(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                request = json.loads(kwargs["messages"][-1]["content"])
                assert (
                    request["source_inputs"]["input:dep"]["fields"]["release"]["value"]
                    == 2
                )
                assert request["input_versions"]["input:dep"]
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            "write",
                            "write_file",
                            {"path": "result.json", "content": '{"ok":true}'},
                        )
                    ],
                )
            return LLMResponse(content="completed")

    journal, store, _ = setup(tmp_path)
    provider = Provider()
    service = GoalService(store, GoalSubagentExecutor(provider, "fake-model"))
    try:
        subscribe(service)
        publish(service)
        result = await converge(store, service)
        assert result["status"] == "succeeded"
        assert provider.calls == 2
        assert len(service.inputs.receipts("dep")) == 1
    finally:
        journal.close()


async def test_unsubscribe_keeps_captured_timer_from_falling_back_to_notification(
    tmp_path,
):
    journal, store, service = setup(tmp_path)
    push = SimpleNamespace(send=AsyncMock())
    loop = SimpleNamespace(process_direct=AsyncMock())
    scheduler = SchedulerService(
        tmp_path / "schedules.json", push, agent_loop=loop, _now_fn=lambda: NOW
    )
    service.bind_scheduler(scheduler)
    job = make_job(fire_at=NOW, session_key="telegram:1")
    scheduler.add_job(job)
    service.inputs.subscribe(
        "timer",
        "g",
        kind="timer",
        source_id=job.id,
        fields=["scheduled_at"],
        owner_session=job.session_key,
        expected_version=store.get("g")["version"],
    )
    try:
        assert service.input_sources.capture_timer(job, NOW)
        service.inputs.cancel("timer")
        await scheduler._execute_and_reschedule(job)
        assert scheduler.list_jobs() == []
        push.send.assert_not_awaited()
        loop.process_direct.assert_not_awaited()
        assert store.get("g")["status"] == "waiting"
    finally:
        await scheduler.aclose()
        journal.close()


async def test_goal_context_does_not_suppress_unrelated_context_transition(tmp_path):
    from tests.backend.proactive_v2.akashic_wake.test_runtime import (
        _scope,
        _source,
        FakeContextGateway,
        FakeOrchestrator,
        FixedClock,
    )
    from agent.plugins.specs import ProactiveSourceSpec, RegisteredProactiveSource
    from plugins.wake_proactive.runtime import WakeRuntime
    from plugins.wake_proactive.state import WakeStateStore
    from proactive_v2.frame import new_proactive_frame

    journal, store, service = setup(tmp_path)
    service.inputs.subscribe(
        "dep",
        "g",
        kind="wake.context",
        source_id="feed_plugin:main",
        fields=["presence"],
        owner_session="telegram:1",
        expected_version=store.get("g")["version"],
    )
    snapshot = {
        "revision": 2,
        "presence": "active",
        "transition": "away->active",
        "confidence": 0.9,
        "interruptibility": 0.8,
        "observed_at": NOW.isoformat(),
    }
    scope = _scope(
        tmp_path,
        FakeContextGateway(snapshot),
        SimpleNamespace(chat=AsyncMock()),
        FakeOrchestrator(),
        _source("context"),
    )
    scope.proactive_sources.append(
        RegisteredProactiveSource(
            plugin_id="other",
            spec=ProactiveSourceSpec(
                id="main", channels=("context",), server="other", fetch_tool="fetch"
            ),
        )
    )
    scope.goal_input_sources = service.input_sources
    # Inject a different persistence filename as supported by Wake's constructor.
    runtime = WakeRuntime(
        scope,
        state_store=WakeStateStore(tmp_path / "injected.db"),
        clock=FixedClock(NOW),
    )
    try:
        state = runtime.begin(new_proactive_frame("telegram:1"))
        await runtime.ingest(state)
        assert state.context_event["_source"] == "other:main"
        assert state.context_reevaluate
        await service.tick(NOW)
        assert store.get("g")["budget"]["used"]["model"] == 1
    finally:
        runtime.close()
        journal.close()
