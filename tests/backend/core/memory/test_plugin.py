from core.memory.plugin import DisabledMemoryEngine


def test_disabled_memory_engine_deletes_no_memories():
    engine = DisabledMemoryEngine()
    assert engine.delete_item("missing") is False
    assert engine.delete_items_batch(["missing"]) == 0
