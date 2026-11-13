"""Tiny SQLite store: Telegram cursors, CheckVisaSlots snapshot, dedupe keys, stats."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tg_cursor (group_name TEXT PRIMARY KEY, last_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cvs_snapshot (location TEXT PRIMARY KEY, last_seen TEXT, row_json TEXT);
CREATE TABLE IF NOT EXISTS alerted (key TEXT PRIMARY KEY, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (ts REAL NOT NULL, kind TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


class Store:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    # Telegram cursors
    def get_cursor(self, group: str) -> int | None:
        row = self.conn.execute(
            "SELECT last_id FROM tg_cursor WHERE group_name=?", (group.lower(),)
        ).fetchone()
        return row[0] if row else None

    def set_cursor(self, group: str, last_id: int) -> None:
        self.conn.execute(
            "INSERT INTO tg_cursor(group_name,last_id) VALUES(?,?) "
            "ON CONFLICT(group_name) DO UPDATE SET last_id=excluded.last_id",
            (group.lower(), last_id),
        )

    # CheckVisaSlots snapshot
    def get_cvs_snapshot(self) -> dict[str, dict]:
        rows = self.conn.execute("SELECT location,row_json FROM cvs_snapshot").fetchall()
        return {loc: json.loads(j) for loc, j in rows}

    def save_cvs_snapshot(self, rows: dict[str, dict]) -> None:
        with self.conn:
            for loc, row in rows.items():
                self.conn.execute(
                    "INSERT INTO cvs_snapshot(location,last_seen,row_json) VALUES(?,?,?) "
                    "ON CONFLICT(location) DO UPDATE SET last_seen=excluded.last_seen, "
                    "row_json=excluded.row_json",
                    (loc, row.get("last_seen"), json.dumps(row)),
                )

    # Dedupe
    def mark_alerted(self, key: str) -> bool:
        """Return True if newly marked, False if it was already alerted."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO alerted(key,ts) VALUES(?,?)", (key, time.time())
        )
        return cur.rowcount == 1

    def is_alerted(self, key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM alerted WHERE key=?", (key,)).fetchone() is not None

    # Stats
    def add_event(self, kind: str) -> None:
        self.conn.execute("INSERT INTO events(ts,kind) VALUES(?,?)", (time.time(), kind))

    def count_events(self, kind: str, since_ts: float) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind=? AND ts>=?", (kind, since_ts)
        ).fetchone()[0]

    def prune(self, older_than_days: int = 14) -> None:
        cutoff = time.time() - older_than_days * 86400
        self.conn.execute("DELETE FROM events WHERE ts<?", (cutoff,))
        self.conn.execute("DELETE FROM alerted WHERE ts<?", (cutoff,))

    # Meta
    def get_meta(self, k: str) -> str | None:
        row = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else None

    def set_meta(self, k: str, v: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v)
        )
