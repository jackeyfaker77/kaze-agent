"""Durable cancellation batches, independent of the session database."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


class PendingChatJournal:
    def __init__(self, workspace: Path):
        self.directory = workspace / "private_runtime" / "pending_chat_writes"

    def _path(self, key: str) -> Path:
        return self.directory / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")

    def load(self) -> dict[str, dict[str, Any]]:
        batches = {}
        for path in sorted(self.directory.glob("*.json")):
            # Never silently discard an unreadable recovery record.
            data = json.loads(path.read_text(encoding="utf-8"))
            key = data.get("session_key")
            messages = data.get("messages")
            if (data.get("version") != 1 or not isinstance(key, str) or not key
                    or self._path(key) != path or not isinstance(messages, list)
                    or not all(isinstance(m, dict) and (m.get("id") or m.get("desktop_write_id")) for m in messages)
                    or not isinstance(data.get("title"), str)
                    or not isinstance(data.get("interruption_metadata"), (dict, type(None)))):
                raise ValueError(f"Invalid pending chat journal: {path.name}")
            batches[key] = data
        return batches

    def save(self, key: str, messages: list[dict[str, Any]], title: str,
             interruption_metadata: dict[str, Any] | None) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        temporary = path.with_suffix(".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump({"version": 1, "session_key": key, "messages": messages,
                           "title": title, "interruption_metadata": interruption_metadata},
                          stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def remove(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
