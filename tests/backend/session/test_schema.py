from __future__ import annotations

import sqlite3
from pathlib import Path

from session.store import SessionStore


def test_session_store_bootstraps_session_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)
    store.close()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        conn.close()

    assert {
        "sessions",
        "messages",
        "messages_fts",
    }.issubset(tables)
