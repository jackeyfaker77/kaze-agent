from dataclasses import dataclass
import json
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    idle_threshold_seconds: int = 300
    snapshot_ttl_seconds: int = 30
    foreground_enabled: bool = True
    include_window_title: bool = False
    game_processes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, minimum, maximum in (
            ("idle_threshold_seconds", 1, 86_400),
            ("snapshot_ttl_seconds", 1, 300),
        ):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(
                    f"{name} must be an integer between {minimum} and {maximum}"
                )
        for name in ("foreground_enabled", "include_window_title"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        if not isinstance(self.game_processes, (list, tuple)) or any(
            not isinstance(value, str)
            or not value.strip()
            or "/" in value
            or "\\" in value
            for value in self.game_processes
        ):
            raise ValueError("game_processes must be a list of executable file names")
        object.__setattr__(
            self,
            "game_processes",
            tuple(
                dict.fromkeys(value.strip().casefold() for value in self.game_processes)
            ),
        )

    @classmethod
    def from_env(cls) -> "Settings":
        location = os.environ.get("KAZE_DEVICE_CONFIG", "").strip()
        if not location:
            return cls()
        path = Path(location).expanduser().resolve()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Device config must be a JSON object")
        allowed = set(cls.__dataclass_fields__)
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"Unknown device settings: {sorted(unknown)}")
        return cls(**data)
