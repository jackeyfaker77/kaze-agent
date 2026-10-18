from contextlib import contextmanager
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

from .config import Settings
from .models import digest, iso, subscription_url, text, timestamp, utc_now
from .parsing import occurrences


class Store:
    def __init__(self, settings: Settings, *, clock=utc_now):
        self.settings = settings
        self.clock = clock
        Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS calendars (
                    id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                    url TEXT UNIQUE NOT NULL, timezone TEXT NOT NULL,
                    reminder_minutes INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    config_version INTEGER NOT NULL DEFAULT 1,
                    body BLOB, etag TEXT NOT NULL DEFAULT '',
                    last_modified TEXT NOT NULL DEFAULT '', last_checked_at TEXT,
                    last_success_at TEXT, last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS todos (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, notes TEXT NOT NULL,
                    due_at TEXT, reminder_at TEXT, status TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alert_versions (
                    event_id TEXT PRIMARY KEY, item_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS acknowledgements (
                    event_id TEXT NOT NULL, consumer TEXT NOT NULL,
                    reason TEXT NOT NULL, delivery_ref TEXT NOT NULL DEFAULT '',
                    acknowledged_at TEXT NOT NULL,
                    PRIMARY KEY(event_id, consumer)
                );
            """)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.settings.database_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def public_calendar(row: dict) -> dict:
        return {
            key: value
            for key, value in row.items()
            if key not in {"url", "body", "etag", "last_modified", "config_version"}
        }

    def calendars(self, source: str = "") -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM calendars WHERE ? = '' OR id = ? OR name = ? ORDER BY name",
                (source, source, source),
            ).fetchall()
        if source and not rows:
            raise ValueError("Unknown calendar; use an exact calendar_id or name")
        return [dict(row) for row in rows]

    def manage(
        self,
        action: str,
        *,
        name="",
        url="",
        source="",
        timezone="",
        reminder_minutes: int | None = None,
    ) -> dict:
        if action == "list":
            return {
                "calendars": [self.public_calendar(row) for row in self.calendars()]
            }
        if action == "subscribe":
            name = text(name, "name", 200, required=True)
            url = subscription_url(url)
            timezone = timezone or self.settings.timezone
            ZoneInfo(timezone)
            minutes = (
                self.settings.default_reminder_minutes
                if reminder_minutes is None
                else reminder_minutes
            )
            if type(minutes) is not int or not 0 <= minutes <= 10080:
                raise ValueError("reminder_minutes must be an integer in [0, 10080]")
            with self.connection() as conn:
                existing = conn.execute(
                    "SELECT * FROM calendars WHERE name = ? OR url = ?", (name, url)
                ).fetchone()
                if existing:
                    if (
                        existing["name"],
                        existing["url"],
                        existing["timezone"],
                        existing["reminder_minutes"],
                    ) != (name, url, timezone, minutes):
                        raise ValueError(
                            "Calendar name or URL already exists with different settings"
                        )
                    source_id = existing["id"]
                else:
                    source_id = "ics_" + uuid4().hex
                    conn.execute(
                        "INSERT INTO calendars(id, name, url, timezone, reminder_minutes) VALUES (?, ?, ?, ?, ?)",
                        (source_id, name, url, timezone, minutes),
                    )
            return {
                "ok": True,
                "calendar": self.public_calendar(self.calendars(source_id)[0]),
            }
        if action not in {"pause", "resume", "unsubscribe"}:
            raise ValueError(
                "action must be list, subscribe, pause, resume or unsubscribe"
            )
        if not source:
            raise ValueError("source is required")
        source_id = self.calendars(source)[0]["id"]
        with self.connection() as conn:
            if action == "unsubscribe":
                conn.execute("DELETE FROM calendars WHERE id = ?", (source_id,))
            else:
                conn.execute(
                    "UPDATE calendars SET enabled = ?, config_version = config_version + 1 WHERE id = ?",
                    (int(action == "resume"), source_id),
                )
        return {"ok": True, "calendar_id": source_id, "action": action}

    def save_sync(
        self,
        source: dict,
        *,
        body: bytes | None = None,
        etag="",
        last_modified="",
        error="",
    ) -> bool:
        now = iso(self.clock())
        with self.connection() as conn:
            if error:
                cursor = conn.execute(
                    "UPDATE calendars SET last_checked_at = ?, last_error = ? WHERE id = ? AND config_version = ?",
                    (now, error, source["id"], source["config_version"]),
                )
            else:
                cursor = conn.execute(
                    """UPDATE calendars SET body = coalesce(?, body), etag = ?,
                    last_modified = ?, last_checked_at = ?, last_success_at = ?, last_error = ''
                    WHERE id = ? AND config_version = ? AND enabled = 1""",
                    (
                        body,
                        etag,
                        last_modified,
                        now,
                        now,
                        source["id"],
                        source["config_version"],
                    ),
                )
        return cursor.rowcount == 1

    def todo_create(
        self,
        title: str,
        *,
        notes="",
        due_at: str | None = None,
        reminder_at: str | None = None,
    ) -> dict:
        title = text(title, "title", 500, required=True)
        notes = text(notes, "notes", 8000)
        due_at = iso(timestamp(due_at)) if due_at is not None else None
        reminder_at = iso(timestamp(reminder_at)) if reminder_at is not None else due_at
        self._validate_todo_dates(due_at, reminder_at)
        now, item_id = iso(self.clock()), "todo_" + uuid4().hex
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO todos(id, title, notes, due_at, reminder_at,
                         status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'open', ?, ?)""",
                (item_id, title, notes, due_at, reminder_at, now, now),
            )
        return self.todo_get(item_id)

    def todo_get(self, item_id: str) -> dict:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM todos WHERE id = ?", (item_id,)
            ).fetchone()
        if row is None:
            raise ValueError("Unknown todo_id")
        return dict(row)

    def todo_list(self, status="open", *, limit=100, offset=0) -> dict:
        if status not in {"open", "completed", "deleted", "all"}:
            raise ValueError("status must be open, completed, deleted or all")
        self._page(limit, offset)
        with self.connection() as conn:
            total = conn.execute(
                "SELECT count(*) FROM todos WHERE ? = 'all' OR status = ?",
                (status, status),
            ).fetchone()[0]
            rows = conn.execute(
                """SELECT * FROM todos WHERE ? = 'all' OR status = ?
                                 ORDER BY due_at IS NULL, due_at, created_at, id LIMIT ? OFFSET ?""",
                (status, status, limit, offset),
            ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "offset": offset}

    def todo_update(self, item_id: str, changes: dict) -> dict:
        allowed = {"title", "notes", "due_at", "reminder_at", "status"}
        if not isinstance(changes, dict) or not changes or set(changes) - allowed:
            raise ValueError(
                "changes must contain title, notes, due_at, reminder_at and/or status"
            )
        # Read/modify/write inside one transaction: concurrent completion must not be lost.
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM todos WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown todo_id")
            previous = dict(row)
            updated = {**previous, **changes}
            updated["title"] = text(updated["title"], "title", 500, required=True)
            updated["notes"] = text(updated["notes"], "notes", 8000)
            for field in ("due_at", "reminder_at"):
                value = updated[field]
                if value is not None:
                    updated[field] = iso(timestamp(value))
            # A reminder that followed the old deadline continues following the new one.
            if (
                "due_at" in changes
                and "reminder_at" not in changes
                and previous["reminder_at"] == previous["due_at"]
            ):
                updated["reminder_at"] = updated["due_at"]
            self._validate_todo_dates(updated["due_at"], updated["reminder_at"])
            if updated["status"] not in {"open", "completed", "deleted"}:
                raise ValueError("status must be open, completed or deleted")
            generation = previous["generation"] + int(
                previous["status"] != "open" and updated["status"] == "open"
            )
            conn.execute(
                """UPDATE todos SET title=?, notes=?, due_at=?, reminder_at=?, status=?,
                            generation=?, updated_at=? WHERE id=?""",
                (
                    updated["title"],
                    updated["notes"],
                    updated["due_at"],
                    updated["reminder_at"],
                    updated["status"],
                    generation,
                    iso(self.clock()),
                    item_id,
                ),
            )
        return self.todo_get(item_id)

    @staticmethod
    def _validate_todo_dates(due_at, reminder_at):
        if due_at and reminder_at and timestamp(reminder_at) > timestamp(due_at):
            raise ValueError("reminder_at must not be later than due_at")

    @staticmethod
    def _page(limit, offset=0):
        if (
            type(limit) is not int
            or not 1 <= limit <= 500
            or type(offset) is not int
            or offset < 0
        ):
            raise ValueError("limit must be 1..500 and offset must be nonnegative")

    def calendar_query(
        self, start_at: str, end_at: str, *, source="", limit=100, offset=0
    ) -> dict:
        start, end = timestamp(start_at), timestamp(end_at)
        if not start < end or end - start > timedelta(days=93):
            raise ValueError("Query range must be positive and at most 93 days")
        self._page(limit, offset)
        results, sources = [], []
        now = self.clock()
        for row in self.calendars(source):
            fresh = self._fresh(row, now)
            sources.append({**self.public_calendar(row), "fresh": fresh})
            if row["body"] and row["enabled"]:
                items = self._occurrences(row, start, end)
                results.extend(
                    {**item, "fresh": fresh, "last_success_at": row["last_success_at"]}
                    for item in items
                )
        results.sort(key=lambda row: (row["start_at"], row["item_id"]))
        return {
            "items": results[offset : offset + limit],
            "total": len(results),
            "sources": sources,
        }

    def _occurrences(self, row, start, end):
        return occurrences(
            bytes(row["body"]),
            row["id"],
            row["name"],
            row["timezone"],
            start,
            end,
            row["reminder_minutes"],
            self.settings.all_day_reminder_hour,
        )

    def _fresh(self, row, now):
        return bool(
            row["enabled"]
            and row["body"]
            and row["last_success_at"]
            and timedelta(0)
            <= now - timestamp(row["last_success_at"])
            < timedelta(seconds=self.settings.calendar_cache_ttl_seconds)
        )

    def _current_alerts(self, unavailable: set[str] | None = None) -> list[dict]:
        now = self.clock()
        events = []
        for row in self.calendars():
            if not self._fresh(row, now):
                if unavailable is not None and row["enabled"]:
                    unavailable.add(row["id"])
                continue
            # Include all-day events and reminders up to seven days before a timed event.
            try:
                items = self._occurrences(
                    row, now - timedelta(days=2), now + timedelta(days=8)
                )
            except (ValueError, TypeError, KeyError, OverflowError):
                if unavailable is not None:
                    unavailable.add(row["id"])
                continue
            for item in items:
                expires = (
                    timestamp(item["end_at"])
                    if item["all_day"]
                    else timestamp(item["start_at"])
                    + timedelta(minutes=self.settings.calendar_late_minutes)
                )
                if timestamp(item["reminder_at"]) <= now < expires:
                    events.append(self._alert(item, expires))
        with self.connection() as conn:
            todos = conn.execute(
                "SELECT * FROM todos WHERE status='open' AND reminder_at IS NOT NULL"
            ).fetchall()
        for row in todos:
            item = dict(row)
            anchor = timestamp(item["due_at"] or item["reminder_at"])
            expires = anchor + timedelta(hours=self.settings.todo_late_hours)
            if timestamp(item["reminder_at"]) <= now < expires:
                normalized = {
                    key: item[key]
                    for key in ("title", "notes", "due_at", "reminder_at", "generation")
                }
                item = {
                    **normalized,
                    "item_id": item["id"],
                    "type": "todo",
                    "source_name": "本地待办",
                }
                item["revision"] = digest(item)
                events.append(self._alert(item, expires))
        return sorted(
            events, key=lambda event: (event["reminder_at"], event["event_id"])
        )

    def _alert(self, item, expires):
        zone = ZoneInfo(self.settings.timezone)
        anchor = item.get("start_at") or item.get("due_at") or item["reminder_at"]
        local = timestamp(anchor).astimezone(zone).isoformat()
        label = (
            "日程开始"
            if item["type"] == "calendar"
            else "待办截止"
            if item.get("due_at")
            else "待办提醒"
        )
        details = [
            f"{label}：{local}",
            f"当前时间：{self.clock().astimezone(zone).isoformat()}",
        ]
        if item.get("all_day"):
            details.append("全天日程；开始和结束边界按日历时区解释。")
        if item.get("location"):
            details.append(f"地点：{item['location']}")
        if item.get("notes"):
            details.append(f"备注：{item['notes']}")
        return {
            **item,
            "kind": "alert",
            "event_id": f"{item['item_id']}@{item['revision']}",
            "content": "\n".join(details),
            "content_hash": item["revision"],
            "severity": "info",
            "published_at": item["reminder_at"],
            "expires_at": iso(expires),
            "suggested_tone": "简短、明确，保留准确时间，不声称用户已经完成任务。",
        }

    def events(self, consumer: str, limit=100) -> list[dict]:
        consumer = text(consumer, "consumer", 500, required=True)
        self._page(limit)
        current = self._current_alerts()
        now = iso(self.clock())
        with self.connection() as conn:
            acknowledged = {
                row[0]
                for row in conn.execute(
                    "SELECT event_id FROM acknowledgements WHERE consumer=?",
                    (consumer,),
                )
            }
            selected = [
                item for item in current if item["event_id"] not in acknowledged
            ][:limit]
            for item in selected:
                conn.execute(
                    "INSERT INTO alert_versions(event_id,item_id,payload_json,created_at) VALUES(?,?,?,?) ON CONFLICT(event_id) DO NOTHING",
                    (
                        item["event_id"],
                        item["item_id"],
                        json.dumps(item, ensure_ascii=False),
                        now,
                    ),
                )
        return selected

    def validate(self, event_ids: list[str], consumer: str) -> dict:
        self._event_ids(event_ids)
        consumer = text(consumer, "consumer", 500, required=True)
        unavailable_sources: set[str] = set()
        active = {
            item["event_id"] for item in self._current_alerts(unavailable_sources)
        }
        with self.connection() as conn:
            acknowledged = {
                row[0]
                for row in conn.execute(
                    "SELECT event_id FROM acknowledgements WHERE consumer=?",
                    (consumer,),
                )
            }
            issued = {
                row["event_id"]: json.loads(row["payload_json"])
                for row in conn.execute(
                    "SELECT event_id,payload_json FROM alert_versions"
                )
            }
        unavailable = [
            key
            for key in event_ids
            if key not in acknowledged
            and issued.get(key, {}).get("calendar_id") in unavailable_sources
            and self.clock() < timestamp(issued[key]["expires_at"])
        ]
        return {
            "valid_event_ids": [
                key for key in event_ids if key in active and key not in acknowledged
            ],
            "unavailable_event_ids": unavailable,
        }

    @staticmethod
    def _event_ids(event_ids):
        if (
            not isinstance(event_ids, list)
            or len(event_ids) > 500
            or any(
                not isinstance(key, str) or not key.strip() or len(key) > 200
                for key in event_ids
            )
        ):
            raise ValueError("event_ids must contain at most 500 nonblank version IDs")

    def acknowledge(
        self, event_ids: list[str], consumer: str, reason="processed", delivery_ref=""
    ) -> dict:
        self._event_ids(event_ids)
        consumer = text(consumer, "consumer", 500, required=True)
        if reason not in {"processed", "read", "discarded", "delivered"}:
            raise ValueError("Unsupported acknowledgement reason")
        delivery_ref = text(delivery_ref, "delivery_ref", 1000)
        if reason == "delivered" and not delivery_ref:
            raise ValueError("delivered requires an actual delivery_ref")
        missing, accepted = [], []
        with self.connection() as conn:
            for event_id in dict.fromkeys(event_ids):
                if not conn.execute(
                    "SELECT 1 FROM alert_versions WHERE event_id=?", (event_id,)
                ).fetchone():
                    missing.append(event_id)
                    continue
                conn.execute(
                    """INSERT INTO acknowledgements(event_id,consumer,reason,delivery_ref,acknowledged_at)
                    VALUES(?,?,?,?,?) ON CONFLICT(event_id,consumer) DO NOTHING""",
                    (event_id, consumer, reason, delivery_ref, iso(self.clock())),
                )
                accepted.append(event_id)
        return {"ok": not missing, "acknowledged": accepted, "missing": missing}
