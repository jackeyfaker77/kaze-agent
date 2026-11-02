"""Behavior contracts for ordered retrieval and explicit memory replacement."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.tools.memorize import MemorizeTool
from core.memory.engine import MemoryMutation, MemoryQuery, MemoryQueryFilters, MemoryScope
from memory2.memorizer import Memorizer
from memory2.retriever import Retriever
from memory2.store import MemoryStore2
from plugins.default_memory.config import (
    load_default_memory_config,
    render_default_memory_config,
)
from plugins.default_memory.engine import DefaultMemoryEngine


@pytest.fixture
def store(tmp_path):
    value = MemoryStore2(tmp_path / "memory.db", vec_dim=2)
    yield value
    value.close()


def hit(id, kind="preference", score=0.9, **values):
    return {
        "id": id,
        "memory_type": kind,
        "summary": f"memory {id}",
        "score": score,
        **values,
    }


def engine(store, embedder=None):
    value = DefaultMemoryEngine.__new__(DefaultMemoryEngine)
    value._v2_store = store
    value._memorizer = Memorizer(
        store, embedder or SimpleNamespace(embed=AsyncMock(return_value=[1.0, 0.0]))
    )
    value._retriever = Retriever(
        store, SimpleNamespace(embed=AsyncMock(return_value=[1.0, 0.0]))
    )
    value._tagger = None
    value._light_provider = SimpleNamespace(
        chat=AsyncMock(side_effect=AssertionError("unexpected LLM call"))
    )
    return value


async def test_rrf_order_survives_mixed_types_into_prompt():
    retriever = Retriever(None, None)
    a, b = hit("a", "procedure", 0.99), hit("b", "event", 0.8)
    retriever._retrieve_vector_lanes = AsyncMock(return_value=[a, b])
    retriever._retrieve_keyword_lane = lambda *args, **kwargs: [b]
    items = await retriever.retrieve("query")
    assert [i["id"] for i in items] == ["b", "a"]
    block, ids = retriever.build_injection_block(items)
    assert ids == ["b", "a"]
    assert block.index("[b]") < block.index("[a]")


def test_procedure_has_no_threshold_or_budget_exemption():
    retriever = Retriever(
        None, None, score_thresholds={"procedure": 0.66}, inject_max_chars=220
    )
    low = hit("low", "procedure", 0.65, extra_json={"tool_requirement": "shell"})
    huge = hit(
        "huge",
        "procedure",
        0.99,
        summary="x" * 300,
        extra_json={"tool_requirement": "shell"},
    )
    good = hit("good", "event", 0.9)
    block, ids = retriever.build_injection_block([low, huge, good])
    assert ids == ["good"]
    assert len(block) <= 220
    assert "low" not in block and "huge" not in block


def test_injection_filters_retired_and_duplicate_hits_without_reordering():
    retriever = Retriever(None, None)
    block, ids = retriever.build_injection_block(
        [
            hit("retired", status="superseded"),
            hit("b"),
            hit("a"),
            hit("b"),
        ]
    )
    assert ids == ["b", "a"]
    assert "retired" not in block and block.count("[b]") == 1


async def test_explicit_recall_searches_actual_question_without_auxiliary_llm(store):
    value = engine(store)
    value._retriever = SimpleNamespace(retrieve=AsyncMock(return_value=[hit("a")]))
    result = await value.query(MemoryQuery(text="我的项目叫什么？", intent="answer"))
    assert result.records[0].id == "a"
    value._light_provider.chat.assert_not_called()
    call = value._retriever.retrieve.await_args
    assert call.args == ("我的项目叫什么？",)
    assert call.kwargs["aux_queries"] is None


@pytest.mark.parametrize(
    "kind,extra",
    [
        ("preference", {}),
        ("profile", {"category": "status"}),
        ("profile", {"category": "purchase"}),
        ("procedure", {"tool_requirement": "shell"}),
    ],
)
async def test_identical_embeddings_do_not_merge_or_retire_distinct_memories(
    store, kind, extra
):
    memorizer = Memorizer(
        store, SimpleNamespace(embed=AsyncMock(return_value=[1.0, 0.0]))
    )
    first = await memorizer.save_item_with_supersede(
        "工作时喜欢安静", kind, extra, "user:1"
    )
    second = await memorizer.save_item_with_supersede(
        "运动时喜欢听音乐", kind, extra, "user:2"
    )
    rows = store.get_items_by_ids([first.split(":")[1], second.split(":")[1]])
    assert [r["status"] for r in rows] == ["active", "active"]
    assert rows[0]["summary"] == "工作时喜欢安静"
    assert store.list_replacements() == []


async def test_tool_replacement_records_relation_and_old_content_stays_retired_on_replay(
    store,
):
    value = engine(store)
    tool = MemorizeTool(value, value.tool_profile().memorize)
    old = json.loads(
        await tool.execute(
            "我住大阪",
            memory_kind="profile",
            current_user_source_ref="user:1",
            channel="telegram",
            chat_id="a",
            session_key="telegram:a",
        )
    )["item_id"]
    args = dict(
        memory_kind="profile",
        supersedes=[old],
        current_user_source_ref="user:2",
        channel="telegram",
        chat_id="a",
        session_key="telegram:a",
    )
    new = json.loads(await tool.execute("我从大阪搬到东京", **args))
    assert new["status"] == "replaced"
    relation = store.list_replacements()
    assert len(relation) == 1
    assert relation[0]["old_item_id"] == old
    assert relation[0]["new_item_id"] == new["item_id"]
    assert relation[0]["source_ref"] == "user:2"
    before = list(store._db.iterdump())
    assert json.loads(await tool.execute("我从大阪搬到东京", **args)) == new
    assert list(store._db.iterdump()) == before
    replay = await value._memorizer.save_item_with_supersede(
        "我住大阪", "profile", {}, "user:1"
    )
    assert replay == f"skipped:{old}"
    hits = await value._retriever.retrieve("住址", score_threshold=0)
    assert old not in [i["id"] for i in hits]
    block, ids = value._retriever.build_injection_block(hits)
    assert old not in ids and "我住大阪" not in block


@pytest.mark.parametrize(
    "extra",
    [
        {"role_id": "other"},
        {"memory_domain": "private"},
    ],
)
async def test_cross_scope_replacement_rejected_before_any_store_change(store, extra):
    old = store.upsert_item("preference", "old", [1.0, 0.0], extra=extra).split(":")[1]
    memorizer = Memorizer(
        store, SimpleNamespace(embed=AsyncMock(return_value=[1.0, 0.0]))
    )
    before = list(store._db.iterdump())
    with pytest.raises(ValueError, match="scope mismatch"):
        await memorizer.save_item_with_supersede(
            "new", "preference", {}, "user:2", supersedes=[old]
        )
    assert list(store._db.iterdump()) == before


def test_replacement_failure_rolls_back_new_item_relation_and_old_status(
    store, monkeypatch
):
    old = store.upsert_item("preference", "old", [1.0, 0.0]).split(":")[1]
    before = list(store._db.iterdump())
    original = store.mark_superseded_batch

    def fail(ids):
        original(ids)
        raise RuntimeError("failure after retirement")

    monkeypatch.setattr(store, "mark_superseded_batch", fail)
    with pytest.raises(RuntimeError, match="failure after retirement"):
        store.replace_items(
            memory_type="preference",
            summary="new",
            embedding=[1.0, 0.0],
            source_ref="user:2",
            extra={},
            supersedes=[old],
        )
    assert list(store._db.iterdump()) == before
    assert not store._db.defer_commit and not store._db.in_transaction


def test_replacement_respects_outer_task_transaction(store):
    old = store.upsert_item("preference", "old", [1.0, 0.0]).split(":")[1]
    before = list(store._db.iterdump())
    store._db.execute("BEGIN IMMEDIATE")
    store._db.defer_commit = True
    store.replace_items(
        memory_type="preference",
        summary="new",
        embedding=[1.0, 0.0],
        source_ref="user:2",
        extra={},
        supersedes=[old],
    )
    assert store._db.defer_commit and store._db.in_transaction
    store._db.rollback()
    store._db.defer_commit = False
    assert list(store._db.iterdump()) == before


def test_replacement_and_task_receipt_survive_commit_before_crash(store, monkeypatch):
    from core.memory.task_context import memory_task
    from memory2 import durable_tasks

    store._db.execute(
        "CREATE TABLE memory_task_steps ("
        "job TEXT, path TEXT, kind TEXT, result TEXT, input_digest TEXT,"
        "PRIMARY KEY(job, path))"
    )
    store._db.commit()
    old = store.upsert_item("preference", "old", [1.0, 0.0]).split(":")[1]
    task = durable_tasks.Task(
        SimpleNamespace(store=store, db=store._db), {"id": "correction"}
    )

    def crash(point):
        if point == "after_memory_commit":
            raise RuntimeError("crash after committed receipt")

    monkeypatch.setattr(durable_tasks, "fault", crash)
    token = memory_task.set(task)
    try:
        for attempt in range(2):
            frame = durable_tasks._frame.set(("", [0]))
            try:
                kwargs = dict(
                    memory_type="preference",
                    summary="new",
                    embedding=[1.0, 0.0],
                    source_ref="user:2",
                    extra={},
                    supersedes=[old],
                )
                if attempt == 0:
                    with pytest.raises(RuntimeError, match="committed receipt"):
                        store.replace_items(**kwargs)
                    before_replay = list(store._db.iterdump())
                else:
                    assert store.replace_items(**kwargs).startswith("replaced:")
                    assert list(store._db.iterdump()) == before_replay
            finally:
                durable_tasks._frame.reset(frame)
    finally:
        memory_task.reset(token)
    assert len(store.list_replacements()) == 1
    assert store.get_items_by_ids([old])[0]["status"] == "superseded"
    assert store._db.execute("SELECT count(*) FROM memory_task_steps").fetchone() == (
        1,
    )


@pytest.mark.parametrize(
    "ids", [["missing"], ["notebook:1"], [""], list(map(str, range(21)))]
)
def test_invalid_replacement_targets_leave_store_unchanged(store, ids):
    before = list(store._db.iterdump())
    with pytest.raises(ValueError):
        store.replace_items(
            memory_type="preference",
            summary="new",
            embedding=[1.0, 0.0],
            source_ref="user:2",
            extra={},
            supersedes=ids,
        )
    assert list(store._db.iterdump()) == before


def test_replacement_never_revives_a_retired_new_target(store):
    old = store.upsert_item("preference", "old", [1.0, 0.0]).split(":")[1]
    new = store.upsert_item("preference", "new", [1.0, 0.0]).split(":")[1]
    store.mark_superseded(new)
    before = list(store._db.iterdump())
    result = store.replace_items(
        memory_type="preference",
        summary="new",
        embedding=[1.0, 0.0],
        source_ref="user:2",
        extra={},
        supersedes=[old],
    )
    assert result == f"skipped:{new}"
    assert list(store._db.iterdump()) == before


async def test_explicit_forget_is_not_undo_of_replacement(store):
    value = engine(store)
    old = store.upsert_item("preference", "old", [1.0, 0.0]).split(":")[1]
    result = store.replace_items(
        memory_type="preference",
        summary="new",
        embedding=[1.0, 0.0],
        source_ref="user:2",
        extra={},
        supersedes=[old],
    )
    new = result.split(":")[1]
    await value.mutate(MemoryMutation(kind="forget", ids=(new,), scope=MemoryScope()))
    assert [i["status"] for i in store.get_items_by_ids([old, new])] == [
        "superseded",
        "superseded",
    ]


def test_removed_config_keys_are_ignored_and_not_reemitted(tmp_path):
    (tmp_path / "config.local.toml").write_text(
        "[retrieval]\nrelative_delta=0.8\nprocedure_guard_enabled=true\n"
        "[retrieval.inject]\nline_max=90\nforced=9\nmax_chars=3000\n",
        encoding="utf-8",
    )
    config = load_default_memory_config(plugin_dir=tmp_path)
    assert config.retrieval.inject.max_chars == 3000
    rendered = render_default_memory_config(config)
    for key in ("relative_delta", "line_max", "procedure_guard_enabled", "forced"):
        assert key not in rendered


@pytest.mark.parametrize("reuse_existing_target", [False, True])
def test_replacement_rejects_retired_old_target_without_prior_relation(
    store, reuse_existing_target
):
    old = store.upsert_item("preference", "old", [1.0, 0.0]).split(":")[1]
    store.mark_superseded(old)
    if reuse_existing_target:
        store.upsert_item("preference", "new", [1.0, 0.0])
    before = list(store._db.iterdump())

    with pytest.raises(ValueError, match="Replacement target is no longer active"):
        store.replace_items(
            memory_type="preference",
            summary="new",
            embedding=[1.0, 0.0],
            source_ref="user:2",
            extra={},
            supersedes=[old],
        )

    # No new item, reinforcement, relationship, or status change on rejection.
    assert list(store._db.iterdump()) == before
    assert not store._db.defer_commit and not store._db.in_transaction


async def test_context_expansion_is_opt_in_and_does_not_generate_queries(store):
    primary = store.upsert_item("preference", "primary record", [1.0, 0.0]).split(":")[1]
    auxiliary = store.upsert_item("preference", "auxiliary record", [0.0, 1.0]).split(":")[1]
    vectors = {"original topic": [1.0, 0.0], "additional topic": [0.0, 1.0]}
    embedder = SimpleNamespace(embed=AsyncMock(side_effect=lambda text: vectors[text]))
    value = engine(store)
    value._retriever = Retriever(store, embedder, score_threshold=0.8)

    ordinary = await value.query(MemoryQuery(text="original topic", intent="context"))
    assert [record.id for record in ordinary.records] == [primary]
    embedder.embed.reset_mock()
    expanded = await value.query(
        MemoryQuery(
            text="original topic",
            intent="context",
            filters=MemoryQueryFilters(
                hints={"queries": ["original topic", "additional topic", "additional topic"]}
            ),
        )
    )

    assert {record.id for record in expanded.records} == {primary, auxiliary}
    assert [call.args[0] for call in embedder.embed.await_args_list] == [
        "original topic", "additional topic"
    ]
    value._light_provider.chat.assert_not_called()
