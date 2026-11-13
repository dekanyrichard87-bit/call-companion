"""Persistent call history (v0.6.0): one small SQLite file in %LOCALAPPDATA%\\CallCompanion\\history.db.

Pure Python (stdlib sqlite3), no Qt: StatusModel calls save() after every change of a call record, and load()
once at start. All calls happen on the Qt main thread; a write is a single UPSERT (well under 1 ms on a local
disk). A broken or locked database never stops the app: every error is logged and the history simply stays in
memory for this session.

Retention: rows older than `retention_days` (default 90) are deleted at start, and at most `max_rows` (default
2000) are kept. Both come from the "history" config block.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("callcompanion.history")

SCHEMA_VERSION = 1
FIELDS = ("call_id", "started", "direction", "number", "name", "company", "profile_url",
          "status", "lookup", "detail", "duration_s", "elapsed_ms", "note")

_DDL = """
CREATE TABLE IF NOT EXISTS calls (
    call_id     TEXT PRIMARY KEY,
    started     TEXT NOT NULL,          -- ISO local time, 2026-11-11T19:15:02
    direction   TEXT NOT NULL DEFAULT 'in',
    number      TEXT NOT NULL DEFAULT '',
    name        TEXT,
    company     TEXT,
    profile_url TEXT,
    status      TEXT NOT NULL DEFAULT 'ringing',
    lookup      TEXT NOT NULL DEFAULT 'pending',
    detail      TEXT NOT NULL DEFAULT '',
    duration_s  INTEGER NOT NULL DEFAULT 0,
    elapsed_ms  INTEGER NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT ''   -- e.g. 'Átkapcsolva: Kovács Péter (102)'
);
CREATE INDEX IF NOT EXISTS calls_started ON calls(started);
"""


class HistoryStore:
    def __init__(self, path: Path | str, retention_days: int = 90, max_rows: int = 2000):
        self.path = Path(path)
        self.retention_days = max(1, int(retention_days or 90))
        self.max_rows = max(50, int(max_rows or 2000))
        self._db: sqlite3.Connection | None = None
        try:
            self._db = sqlite3.connect(str(self.path), timeout=2, isolation_level=None)   # autocommit
            self._db.executescript(_DDL)
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.prune()
        except sqlite3.Error:
            log.exception("history database unavailable (%s) - the call list is kept in memory only", self.path)
            self.close()

    @property
    def ok(self) -> bool:
        return self._db is not None

    def close(self):
        if self._db is not None:
            try:
                self._db.close()
            except sqlite3.Error:
                pass
        self._db = None

    # ---- read / write -------------------------------------------------------------------------------------------
    def load(self, limit: int = 300) -> list[dict]:
        """Newest first. Each row is a dict with FIELDS; `started` is a datetime."""
        if not self._db:
            return []
        try:
            cur = self._db.execute(f"SELECT {', '.join(FIELDS)} FROM calls ORDER BY started DESC LIMIT ?",
                                   (int(limit),))
            rows = []
            for values in cur.fetchall():
                row = dict(zip(FIELDS, values))
                try:
                    row["started"] = datetime.fromisoformat(row["started"])
                except (TypeError, ValueError):
                    continue
                rows.append(row)
            return rows
        except sqlite3.Error:
            log.exception("history load failed")
            return []

    def query(self, since: datetime | None = None, until: datetime | None = None, limit: int | None = None) -> list[dict]:
        """v0.9.0: calls with since <= started < until (either end open), newest first, at most `limit`
        (default max_rows). Used by the main window's date filter and search, which work on the whole history."""
        if not self._db:
            return []
        where, args = [], []
        if since is not None:
            where.append("started >= ?")
            args.append(since.isoformat(timespec="seconds"))
        if until is not None:
            where.append("started < ?")
            args.append(until.isoformat(timespec="seconds"))
        sql = f"SELECT {', '.join(FIELDS)} FROM calls"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY started DESC LIMIT ?"
        args.append(int(limit or self.max_rows))
        try:
            rows = []
            for values in self._db.execute(sql, args).fetchall():
                row = dict(zip(FIELDS, values))
                try:
                    row["started"] = datetime.fromisoformat(row["started"])
                except (TypeError, ValueError):
                    continue
                rows.append(row)
            return rows
        except sqlite3.Error:
            log.exception("history query failed")
            return []

    def save(self, rec) -> None:
        """Insert or update one call. `rec` is any object with the FIELDS as attributes (CallRecord)."""
        if not self._db:
            return
        values = []
        for f in FIELDS:
            v = getattr(rec, f, None)
            if f == "started":
                v = v.isoformat(timespec="seconds") if isinstance(v, datetime) else str(v)
            elif f in ("duration_s", "elapsed_ms"):
                v = int(v or 0)
            elif f in ("detail", "note", "number", "direction", "status", "lookup"):
                v = "" if v is None else str(v)
            values.append(v)
        cols = ", ".join(FIELDS)
        marks = ", ".join("?" for _ in FIELDS)
        updates = ", ".join(f"{f}=excluded.{f}" for f in FIELDS if f != "call_id")
        try:
            self._db.execute(f"INSERT INTO calls ({cols}) VALUES ({marks}) "
                             f"ON CONFLICT(call_id) DO UPDATE SET {updates}", values)
        except sqlite3.Error:
            log.exception("history save failed")

    def clear(self) -> None:
        if not self._db:
            return
        try:
            self._db.execute("DELETE FROM calls")
        except sqlite3.Error:
            log.exception("history clear failed")

    def prune(self) -> None:
        if not self._db:
            return
        t0 = time.monotonic()
        cutoff = (datetime.now() - timedelta(days=self.retention_days)).isoformat(timespec="seconds")
        try:
            n1 = self._db.execute("DELETE FROM calls WHERE started < ?", (cutoff,)).rowcount
            n2 = self._db.execute("DELETE FROM calls WHERE call_id NOT IN "
                                  "(SELECT call_id FROM calls ORDER BY started DESC LIMIT ?)",
                                  (self.max_rows,)).rowcount
            # A call whose end was never seen (app quit mid-call, PC switched off) must not stay "ringing" forever.
            self._db.execute("UPDATE calls SET status='missed' WHERE status='ringing' AND direction='in'")
            self._db.execute("UPDATE calls SET status='unanswered' WHERE status='ringing' AND direction='out'")
            self._db.execute("UPDATE calls SET status='ended' WHERE status IN ('answered','hold')")
            if n1 or n2:
                log.info("history: pruned %d old + %d over-limit row(s) (%d ms)", max(n1, 0), max(n2, 0),
                         int((time.monotonic() - t0) * 1000))
        except sqlite3.Error:
            log.exception("history prune failed")
