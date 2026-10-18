from dataclasses import dataclass, fields
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class Settings:
    database_path: str = "agenda.sqlite3"
    timezone: str = "Asia/Shanghai"
    poll_interval_seconds: int = 300
    calendar_cache_ttl_seconds: int = 1800
    request_timeout_seconds: int = 15
    default_reminder_minutes: int = 15
    calendar_late_minutes: int = 15
    todo_late_hours: int = 24
    all_day_reminder_hour: int = 9

    def __post_init__(self):
        if not isinstance(self.database_path, str) or not self.database_path.strip():
            raise ValueError("database_path must not be blank")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, TypeError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        for name, minimum, maximum in (
            ("poll_interval_seconds", 30, 86400),
            ("calendar_cache_ttl_seconds", 30, 604800),
            ("request_timeout_seconds", 1, 120),
            ("default_reminder_minutes", 0, 10080),
            ("calendar_late_minutes", 1, 1440),
            ("todo_late_hours", 1, 168),
            ("all_day_reminder_hour", 0, 23),
        ):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
        if self.calendar_cache_ttl_seconds < self.poll_interval_seconds:
            raise ValueError("calendar_cache_ttl_seconds must cover one poll interval")

    @classmethod
    def load(cls, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("calendar settings must be a JSON object")
        unknown = set(data) - {item.name for item in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown calendar settings: {sorted(unknown)}")
        if "database_path" in data:
            database = Path(data["database_path"]).expanduser()
            data["database_path"] = str(
                database if database.is_absolute() else path.parent / database
            )
        else:
            data["database_path"] = str(path.parent / "agenda.sqlite3")
        return cls(**data)

    @classmethod
    def from_env(cls):
        path = os.environ.get("KAZE_CALENDAR_CONFIG", "")
        return cls.load(Path(path).resolve()) if path else cls()
