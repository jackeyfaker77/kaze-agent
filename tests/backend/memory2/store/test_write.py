from contextlib import closing

from memory2.store import MemoryStore2


def test_upsert_item_does_not_reactivate_superseded_memory_after_reopen(tmp_path):
    path = tmp_path / "memory2.db"
    with closing(MemoryStore2(path)) as store:
        item_id = store.upsert_item(
            "profile", "你住在上海", embedding=None, source_ref="old-source"
        ).split(":", 1)[1]
        store.mark_superseded(item_id)
        retired = store.get_item_for_admin(item_id)

    with closing(MemoryStore2(path)) as store:
        for source_ref in ("old-source", "another-source"):
            assert store.upsert_item(
                "profile",
                "你住在上海",
                embedding=None,
                source_ref=source_ref,
                emotional_weight=9,
            ) == f"skipped:{item_id}"
            assert store.get_item_for_admin(item_id) == retired
        assert [item["id"] for item in store.list_by_type("profile")] == [item_id]


def test_consolidation_records_skipped_source_without_reactivating_memory(tmp_path):
    path = tmp_path / "memory2.db"
    with closing(MemoryStore2(path)) as store:
        item_id = store.upsert_consolidation_event(
            source_ref="old-source", summary="旧活动计划", embedding=None
        ).split(":", 1)[1]
        store.mark_superseded(item_id)
        retired = store.get_item_for_admin(item_id)

    with closing(MemoryStore2(path)) as store:
        assert store.upsert_consolidation_event(
            source_ref="another-source",
            summary="旧活动计划",
            embedding=None,
            happened_at="2026-11-01T12:00:00",
            emotional_weight=9,
        ) == f"skipped:{item_id}"
        assert store.get_item_for_admin(item_id) == retired
        assert store.has_consolidation_source_ref("another-source")
        assert store.upsert_consolidation_event(
            source_ref="another-source", summary="不同的重放文本", embedding=None
        ) == f"skipped:{item_id}"
        assert [item["id"] for item in store.list_by_type("event")] == [item_id]
        assert store.get_item_for_admin(item_id) == retired
