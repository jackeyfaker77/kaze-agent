"""Shared append-only fact space for execution and conversation projections."""
import sqlite3


def ensure_event_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS identity (id INTEGER PRIMARY KEY CHECK(id=1), root_id TEXT NOT NULL, version INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, turn_id TEXT NOT NULL,
            run_id TEXT NOT NULL, epoch TEXT NOT NULL, version INTEGER NOT NULL,
            kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS events_turn ON events(turn_id, seq);
        CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
            BEGIN SELECT RAISE(ABORT, 'execution events are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
            BEGIN SELECT RAISE(ABORT, 'execution events are immutable'); END;
    """)
