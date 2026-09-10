"""Durable, redacted execution records for operator-visible Hive runs.

``EventBus`` intentionally remains synchronous and in-memory.  This module is
its durable counterpart: it records a small, safe event envelope so an
operator can inspect a completed or interrupted run after the process exits.
It is not a transcript store and must never retain model reasoning or raw tool
payloads.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from hive.core.events import Event, EventBus, EventType
from hive.core.redact import redact_value

_TERMINAL_STATES = frozenset({"ok", "error", "cancelled"})


class RunLedger:
    """Append-only event ledger and lifecycle state for one HiveOS database."""

    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = str(db_path)
        self._clock = clock
        self._lock = threading.RLock()
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS hive_runs(
                  run_id TEXT PRIMARY KEY,
                  kind TEXT NOT NULL,
                  session_id TEXT NOT NULL DEFAULT '',
                  state TEXT NOT NULL CHECK(state IN ('running', 'ok', 'error', 'cancelled')),
                  started_ts REAL NOT NULL,
                  ended_ts REAL,
                  error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_hive_runs_started ON hive_runs(started_ts DESC);
                CREATE INDEX IF NOT EXISTS idx_hive_runs_session ON hive_runs(session_id, started_ts DESC);
                CREATE TABLE IF NOT EXISTS hive_run_events(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT NOT NULL,
                  ts REAL NOT NULL,
                  event_type TEXT NOT NULL,
                  data_json TEXT NOT NULL,
                  FOREIGN KEY(run_id) REFERENCES hive_runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_hive_run_events_run ON hive_run_events(run_id, id);
                """
            )

    def attach(self, bus: EventBus) -> "RunLedger":
        """Persist every event that carries a non-empty ``run_id``."""
        for event_type in EventType:
            bus.subscribe(event_type, self.record_event)
        return self

    def begin(self, run_id: str, *, kind: str, session_id: str = "") -> None:
        """Record a new running operation. Reusing an id is deliberately rejected."""
        normalized = str(run_id).strip()
        if not normalized:
            raise ValueError("run_id must not be empty")
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO hive_runs(run_id, kind, session_id, state, started_ts) "
                "VALUES (?, ?, ?, 'running', ?)",
                (normalized, str(kind or "unknown"), str(session_id or ""), self._clock()),
            )

    def finish(self, run_id: str, *, state: str, error: str = "") -> None:
        """Finish a started run exactly once; an old terminal result cannot change."""
        if state not in _TERMINAL_STATES:
            raise ValueError(f"unsupported terminal run state: {state}")
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE hive_runs SET state=?, ended_ts=?, error=? "
                "WHERE run_id=? AND state='running'",
                (state, self._clock(), str(redact_value(error)), str(run_id)),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"run {run_id!r} is missing or already terminal")

    def record_event(self, event: Event) -> None:
        """Persist a redacted event only when it is correlated to a known run."""
        run_id = str(event.data.get("run_id") or "")
        if not run_id:
            return
        payload = redact_value(dict(event.data))
        with self._lock, self._db:
            exists = self._db.execute(
                "SELECT 1 FROM hive_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if exists is None:
                return
            self._db.execute(
                "INSERT INTO hive_run_events(run_id, ts, event_type, data_json) VALUES (?, ?, ?, ?)",
                (run_id, float(event.timestamp), event.event_type.value,
                 json.dumps(payload, sort_keys=True, default=str)),
            )

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT run_id, kind, session_id, state, started_ts, ended_ts, error "
                "FROM hive_runs WHERE run_id=?", (str(run_id),)
            ).fetchone()
        return dict(row) if row is not None else None

    def recent(self, *, limit: int = 20, session_id: str | None = None) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        sql = "SELECT run_id, kind, session_id, state, started_ts, ended_ts, error FROM hive_runs"
        params: tuple[Any, ...] = ()
        if session_id is not None:
            sql += " WHERE session_id=?"
            params = (str(session_id),)
        sql += " ORDER BY started_ts DESC LIMIT ?"
        with self._lock:
            rows = self._db.execute(sql, params + (safe_limit,)).fetchall()
        return [dict(row) for row in rows]

    def events(self, run_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 1000))
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, event_type, data_json FROM hive_run_events "
                "WHERE run_id=? ORDER BY id ASC LIMIT ?", (str(run_id), safe_limit),
            ).fetchall()
        return [
            {"ts": float(row["ts"]), "type": str(row["event_type"]),
             "data": json.loads(str(row["data_json"]))}
            for row in rows
        ]

    def recover_interrupted(self) -> int:
        """Mark runs left active by a process crash as cancelled, preserving evidence."""
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE hive_runs SET state='cancelled', ended_ts=?, error=? WHERE state='running'",
                (self._clock(), "process ended before run completion"),
            )
        return max(0, int(cursor.rowcount))

    def close(self) -> None:
        with self._lock:
            self._db.close()
