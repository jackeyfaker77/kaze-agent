import json
from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace

from proactive_v2.legacy_history import classify_legacy_deliveries
from session.store import SessionStore


def test_legacy_ledger_only_classifies_matching_successful_history(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    key = "desktop:daily"
    store.create_session(key=key, metadata={})
    now = datetime(2026, 10, 8, tzinfo=timezone.utc).isoformat()
    store.insert_message(
        key, seq=0, role="assistant", content="历史提醒\nUSED: one", ts=now
    )
    store.insert_message(key, seq=1, role="assistant", content="普通回复", ts=now)
    folder = tmp_path / "proactive_deliveries"
    folder.mkdir()
    (folder / f"{sha256(key.encode()).hexdigest()}.json").write_text(
        json.dumps(
            {
                "deliveries": [
                    {
                        "sent_at": now,
                        "message": "历史提醒",
                        "item_ids": ["one"],
                        "delivery_ref": "telegram:99",
                    },
                    {"sent_at": now, "message": "没有对应历史的回执"},
                ]
            }
        ),
        encoding="utf-8",
    )
    sessions = SimpleNamespace(workspace=tmp_path, _cache={})
    try:
        assert classify_legacy_deliveries(sessions) == 1
        assert classify_legacy_deliveries(sessions) == 0
        messages = store.fetch_messages_page(key)["messages"]
        assert len(messages) == 2
        assert (
            messages[0]["proactive"]
            and messages[0]["legacy_delivery_ref"] == "telegram:99"
        )
        assert not messages[1].get("proactive")
        assert messages[0]["content"] == "历史提醒\nUSED: one"
    finally:
        store.close()
