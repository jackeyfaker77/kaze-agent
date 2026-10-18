from datetime import UTC, datetime

import pytest

from kaze_calendar.config import Settings
from kaze_calendar.store import Store


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 16, 8, 50, tzinfo=UTC)

    def __call__(self):
        return self.now


def ics(*events, timezone="UTC"):
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Kaze Tests//EN\r\n"
        + f"X-WR-TIMEZONE:{timezone}\r\n"
        + "\r\n".join(events)
        + "\r\nEND:VCALENDAR\r\n"
    ).encode()


def event(uid="meeting", start="20261016T090000Z", end="20261016T100000Z", extra=""):
    return (
        f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTART:{start}\r\nDTEND:{end}\r\n"
        + "SUMMARY:会议\r\n"
        + extra
        + "\r\nEND:VEVENT"
    )


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return Store(Settings(database_path=str(tmp_path / "agenda.sqlite3")), clock=clock)


@pytest.fixture
def calendar(store):
    row = store.manage(
        "subscribe", name="工作", url="https://example.test/private-secret.ics"
    )["calendar"]
    row = store.calendars(row["id"])[0]
    assert store.save_sync(row, body=ics(event()), etag='"v1"')
    return store.calendars(row["id"])[0]
