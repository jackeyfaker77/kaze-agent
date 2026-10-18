from datetime import timedelta
from dataclasses import replace

import pytest

from conftest import event, ics
from kaze_calendar.config import Settings
from kaze_calendar.store import Store


def test_subscription_lifecycle_and_secret_redaction(store, calendar):
    assert "url" not in store.manage("list")["calendars"][0]
    assert "private-secret" not in str(store.manage("list"))
    assert store.events("session1")
    store.manage("pause", source=calendar["id"])
    assert store.events("session1") == []
    store.manage("resume", source=calendar["id"])
    assert store.events("session1")
    store.manage("unsubscribe", source=calendar["id"])
    assert store.events("session1") == []


def test_pending_reads_do_not_consume_and_ack_is_per_consumer(store, calendar):
    first = store.events("session1")
    assert first == store.events("session1")
    version = first[0]["event_id"]
    assert store.acknowledge([version], "session1")["ok"]
    assert store.events("session1") == []
    assert store.events("session2")[0]["event_id"] == version
    assert store.acknowledge([version], "session1")["ok"]


def test_old_ack_cannot_consume_updated_calendar_or_todo(store, calendar):
    old = store.events("session1")[0]["event_id"]
    body = ics(event()).replace("会议".encode(), "更新会议".encode())
    assert store.save_sync(calendar, body=body)
    store.acknowledge([old], "session1")
    new = store.events("session1")[0]["event_id"]
    assert new != old
    assert store.validate([old, new], "session1")["valid_event_ids"] == [new]


def test_cancel_reschedule_and_complete_invalidate_pending_versions(store, calendar):
    old = store.events("session1")[0]["event_id"]
    assert store.save_sync(calendar, body=ics(event(extra="STATUS:CANCELLED")))
    assert store.validate([old], "session1")["valid_event_ids"] == []
    assert store.events("session1") == []
    assert store.save_sync(
        calendar, body=ics(event(start="20261017T090000Z", end="20261017T100000Z"))
    )
    assert store.events("session1") == []
    task = store.todo_create(
        "改简历",
        due_at="2026-10-16T17:00:00+08:00",
        reminder_at="2026-10-16T16:45:00+08:00",
    )
    token = store.events("session1")[0]["event_id"]
    store.todo_update(task["id"], {"status": "completed"})
    assert store.validate([token], "session1")["valid_event_ids"] == []


def test_stale_cache_is_visible_but_no_reminders_and_validation_is_retryable(
    store, calendar, clock
):
    store.settings = replace(store.settings, calendar_cache_ttl_seconds=300)
    token = store.events("session1")[0]["event_id"]
    clock.now += timedelta(minutes=5)
    assert store.events("session1") == []
    validation = store.validate([token], "session1")
    assert validation["unavailable_event_ids"] == [token]
    data = store.calendar_query("2026-10-16T00:00:00Z", "2026-10-17T00:00:00Z")
    assert data["items"][0]["fresh"] is False
    assert store.save_sync(calendar, body=calendar["body"])
    assert store.validate([token], "session1")["valid_event_ids"] == [token]


def test_expiry_boundary_and_unknown_ack_are_honest(store, calendar, clock):
    token = store.events("session1")[0]["event_id"]
    assert not store.acknowledge(["unknown"], "session1")["ok"]
    with pytest.raises(ValueError, match="delivery_ref"):
        store.acknowledge([token], "session1", "delivered")
    clock.now += timedelta(minutes=25)
    assert store.validate([token], "session1") == {
        "valid_event_ids": [],
        "unavailable_event_ids": [],
    }


def test_todos_deadline_change_reopen_and_persistent_ack(store, clock):
    task = store.todo_create("任务", due_at="2026-10-16T16:50:00+08:00")
    token = store.events("session1")[0]["event_id"]
    store.acknowledge([token], "session1")
    restarted = Store(store.settings, clock=clock)
    assert restarted.events("session1") == []
    assert restarted.todo_get(task["id"])["status"] == "open"
    restarted.todo_update(task["id"], {"status": "completed"})
    restarted.todo_update(task["id"], {"status": "open"})
    assert restarted.events("session1")[0]["event_id"] != token
    moved = restarted.todo_update(task["id"], {"due_at": "2026-10-17T16:50:00+08:00"})
    assert moved["reminder_at"] == moved["due_at"]
    assert restarted.events("session1") == []
    restarted.todo_update(task["id"], {"status": "deleted"})
    assert restarted.todo_list()["items"] == []
    assert restarted.todo_list("deleted")["total"] == 1


def test_no_due_todo_and_explicit_reminder_can_be_cleared(store):
    task = store.todo_create("无期限任务")
    assert store.events("session1") == []
    store.todo_update(task["id"], {"reminder_at": "2026-10-16T16:45:00+08:00"})
    assert len(store.events("session1")) == 1
    store.todo_update(task["id"], {"reminder_at": None})
    assert store.events("session1") == []


@pytest.mark.parametrize(
    "changes",
    [
        {"due_at": "2026-10-17T09:00:00"},
        {"title": ""},
        {"status": "unknown"},
        {"surprise": 1},
        {"reminder_at": "2026-10-18T09:00:00Z"},
    ],
)
def test_invalid_update_rolls_back(store, changes):
    task = store.todo_create("任务", due_at="2026-10-17T09:00:00Z")
    with pytest.raises(ValueError):
        store.todo_update(task["id"], changes)
    assert store.todo_get(task["id"]) == task


def test_sync_does_not_commit_after_subscription_paused_during_fetch(store, calendar):
    store.manage("pause", source=calendar["id"])
    assert not store.save_sync(calendar, body=ics(event()), etag="new")
    assert store.calendars(calendar["id"])[0]["etag"] == '"v1"'


@pytest.mark.parametrize(
    "value",
    [
        "file:///secret.ics",
        "https://u:pw@example.test/a.ics",
        "https://example.test/a#fragment",
        "not-a-url",
    ],
)
def test_invalid_subscription_urls(store, value):
    with pytest.raises(ValueError):
        store.manage("subscribe", name="invalid", url=value)


def test_config_validates_and_resolves_database_relative_to_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"database_path":"agenda.sqlite3"}', encoding="utf-8")
    assert Settings.load(path).database_path == str(tmp_path / "agenda.sqlite3")
    with pytest.raises(ValueError):
        Settings(poll_interval_seconds=True)


@pytest.mark.parametrize(
    "kwargs", [{"due_at": ""}, {"reminder_at": ""}, {"due_at": "2026-10-16T17:00:00"}]
)
def test_ambiguous_task_time_cannot_silently_disable_requested_reminder(store, kwargs):
    with pytest.raises(ValueError):
        store.todo_create("任务", **kwargs)
    assert store.todo_list()["total"] == 0
