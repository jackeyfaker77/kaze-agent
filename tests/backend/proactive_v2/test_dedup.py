from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from proactive_v2.dedup import DeliveryStore, MessageDeduper, reference_key

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


def test_successful_delivery_survives_restart_and_expires(tmp_path):
    store = DeliveryStore(tmp_path / "deliveries.json", hours=24)
    event = {
        "item_id": "news:1",
        "url": "https://EXAMPLE.org/article?utm_source=feed&id=2#section",
    }
    store.record("News", [event], "telegram:99", now=NOW)
    restarted = DeliveryStore(store.path, hours=24)
    recent = restarted.recent(now=NOW + timedelta(hours=23))
    assert restarted.matching_item(event, recent)["delivery_ref"] == "telegram:99"
    cross_source = {"item_id": "other:2", "url": "https://example.org/article?id=2"}
    assert restarted.matching_item(cross_source, recent) is not None
    assert restarted.is_duplicate("Paraphrased", [cross_source], recent)
    assert not restarted.recent(now=NOW + timedelta(hours=25))
    assert reference_key(
        {"url": "https://example.org/article?id=3", "item_id": "x"}
    ) != reference_key(event)


def test_empty_candidate_followup_uses_entire_normalized_message(tmp_path):
    store = DeliveryStore(tmp_path / "deliveries.json")
    store.record("How  are\nYOU?", [], "", now=NOW)
    recent = store.recent(now=NOW)
    assert store.is_duplicate("how are you?", [], recent)
    assert not store.is_duplicate("You have a new appointment", [], recent)


def test_changing_alert_state_is_not_deduplicated_by_its_dashboard_url(tmp_path):
    store = DeliveryStore(tmp_path / "deliveries.json")
    first = {
        "kind": "alert",
        "item_id": "sensor:low",
        "url": "https://example.org/dashboard",
    }
    changed = {**first, "item_id": "sensor:critical"}
    store.record("Low battery", [first], "", now=NOW)
    assert store.matching_item(changed, store.recent(now=NOW)) is None


def test_corrupt_ledger_does_not_silently_disable_dedup(tmp_path):
    path = tmp_path / "deliveries.json"
    path.write_text('{"deliveries":[{"sent_at":"bad"}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="timestamp"):
        DeliveryStore(path).recent(now=NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,expected",
    [
        ('{"is_duplicate":true}', True),
        ('{"is_duplicate":false}', False),
        ('{"is_duplicate":"false"}', False),
        ("malformed JSON", False),
    ],
)
async def test_semantic_dedup_requires_explicit_boolean(response, expected):
    provider = SimpleNamespace(
        chat=AsyncMock(return_value=SimpleNamespace(content=response))
    )
    deduper = MessageDeduper(provider, "light-model")
    recent = [{"sent_at": NOW.isoformat(), "message": "Take a break"}]
    assert await deduper.is_duplicate("Remember to rest", recent) is expected
    assert provider.chat.await_args.kwargs["tools"] == []
    assert provider.chat.await_args.kwargs["model"] == "light-model"


@pytest.mark.asyncio
async def test_no_recent_delivery_needs_no_semantic_request():
    provider = SimpleNamespace(chat=AsyncMock())
    assert not await MessageDeduper(provider, "light").is_duplicate(
        "First notification", []
    )
    provider.chat.assert_not_awaited()
