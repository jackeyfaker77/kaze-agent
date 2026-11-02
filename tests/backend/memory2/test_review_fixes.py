"""Regression boundaries found by the independent P0/P1 review."""

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.tools.memorize import MemorizeTool
from agent.tools.recall_memory import RecallMemoryTool
from core.memory.task_context import memory_task
from memory2 import durable_tasks
from memory2.memorizer import Memorizer
from memory2.retriever import Retriever
from memory2.store import MemoryStore2
from plugins.default_memory.engine import DefaultMemoryEngine
from plugins.default_memory.engine.admin import _undo_store_by_message_sources


@pytest.fixture
def store(tmp_path):
    result = MemoryStore2(tmp_path / "memory.db", vec_dim=2)
    yield result
    result.close()


def insert(store, text, source):
    return store.upsert_item("profile", text, [1.0, 0.0], source).split(":")[1]


def replace(store, old, text, source):
    return store.replace_items(
        memory_type="profile",
        summary=text,
        embedding=[1.0, 0.0],
        source_ref=source,
        extra={},
        supersedes=[old],
    ).split(":")[1]


def statuses(store):
    return dict(store._db.execute("SELECT summary,status FROM memory_items").fetchall())


@pytest.mark.parametrize("state", ["pending", "running"])
async def test_v1_committed_receipt_is_blocked_before_v2_worker_runs(
    store, tmp_path, state
):
    journal_db = sqlite3.connect(":memory:")
    journal_db.execute("CREATE TABLE events(seq INTEGER,kind TEXT,payload TEXT)")
    journal = SimpleNamespace(
        root_id="root", epoch="new", directory=tmp_path, _conn=journal_db
    )
    worker = SimpleNamespace(run=AsyncMock())
    jobs = durable_tasks.MemoryTasks(store, journal, worker)
    store._db.execute(
        "INSERT INTO memory_tasks VALUES (?,?,?,?,?,?,?,?,?)",
        ("old-job", "root", 1, "turn", 1, "{}", state, "dead", None),
    )
    store._db.commit()
    task = durable_tasks.Task(jobs, {"id": "old-job"})
    token = memory_task.set(task)
    frame = durable_tasks._frame.set(("", [0]))
    try:
        # v1's search occupied a durable step before the committed write.
        store.vector_search([1.0, 0.0])
        store.upsert_item("preference", "old committed item", [1.0, 0.0], "user:1")
    finally:
        durable_tasks._frame.reset(frame)
        memory_task.reset(token)
    receipts = store._db.execute("SELECT * FROM memory_task_steps").fetchall()
    try:
        await jobs.drain()
        await jobs.drain()
        worker.run.assert_not_awaited()
        assert store._db.execute("SELECT state,error FROM memory_tasks").fetchone() == (
            "blocked",
            "unsupported_workflow_version",
        )
        assert (
            store._db.execute("SELECT * FROM memory_task_steps").fetchall() == receipts
        )
        assert store._db.execute(
            "SELECT reinforcement FROM memory_items"
        ).fetchone() == (1,)
        # Current-version work remains runnable; completed old work is untouched.
        store._db.execute(
            "INSERT INTO memory_tasks VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "new-job",
                "root",
                2,
                "new-turn",
                jobs.VERSION,
                "{}",
                "pending",
                None,
                None,
            ),
        )
        store._db.execute(
            "INSERT INTO memory_tasks VALUES (?,?,?,?,?,?,?,?,?)",
            ("old-done", "root", 3, "done-turn", 1, "{}", "done", "dead", None),
        )
        store._db.commit()
        await jobs.drain()
        worker.run.assert_awaited_once()
        assert store._db.execute(
            "SELECT state FROM memory_tasks WHERE id='new-job'"
        ).fetchone() == ("done",)
        assert store._db.execute(
            "SELECT state FROM memory_tasks WHERE id='old-done'"
        ).fetchone() == ("done",)
    finally:
        await jobs.aclose()
        journal_db.close()


async def test_shared_memory_can_be_recalled_and_corrected_from_another_channel(store):
    embedder = SimpleNamespace(embed=AsyncMock(return_value=[1.0, 0.0]))
    engine = DefaultMemoryEngine.__new__(DefaultMemoryEngine)
    engine._v2_store = store
    engine._memorizer = Memorizer(store, embedder)
    engine._retriever = Retriever(store, embedder)
    engine._tagger = None
    tool = MemorizeTool(engine, engine.tool_profile().memorize)
    first = json.loads(
        await tool.execute(
            "I live in Osaka",
            memory_kind="profile",
            current_user_source_ref="desktop:1",
            channel="desktop",
            chat_id="session-a",
            session_key="session-a",
        )
    )
    recall = RecallMemoryTool(engine, engine.tool_profile().recall)
    result = json.loads(
        await recall.execute(query="Where do I live?", channel="telegram", chat_id="42")
    )
    assert first["item_id"] in {item["id"] for item in result["items"]}
    result = json.loads(
        await tool.execute(
            "I now live in Tokyo",
            memory_kind="profile",
            current_user_source_ref="telegram:1",
            channel="telegram",
            chat_id="42",
            session_key="telegram:42",
            supersedes=[first["item_id"]],
        )
    )
    assert result["status"] == "replaced"
    assert statuses(store) == {
        "I live in Osaka": "superseded",
        "I now live in Tokyo": "active",
    }


def test_private_domain_keeps_source_scope_isolation(store):
    old = store.upsert_item(
        "profile",
        "private",
        [1.0, 0.0],
        extra={
            "memory_domain": "private",
            "scope_channel": "desktop",
            "scope_chat_id": "a",
        },
    ).split(":")[1]
    with pytest.raises(ValueError, match="scope mismatch"):
        store.replace_items(
            memory_type="profile",
            summary="changed",
            embedding=[1.0, 0.0],
            source_ref="m2",
            extra={
                "memory_domain": "private",
                "scope_channel": "telegram",
                "scope_chat_id": "b",
            },
            supersedes=[old],
        )
    assert statuses(store) == {"private": "active"}


