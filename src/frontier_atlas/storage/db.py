"""Record store.

Trial implementation: SQLite (zero setup, single file, WAL mode). Records are
stored as JSON keyed by a deterministic natural key, so re-running a crawl
upserts instead of duplicating, and an interrupted crawl resumes from where it
stopped. The same `RecordStore` interface maps 1:1 onto PostgreSQL
(`INSERT … ON CONFLICT (key) DO UPDATE`, JSONB column) for production.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from ..schemas.base import BaseRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    key          TEXT PRIMARY KEY,
    record_type  TEXT NOT NULL,
    body         TEXT NOT NULL,
    first_seen   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_type ON records(record_type);
CREATE TABLE IF NOT EXISTS crawl_state (
    source TEXT PRIMARY KEY,
    state  TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class RecordStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._lock = asyncio.Lock()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ records
    def _upsert_sync(self, rows: list[tuple[str, str, str]]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.executemany(
                """INSERT INTO records(key, record_type, body, first_seen, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET body=excluded.body, updated_at=excluded.updated_at""",
                [(k, t, b, now, now) for k, t, b in rows],
            )

    async def upsert(self, items: Iterable[tuple[str, BaseRecord]]) -> int:
        rows = [(key, rec.recordType, rec.model_dump_json()) for key, rec in items]
        async with self._lock:
            await asyncio.to_thread(self._upsert_sync, rows)
        return len(rows)

    def iter_bodies(self, record_type: str) -> Iterable[dict]:
        cur = self._conn.execute(
            "SELECT body FROM records WHERE record_type=? ORDER BY first_seen, key", (record_type,)
        )
        for (body,) in cur:
            yield json.loads(body)

    def iter_items(self, record_type: str) -> Iterable[tuple[str, dict]]:
        cur = self._conn.execute(
            "SELECT key, body FROM records WHERE record_type=? ORDER BY first_seen, key", (record_type,)
        )
        for key, body in cur:
            yield key, json.loads(body)

    def count(self, record_type: str) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM records WHERE record_type=?", (record_type,)).fetchone()[0]

    def existing_keys(self, record_type: str) -> set[str]:
        cur = self._conn.execute("SELECT key FROM records WHERE record_type=?", (record_type,))
        return {k for (k,) in cur}

    # ------------------------------------------------------------------ crawl checkpoints
    def get_state(self, source: str) -> dict:
        row = self._conn.execute("SELECT state FROM crawl_state WHERE source=?", (source,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_state(self, source: str, state: dict) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO crawl_state(source, state, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(source) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
                (source, json.dumps(state), datetime.now(timezone.utc).isoformat()),
            )
