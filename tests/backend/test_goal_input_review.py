"""Regressions from the independent P1 source-intake review."""

from unittest.mock import AsyncMock

import pytest

from core.execution.journal import ExecutionJournal
from core.goals.cli import demo_authorization, demo_definition
from core.goals.service import DemoExecutor, GoalService
from core.goals.store import GoalStore
from tests.backend.conftest import make_job
from tests.backend.test_goal_inputs import NOW, setup, subscribe
from tests.backend.test_spawn_handoff import completed_child, parent


def two_owners(store, service, kind, source):
    store.create(
        "other", demo_definition(store.workspace / "trusted"), demo_authorization()
    )
    for sid, gid, owner in [("a", "g", "session:a"), ("b", "other", "session:b")]:
        service.input_sources.subscribe(
            sid,
            gid,
            kind=kind,
            source_id=source,
            fields=["release"],
            owner_session=owner,
            expected_version=store.get(gid)["version"],
        )


async def test_wake_validation_and_dedup_remain_session_scoped_after_restart(tmp_path):
    journal, store, service = setup(tmp_path)
    event = {"_source": "feed:main", "event_id": "release", "revision": 1, "release": 2}
    try:
        two_owners(store, service, "wake.content", "feed:main")
        a, b = AsyncMock(return_value=True), AsyncMock(return_value=False)
        service.input_sources.bind_wake_validator("session:a", a)
        service.input_sources.bind_wake_validator("session:b", b)
        assert await service.input_sources.capture_wake(
            "session:a", "content", event, NOW
        )
        assert not await service.input_sources.capture_wake(
            "session:b", "content", event, NOW
        )
        assert (a.await_count, b.await_count) == (1, 1)
        assert service.inputs.consume() == 1
        assert store.get("other")["status"] == "waiting"
        assert service.inputs.receipts("b") == []
    finally:
        journal.close()
    journal = ExecutionJournal(tmp_path)
    try:
        store = GoalStore(journal, tmp_path)
        service = GoalService(store, DemoExecutor())
        # A's durable capture never grants B eligibility while B has no validator.
        assert not await service.input_sources.capture_wake(
            "session:b", "content", event, NOW
        )
        b = AsyncMock(return_value=True)
        service.input_sources.bind_wake_validator("session:b", b)
        assert await service.input_sources.capture_wake(
            "session:b", "content", event, NOW
        )
        assert b.await_count == 1
        assert service.inputs.consume() == 1
        assert store.get("other")["status"] == "ready"
        assert len(service.inputs.receipts("b")) == 1
        assert service.inputs.consume() == 0
    finally:
        journal.close()


def test_timer_capture_cannot_wake_another_owner(tmp_path):
    journal, store, service = setup(tmp_path)
    try:
        job = make_job(fire_at=NOW, session_key="session:a")
        two_owners(store, service, "timer", job.id)
        assert service.input_sources.capture_timer(job, NOW)
        assert service.inputs.consume() == 1
        assert store.get("g")["status"] == "ready"
        assert store.get("other")["status"] == "waiting"
        assert service.inputs.receipts("b") == []
    finally:
        journal.close()


def test_session_scoped_publish_requires_explicit_owner(tmp_path):
    journal, _, service = setup(tmp_path)
    try:
        for kind in ("timer", "wake.content"):
            with pytest.raises(ValueError, match="owner session"):
                service.inputs.publish(kind, "source", "item", "1", 1, {})
    finally:
        journal.close()


async def test_completed_child_is_baselined_before_first_result_poll(tmp_path):
    journal, store, service = setup(tmp_path)
    try:
        root = parent(journal)
        child, _ = completed_child(journal, root)
        journal.finish(root, "completed")
        sub = service.input_sources.subscribe(
            "late",
            "g",
            kind="result",
            source_id="child",
            fields=["result"],
            expected_version=store.get("g")["version"],
        )
        assert sub["positions"][child] > 0
        await service.tick(NOW)
        assert store.get("g")["status"] == "waiting"
        assert store.get("g")["budget"]["used"]["model"] == 0
        assert service.inputs.receipts("late")[0]["outcome"] == "baseline"
        assert journal.spawn_completion("child")["acked"] == 0
    finally:
        journal.close()


async def test_completed_goal_phase_is_also_baselined(tmp_path):
    journal, store, service = setup(tmp_path)
    try:
        await service.advance("g", expected_version=store.get("g")["version"])
        phase = store.get("g")["phases"][0]
        store.create(
            "other", demo_definition(tmp_path / "trusted"), demo_authorization()
        )
        service.input_sources.subscribe(
            "late",
            "other",
            kind="result",
            source_id=phase["phase_id"],
            fields=["result"],
            expected_version=store.get("other")["version"],
        )
        service.input_sources.poll_results()
        assert service.inputs.consume() == 0
        assert store.get("other")["status"] == "waiting"
        assert service.inputs.receipts("late")[0]["outcome"] == "baseline"
    finally:
        journal.close()


def test_wildcard_fingerprints_survive_restart_and_ignore_unrelated_changes(tmp_path):
    journal, store, service = setup(tmp_path)

    def publish(item, version, release):
        return service.inputs.publish(
            "wake.content",
            "feed_plugin:main",
            item,
            str(version),
            version,
            {"release": release, "rank": version},
            owner_session="telegram:1",
        )

    try:
        publish("A", 1, 1)
        publish("B", 1, 2)
        subscribe(service)
        initial = store.get("g")["observed_sources"]["input:dep"]
        publish("A", 2, 1)
        assert service.inputs.consume() == 0
        assert store.get("g")["status"] == "waiting"
        assert service.inputs.receipts("dep")[0]["outcome"] == "unchanged"
    finally:
        journal.close()
    journal = ExecutionJournal(tmp_path)
    try:
        store = GoalStore(journal, tmp_path)
        service = GoalService(store, DemoExecutor())
        publish("B", 2, 2)
        assert service.inputs.consume() == 0
        # A changes to the same value as B: still a real change in A.
        publish("A", 3, 2)
        assert service.inputs.consume() == 1
        assert store.get("g")["status"] == "ready"
        assert store.get("g")["observed_sources"]["input:dep"] != initial
        assert len(service.inputs.get("dep")["fingerprints"]) == 2
        assert service.inputs.consume() == 0
    finally:
        journal.close()