def test_undo_correction_to_existing_target_preserves_target_and_is_idempotent(store):
    a, b = insert(store, "A", "m1"), insert(store, "B", "m0")
    assert replace(store, a, "B", "m2") == b
    before = list(store._db.iterdump())
    preview = _undo_store_by_message_sources(store, ["m2"], dry_run=True)
    assert list(store._db.iterdump()) == before
    result = _undo_store_by_message_sources(store, ["m2"])
    assert preview == result
    assert result["affected_ids"] == [] and result["restored_ids"] == [a]
    assert result["rollback_source_ids"] == ["m2"]
    assert statuses(store) == {"A": "active", "B": "active"}
    assert store.list_replacements()[0]["relation_type"] == "supersede_undone"
    before = list(store._db.iterdump())
    assert _undo_store_by_message_sources(store, ["m2"])["restored_ids"] == []
    with pytest.raises(ValueError, match="source has been undone"):
        replace(store, a, "B", "m2")
    assert list(store._db.iterdump()) == before


def test_undo_same_turn_replacement_chain_never_restores_withdrawn_intermediate(store):
    a = insert(store, "A", "m1")
    b = replace(store, a, "B", "m2")
    c = replace(store, b, "C", "m2")
    preview = _undo_store_by_message_sources(store, ["m2"], dry_run=True)
    assert _undo_store_by_message_sources(store, ["m2"]) == preview
    assert set(preview["affected_ids"]) == {b, c}
    assert preview["restored_ids"] == [a]
    assert statuses(store) == {"A": "active", "B": "superseded", "C": "superseded"}
    assert _undo_store_by_message_sources(store, ["m2"])["restored_ids"] == []


def test_sequential_undo_restores_only_the_previous_valid_fact(store):
    a = insert(store, "A", "m1")
    b = replace(store, a, "B", "m2")
    replace(store, b, "C", "m3")
    assert _undo_store_by_message_sources(store, ["m3"])["restored_ids"] == [b]
    assert _undo_store_by_message_sources(store, ["m2"])["restored_ids"] == [a]
    assert statuses(store) == {"A": "active", "B": "superseded", "C": "superseded"}


def test_undo_middle_source_preserves_current_fact_and_never_revives_withdrawn_source(
    store,
):
    a = insert(store, "A", "m1")
    b = replace(store, a, "B", "m2")
    replace(store, b, "C", "m3")
    assert _undo_store_by_message_sources(store, ["m2"])["restored_ids"] == []
    assert statuses(store) == {"A": "superseded", "B": "superseded", "C": "active"}
    assert _undo_store_by_message_sources(store, ["m3"])["restored_ids"] == [a]
    assert statuses(store) == {"A": "active", "B": "superseded", "C": "superseded"}


def test_undo_rollback_covers_items_and_relationships(store):
    a = insert(store, "A", "m1")
    replace(store, a, "B", "m2")
    store._db.execute(
        "CREATE TRIGGER fail_undo BEFORE UPDATE ON memory_replacements "
        "BEGIN SELECT RAISE(ABORT, 'undo failed'); END"
    )
    store._db.commit()
    before = list(store._db.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match="undo failed"):
        _undo_store_by_message_sources(store, ["m2"])
    assert list(store._db.iterdump()) == before
    assert store._db.defer_commit is False


def test_undo_respects_existing_outer_transaction(store):
    a = insert(store, "A", "m1")
    replace(store, a, "B", "m2")
    before = list(store._db.iterdump())
    store._db.execute("BEGIN IMMEDIATE")
    store._db.defer_commit = True
    _undo_store_by_message_sources(store, ["m2"])
    assert store._db.in_transaction and store._db.defer_commit
    store._db.rollback()
    store._db.defer_commit = False
    assert list(store._db.iterdump()) == before


def test_procedure_tool_preference_is_visible_without_forced_injection():
    hit = {
        "id": "p",
        "memory_type": "procedure",
        "summary": "Use the preferred lookup tool",
        "score": 0.95,
        "extra_json": {"tool_requirement": "steam_mcp", "steps": []},
    }
    retriever = Retriever(None, None)
    block, ids = retriever.build_injection_block([hit])
    assert ids == ["p"] and "steam_mcp" in block
    hit["score"] = 0.1
    assert retriever.build_injection_block([hit]) == ("", [])
    hit["score"] = 0.95
    hit["extra_json"]["tool_requirement"] = "x" * 500
    assert Retriever(None, None, inject_max_chars=200).build_injection_block([hit]) == (
        "",
        [],
    )


@pytest.mark.parametrize(
    "order",
    [
        ("m2", "m3", "m4"),
        ("m2", "m4", "m3"),
        ("m3", "m2", "m4"),
        ("m3", "m4", "m2"),
        ("m4", "m2", "m3"),
        ("m4", "m3", "m2"),
    ],
)
def test_cross_session_undo_order_preserves_latest_surviving_source(store, order):
    last = insert(store, "A", "m1")
    for source, summary in (("m2", "B"), ("m3", "C"), ("m4", "D")):
        last = replace(store, last, summary, source)
    remaining = {"m1": "A", "m2": "B", "m3": "C", "m4": "D"}
    for source in order:
        remaining.pop(source)
        _undo_store_by_message_sources(store, [source])
        latest = remaining[max(remaining)]
        assert statuses(store) == {
            s: "active" if s == latest else "superseded" for s in "ABCD"
        }
