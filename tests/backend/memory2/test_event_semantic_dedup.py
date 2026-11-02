from typing import Any, cast
import asyncio
from datetime import datetime, timedelta, timezone

from memory2.memorizer import Memorizer
from memory2.store import MemoryStore2


class _FakeEmbedder:
    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self._mapping = mapping

    async def embed(self, text: str) -> list[float]:
        return list(self._mapping[text])


def test_same_event_text_at_distinct_times_keeps_both_occurrences(tmp_path):
    store = MemoryStore2(tmp_path / "memory2.db", vec_dim=2)
    try:
        def record(source, date):
            return store.commit_change('archive_event', memory_type='event', summary='review meeting',
                embedding=[1., 0.], source_ref=source, happened_at=date)
        first = record('one', '2026-10-01').split(':')[1]
        second = record('two', '2026-10-02').split(':')[1]
        assert first != second
        record('two', '2026-10-02')
        assert store._db.execute('SELECT happened_at,reinforcement FROM memory_items ORDER BY happened_at').fetchall() == [
            ('2026-10-01', 1), ('2026-10-02', 1)]
    finally:
        store.close()


def test_similar_descriptions_do_not_establish_same_occurrence(tmp_path):
    store = MemoryStore2(tmp_path / "memory2.db")
    embedder = _FakeEmbedder(
        {
            "用户把仓库脱敏后公开发布": [1.0, 0.0],
            "用户公开了脱敏后的仓库": [0.99, 0.01],
        }
    )
    memorizer = Memorizer(store, cast(Any, embedder))

    async def _run() -> None:
        await memorizer.save_from_consolidation(
            history_entry="用户把仓库脱敏后公开发布",
            behavior_updates=[],
            source_ref="session@1-10#0",
            scope_channel="telegram",
            scope_chat_id="1",
        )
        await memorizer.save_from_consolidation(
            history_entry="用户公开了脱敏后的仓库",
            behavior_updates=[],
            source_ref="session@1-10#1",
            scope_channel="telegram",
            scope_chat_id="1",
        )

    asyncio.run(_run())

    items = store.list_by_type("event")
    assert len(items) == 2


def test_distinct_event_saves_normally(tmp_path):
    store = MemoryStore2(tmp_path / "memory2.db")
    embedder = _FakeEmbedder(
        {
            "用户把仓库脱敏后公开发布": [1.0, 0.0],
            "用户买了一个新键盘": [0.0, 1.0],
        }
    )
    memorizer = Memorizer(store, cast(Any, embedder))

    async def _run() -> None:
        await memorizer.save_from_consolidation(
            history_entry="用户把仓库脱敏后公开发布",
            behavior_updates=[],
            source_ref="session@1-10#0",
            scope_channel="telegram",
            scope_chat_id="1",
        )
        await memorizer.save_from_consolidation(
            history_entry="用户买了一个新键盘",
            behavior_updates=[],
            source_ref="session@1-10#1",
            scope_channel="telegram",
            scope_chat_id="1",
        )

    asyncio.run(_run())

    items = store.list_by_type("event")
    assert len(items) == 2


def test_vector_similarity_does_not_reinforce(tmp_path):
    store = MemoryStore2(tmp_path / "memory2.db")
    embedder = _FakeEmbedder(
        {
            "用户把仓库脱敏后公开发布": [1.0, 0.0],
            "用户公开了脱敏后的仓库": [0.99, 0.01],
        }
    )
    memorizer = Memorizer(store, cast(Any, embedder))

    async def _run() -> None:
        await memorizer.save_from_consolidation(
            history_entry="用户把仓库脱敏后公开发布",
            behavior_updates=[],
            source_ref="session@1-10#0",
            scope_channel="telegram",
            scope_chat_id="1",
        )
        await memorizer.save_from_consolidation(
            history_entry="用户公开了脱敏后的仓库",
            behavior_updates=[],
            source_ref="session@1-10#1",
            scope_channel="telegram",
            scope_chat_id="1",
        )

    asyncio.run(_run())

    items = store.list_by_type("event")
    assert [i["reinforcement"] for i in items] == [1, 1]


def test_similar_occurrences_keep_their_own_weights(tmp_path):
    store = MemoryStore2(tmp_path / "memory2.db")
    embedder = _FakeEmbedder(
        {
            "用户把仓库脱敏后公开发布": [1.0, 0.0],
            "用户公开了脱敏后的仓库": [0.99, 0.01],
        }
    )
    memorizer = Memorizer(store, cast(Any, embedder))

    async def _run() -> None:
        await memorizer.save_from_consolidation(
            history_entry="用户把仓库脱敏后公开发布",
            behavior_updates=[],
            source_ref="session@1-10#0",
            scope_channel="telegram",
            scope_chat_id="1",
            emotional_weight=0,
        )
        await memorizer.save_from_consolidation(
            history_entry="用户公开了脱敏后的仓库",
            behavior_updates=[],
            source_ref="session@1-10#1",
            scope_channel="telegram",
            scope_chat_id="1",
            emotional_weight=8,
        )

    asyncio.run(_run())

    items = store.list_by_type("event")
    assert sorted(i["emotional_weight"] for i in items) == [0, 8]


def test_old_event_is_also_kept(tmp_path):
    store = MemoryStore2(tmp_path / "memory2.db")
    embedder = _FakeEmbedder(
        {
            "用户把仓库脱敏后公开发布": [1.0, 0.0],
            "用户公开了脱敏后的仓库": [0.99, 0.01],
        }
    )
    memorizer = Memorizer(store, cast(Any, embedder))

    async def _run() -> None:
        await memorizer.save_from_consolidation(
            history_entry="用户把仓库脱敏后公开发布",
            behavior_updates=[],
            source_ref="session@1-10#0",
            scope_channel="telegram",
            scope_chat_id="1",
        )

    asyncio.run(_run())

    old_created_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    store._db.execute(
        "UPDATE memory_items SET created_at=?, updated_at=? WHERE memory_type='event'",
        (old_created_at, old_created_at),
    )
    store._db.commit()

    async def _run_again() -> None:
        await memorizer.save_from_consolidation(
            history_entry="用户公开了脱敏后的仓库",
            behavior_updates=[],
            source_ref="session@1-10#1",
            scope_channel="telegram",
            scope_chat_id="1",
        )

    asyncio.run(_run_again())

    items = store.list_by_type("event")
    assert len(items) == 2
