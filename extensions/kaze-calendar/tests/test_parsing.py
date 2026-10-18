from datetime import timedelta

import pytest

from conftest import event, ics
from kaze_calendar.models import timestamp
from kaze_calendar.parsing import occurrences


def parse(body, start="2026-10-16T00:00:00Z", end="2026-10-21T00:00:00Z"):
    return occurrences(
        body,
        "calendar1",
        "工作",
        "Asia/Shanghai",
        timestamp(start),
        timestamp(end),
        15,
        9,
    )


def test_timed_event_revision_and_nonrecurring_identity_survive_reschedule():
    first = parse(ics(event()))[0]
    later = parse(ics(event(start="20261016T110000Z", end="20261016T120000Z")))[0]
    assert first["start_at"] == "2026-10-16T09:00:00+00:00"
    assert first["reminder_at"] == "2026-10-16T08:45:00+00:00"
    assert first["item_id"] == later["item_id"]
    assert first["revision"] != later["revision"]
    assert first["revision"] == parse(ics(event()))[0]["revision"]


def test_recurring_exclusions_cancellation_and_instance_move():
    body = ics(
        event(extra="RRULE:FREQ=DAILY;COUNT=4\r\nEXDATE:20261017T090000Z"),
        event(
            start="20261018T110000Z",
            end="20261018T120000Z",
            extra="RECURRENCE-ID:20261018T090000Z",
        ),
        event(
            start="20261019T090000Z",
            end="20261019T100000Z",
            extra="RECURRENCE-ID:20261019T090000Z\r\nSTATUS:CANCELLED",
        ),
    )
    rows = parse(body)
    assert [row["start_at"] for row in rows] == [
        "2026-10-16T09:00:00+00:00",
        "2026-10-18T11:00:00+00:00",
    ]
    old = parse(ics(event(extra="RRULE:FREQ=DAILY;COUNT=4")))
    assert rows[1]["item_id"] == old[2]["item_id"]
    assert len({row["item_id"] for row in old}) == 4


def test_all_day_uses_calendar_timezone_and_daytime_reminder():
    body = ics(
        "BEGIN:VEVENT\r\nUID:birthday\r\nDTSTART;VALUE=DATE:20261016\r\nDTEND;VALUE=DATE:20261017\r\nSUMMARY:生日\r\nEND:VEVENT",
        timezone="Asia/Shanghai",
    )
    row = parse(body)[0]
    assert row["all_day"]
    assert row["start_at"] == "2026-10-15T16:00:00+00:00"
    assert row["reminder_at"] == "2026-10-16T01:00:00+00:00"
    assert row["end_at"] == "2026-10-16T16:00:00+00:00"


def test_dst_preserves_recurring_wall_clock_time():
    body = ics(
        "BEGIN:VEVENT\r\nUID:dst\r\nDTSTART;TZID=America/New_York:20261031T090000\r\nDTEND;TZID=America/New_York:20261031T100000\r\nRRULE:FREQ=DAILY;COUNT=3\r\nSUMMARY:晨会\r\nEND:VEVENT",
        timezone="America/New_York",
    )
    rows = parse(body, "2026-10-31T00:00:00Z", "2026-11-03T00:00:00Z")
    times = [timestamp(row["start_at"]) for row in rows]
    assert [dt.hour for dt in times] == [13, 14, 14]
    assert times[1] - times[0] == timedelta(hours=25)


def test_floating_time_uses_configured_timezone():
    body = ics(
        event(start="20261016T170000", end="20261016T180000"), timezone="Asia/Shanghai"
    )
    assert parse(body)[0]["start_at"] == "2026-10-16T09:00:00+00:00"


@pytest.mark.parametrize(
    "body",
    [
        b"not a calendar",
        ics(event(extra="RRULE:FREQ=SECONDLY")),
        ics(event(extra="RRULE:FREQ=MINUTELY")),
        ics(event()).replace(b"UID:meeting\r\n", b""),
        ics(event(start="bad-date")),
        ics(event()).replace(b"DTSTART:", b"DTSTART;TZID=Not/AZone:"),
    ],
)
def test_invalid_or_excessive_calendar_is_rejected(body):
    with pytest.raises((ValueError, TypeError, KeyError)):
        parse(body)


def test_end_range_is_exclusive_and_cancelled_series_has_no_occurrences():
    assert parse(ics(event()), end="2026-10-16T09:00:00Z") == []
    assert parse(ics(event(extra="STATUS:CANCELLED\r\nRRULE:FREQ=DAILY;COUNT=2"))) == []


@pytest.mark.parametrize(
    "property_line",
    [
        "DTSTART:20261016T090000Z",
        "DTEND:20261016T100000Z",
        "RECURRENCE-ID:20261016T090000Z",
        "RRULE:FREQ=DAILY;COUNT=2",
    ],
)
def test_repeated_singleton_properties_raise_validation_error(property_line):
    body = ics(event(extra=f"{property_line}\r\n{property_line}"))
    with pytest.raises(ValueError):
        parse(body)
