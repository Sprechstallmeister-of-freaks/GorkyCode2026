from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 2


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        cx = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        cx.row_factory = sqlite3.Row
        cx.execute("PRAGMA foreign_keys=ON")
        cx.execute("PRAGMA busy_timeout=15000")
        try:
            yield cx
        finally:
            cx.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as cx:
            cx.execute("BEGIN IMMEDIATE")
            try:
                yield cx
            except BaseException:
                cx.rollback()
                raise
            else:
                cx.commit()

    def initialize(self):
        with self.connect() as cx:
            cx.execute("PRAGMA journal_mode=WAL")
            v = cx.execute("PRAGMA user_version").fetchone()[0]
            if v > SCHEMA_VERSION:
                raise RuntimeError(f"Database version {v} is newer than application {SCHEMA_VERSION}")
            if v == 0:
                cx.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK(kind IN ('task','meeting','reminder')),
                    title TEXT NOT NULL CHECK(length(trim(title)) > 0),
                    scheduled_date TEXT,
                    scheduled_time TEXT,
                    duration_minutes INTEGER,
                    timezone TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','done','cancelled')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    external_id TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    notified_at TEXT,
                    UNIQUE(source, external_id),
                    CHECK(scheduled_time IS NULL OR scheduled_date IS NOT NULL)
                );
                CREATE INDEX IF NOT EXISTS idx_events_date ON events(scheduled_date, scheduled_time);
                CREATE INDEX IF NOT EXISTS idx_events_kind_date ON events(kind, scheduled_date);
                CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
                CREATE INDEX IF NOT EXISTS idx_events_notified ON events(notified_at);
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    active_event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
                    pending_action TEXT,
                    pending_data TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant','tool')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
                PRAGMA user_version=2;
                """)
            if v == 1:
                # Миграция 1 -> 2: колонка уведомлений о наступлении времени события.
                columns = {row["name"] for row in cx.execute("PRAGMA table_info(events)")}
                if "notified_at" not in columns:
                    cx.execute("ALTER TABLE events ADD COLUMN notified_at TEXT")
                cx.execute("CREATE INDEX IF NOT EXISTS idx_events_notified ON events(notified_at)")
                cx.execute("PRAGMA user_version=2")
