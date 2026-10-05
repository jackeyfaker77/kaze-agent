from memory2.store import MemoryStore2


def test_delete_item_uses_explicit_id_without_deleting_other_memories(tmp_path):
    store = MemoryStore2(tmp_path / "memory2.db")
    try:
        first = store.upsert_item("preference", "你喜欢拿铁", embedding=None).split(":", 1)[1]
        second = store.upsert_item("preference", "你喜欢红茶", embedding=None).split(":", 1)[1]
        assert store.delete_item(first) is True
        assert store.get_item_for_admin(first) is None
        assert store.get_item_for_admin(second)["status"] == "active"
    finally:
        store.close()


def test_delete_unknown_and_empty_batch_are_noops(tmp_path):
    store = MemoryStore2(tmp_path / "memory2.db")
    try:
        assert store.delete_item("missing") is False
        assert store.delete_items_batch([]) == 0
    finally:
        store.close()
