from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from icalendar import Calendar
import recurring_ical_events

from .models import calendar_time, digest, iso

MAX_BYTES = 2 * 1024 * 1024
MAX_COMPONENTS = 2000
MAX_OCCURRENCES = 5000


def load_calendar(body: bytes, default_timezone: str) -> Calendar:
    if len(body) > MAX_BYTES:
        raise ValueError("ICS exceeds 2 MiB")
    calendar = Calendar.from_ical(body)
    if calendar.name != "VCALENDAR" or calendar.errors:
        raise ValueError("Invalid VCALENDAR")
    if not calendar.get("X-WR-TIMEZONE"):
        calendar.add("X-WR-TIMEZONE", default_timezone)
    ZoneInfo(str(calendar.get("X-WR-TIMEZONE")))
    events = calendar.walk("VEVENT")
    if len(events) > MAX_COMPONENTS:
        raise ValueError("Calendar exceeds 2000 VEVENT components")
    for event in events:
        if event.errors or not event.get("UID"):
            raise ValueError("Every VEVENT needs a valid UID and dates")
        for name in ("UID", "DTSTART", "DTEND", "RECURRENCE-ID", "RRULE"):
            if isinstance(event.get(name), list):
                raise ValueError(f"Repeated {name}")
        if not event.get("DTSTART"):
            if str(event.get("STATUS", "")).upper() == "CANCELLED":
                event.add(
                    "DTSTART",
                    event.decoded("RECURRENCE-ID")
                    if event.get("RECURRENCE-ID")
                    else datetime(1970, 1, 1, tzinfo=UTC),
                )
            else:
                raise ValueError("Every active VEVENT needs DTSTART")
        for name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
            prop = event.get(name)
            if prop is None:
                continue
            if not isinstance(getattr(prop, "dt", None), (date, datetime)):
                raise ValueError(f"Invalid {name}")
            if prop.params.get("TZID") and prop.to_ical().endswith(b"Z"):
                raise ValueError(f"TZID must not be combined with a UTC {name}")
            # Unknown TZIDs must not silently become floating local time.
            if (
                prop.params.get("TZID")
                and isinstance(prop.dt, datetime)
                and prop.dt.tzinfo is None
            ):
                raise ValueError(f"Unresolved timezone in {name}")
        rule = event.get("RRULE")
        if rule and str(rule.get("FREQ", [""])[0]).upper() in {"SECONDLY", "MINUTELY"}:
            raise ValueError("SECONDLY/MINUTELY calendar recurrence is unsupported")
    return calendar


def occurrences(
    body: bytes,
    source_id: str,
    source_name: str,
    timezone: str,
    start: datetime,
    end: datetime,
    reminder_minutes: int,
    all_day_reminder_hour: int,
) -> list[dict]:
    calendar = load_calendar(body, timezone)
    zone = ZoneInfo(str(calendar.get("X-WR-TIMEZONE")))
    recurring = {
        str(event.get("UID"))
        for event in calendar.walk("VEVENT")
        if event.get("RRULE") or event.get("RDATE") or event.get("RECURRENCE-ID")
    }
    results = []
    query = recurring_ical_events.of(calendar, keep_recurrence_attributes=True)
    # Generator + hard cap avoids materializing unbounded recurrence expansions.
    for index, event in enumerate(query.after(start.astimezone(zone))):
        if index >= MAX_OCCURRENCES:
            raise ValueError(
                "Calendar query exceeds 5000 occurrences; narrow the date range"
            )
        begins_raw = event.decoded("DTSTART")
        begins = calendar_time(begins_raw, zone)
        if begins >= end:
            break
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue
        all_day = not isinstance(begins_raw, datetime)
        ends = calendar_time(event.decoded("DTEND"), zone)
        if ends < begins:
            raise ValueError("Calendar event ends before it begins")
        if ends <= start and begins < start:
            continue
        uid = str(event.get("UID"))
        recurrence = event.get("RECURRENCE-ID")
        occurrence_key = (
            iso(calendar_time(recurrence.dt if recurrence else begins_raw, zone))
            if uid in recurring
            else ""
        )
        item_id = "cal_" + digest([source_id, uid, occurrence_key])
        reminder_at = (
            calendar_time(
                datetime.combine(begins_raw, datetime.min.time()).replace(
                    hour=all_day_reminder_hour, tzinfo=zone
                ),
                zone,
            )
            if all_day
            else begins - timedelta(minutes=reminder_minutes)
        )
        result = {
            "item_id": item_id,
            "type": "calendar",
            "calendar_id": source_id,
            "source_name": source_name,
            "uid": uid,
            "occurrence": occurrence_key,
            "title": str(event.get("SUMMARY", "（无标题日程）"))[:500],
            "notes": str(event.get("DESCRIPTION", ""))[:8000],
            "location": str(event.get("LOCATION", ""))[:1000],
            "start_at": iso(begins),
            "end_at": iso(ends),
            "all_day": all_day,
            "timezone": str(zone),
            "reminder_at": iso(reminder_at),
        }
        result["revision"] = digest(result)
        results.append(result)
    return sorted(results, key=lambda row: (row["start_at"], row["item_id"]))
