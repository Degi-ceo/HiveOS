"""Durable, redacted execution records for operator-visible Hive runs.

``EventBus`` intentionally remains synchronous and in-memory.  This module is
its durable counterpart: it records a small, safe event envelope so an
operator can inspect a completed or interrupted run after the process exits.
It is not a transcript store and must never retain model reasoning or raw tool
payloads.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from hive.core.events import Event, EventBus, EventType
from hive.core.redact import redact_value

_TERMINAL_STATES = frozenset({"ok", "error", "cancelled"})


def _process_is_alive(pid: int) -> bool:
    """Return whether a local process is still alive without inspecting its data."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        # ``os.kill(pid, 0)`` is not a harmless liveness probe on Windows: its
        # implementation maps signals to process termination.  Query the native
        # process handle instead, without requesting terminate permission.
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: process exists.
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class RunLedger:
    """Append-only event ledger and lifecycle state for one HiveOS database."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        process_id: int | None = None,
        hostname: str | None = None,
        process_is_alive: Callable[[int], bool] = _process_is_alive,
    ) -> None:
        self._path = str(db_path)
        self._clock = clock
        self._process_id = os.getpid() if process_id is None else int(process_id)
        self._hostname = socket.gethostname() if hostname is None else str(hostname)
        self._process_is_alive = process_is_alive
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
                  parent_run_id TEXT NOT NULL DEFAULT '',
                  state TEXT NOT NULL CHECK(state IN ('running', 'ok', 'error', 'cancelled')),
                  started_ts REAL NOT NULL,
                  ended_ts REAL,
                  error TEXT NOT NULL DEFAULT '',
                  owner_host TEXT NOT NULL DEFAULT '',
                  owner_pid INTEGER NOT NULL DEFAULT 0
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
            columns = {str(row[1]) for row in self._db.execute("PRAGMA table_info(hive_runs)")}
            if "owner_host" not in columns:
                self._db.execute("ALTER TABLE hive_runs ADD COLUMN owner_host TEXT NOT NULL DEFAULT ''")
            if "owner_pid" not in columns:
                self._db.execute("ALTER TABLE hive_runs ADD COLUMN owner_pid INTEGER NOT NULL DEFAULT 0")
            if "parent_run_id" not in columns:
                self._db.execute("ALTER TABLE hive_runs ADD COLUMN parent_run_id TEXT NOT NULL DEFAULT ''")

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
                "INSERT INTO hive_runs(run_id, kind, session_id, state, started_ts, owner_host, owner_pid) "
                "VALUES (?, ?, ?, 'running', ?, ?, ?)",
                (normalized, str(kind or "unknown"), str(session_id or ""), self._clock(),
                 self._hostname, self._process_id),
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
            child_run_id = str(payload.get("subagent_run_id") or "")
            if event.event_type is EventType.SUBAGENT_STARTED and child_run_id:
                parent = self._db.execute(
                    "SELECT session_id FROM hive_runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if parent is not None:
                    self._db.execute(
                        "INSERT OR IGNORE INTO hive_runs("
                        "run_id, kind, session_id, parent_run_id, state, started_ts, owner_host, owner_pid"
                        ") VALUES (?, 'subagent', ?, ?, 'running', ?, ?, ?)",
                        (child_run_id, str(parent["session_id"]), run_id, float(event.timestamp),
                         self._hostname, self._process_id),
                    )
                    self._db.execute(
                        "INSERT INTO hive_run_events(run_id, ts, event_type, data_json) VALUES (?, ?, ?, ?)",
                        (child_run_id, float(event.timestamp), event.event_type.value,
                         json.dumps(payload, sort_keys=True, default=str)),
                    )
            elif event.event_type in {EventType.SUBAGENT_COMPLETED, EventType.SUBAGENT_FAILED} and child_run_id:
                child_state = "ok" if event.event_type is EventType.SUBAGENT_COMPLETED else "error"
                self._db.execute(
                    "UPDATE hive_runs SET state=?, ended_ts=?, error=? "
                    "WHERE run_id=? AND state='running'",
                    (child_state, float(event.timestamp), "" if child_state == "ok" else "subagent failed",
                     child_run_id),
                )
                self._db.execute(
                    "INSERT INTO hive_run_events(run_id, ts, event_type, data_json) "
                    "SELECT ?, ?, ?, ? WHERE EXISTS(SELECT 1 FROM hive_runs WHERE run_id=?)",
                    (child_run_id, float(event.timestamp), event.event_type.value,
                     json.dumps(payload, sort_keys=True, default=str), child_run_id),
                )
            self._db.execute(
                "INSERT INTO hive_run_events(run_id, ts, event_type, data_json) VALUES (?, ?, ?, ?)",
                (run_id, float(event.timestamp), event.event_type.value,
                 json.dumps(payload, sort_keys=True, default=str)),
            )

    def record_operator_event(self, event: dict[str, Any]) -> None:
        """Append an already-public operator envelope for later safe replay.

        Re-project the input through ``public_operator_event`` at this sink as
        defence in depth: a future caller cannot turn the durable ledger into
        a raw tool-payload store by passing a hand-built dictionary.
        """
        run_id = str(event.get("run_id") or "")
        event_type = str(event.get("type") or "status")
        if not run_id or not event_type:
            return
        from hive.observability.operator_events import public_operator_event

        payload = public_operator_event(
            event,
            run_id=run_id,
            session_id=str(event.get("session_id") or ""),
            sequence=int(event.get("sequence") or 0),
            timestamp=float(event.get("timestamp") or self._clock()),
        )
        duration = event.get("duration_ms")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            payload["duration_ms"] = max(0, round(duration))
        payload = redact_value(payload)
        with self._lock, self._db:
            exists = self._db.execute(
                "SELECT 1 FROM hive_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if exists is None:
                return
            self._db.execute(
                "INSERT INTO hive_run_events(run_id, ts, event_type, data_json) VALUES (?, ?, ?, ?)",
                (run_id, float(payload.get("timestamp") or self._clock()),
                 f"operator.{event_type}", json.dumps(payload, sort_keys=True, default=str)),
            )

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT run_id, kind, session_id, parent_run_id, state, started_ts, ended_ts, error "
                "FROM hive_runs WHERE run_id=?", (str(run_id),)
            ).fetchone()
        return dict(row) if row is not None else None

    def recent(self, *, limit: int = 20, session_id: str | None = None) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        sql = "SELECT run_id, kind, session_id, parent_run_id, state, started_ts, ended_ts, error FROM hive_runs"
        params: tuple[Any, ...] = ()
        if session_id is not None:
            sql += " WHERE session_id=?"
            params = (str(session_id),)
        sql += " ORDER BY started_ts DESC LIMIT ?"
        with self._lock:
            rows = self._db.execute(sql, params + (safe_limit,)).fetchall()
        return [dict(row) for row in rows]

    def children(self, parent_run_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return durable child runs for one parent without exposing tool payloads."""
        safe_limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._db.execute(
                "SELECT run_id, kind, session_id, parent_run_id, state, started_ts, ended_ts, error "
                "FROM hive_runs WHERE parent_run_id=? ORDER BY started_ts ASC LIMIT ?",
                (str(parent_run_id), safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def events(self, run_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 1000))
        with self._lock:
            rows = self._db.execute(
                "SELECT id, ts, event_type, data_json FROM hive_run_events "
                "WHERE run_id=? ORDER BY id ASC LIMIT ?", (str(run_id), safe_limit),
            ).fetchall()
        return [
            {"id": int(row["id"]), "ts": float(row["ts"]), "type": str(row["event_type"]),
             "data": json.loads(str(row["data_json"]))}
            for row in rows
        ]

    def recover_interrupted(self, run_id: str | None = None) -> int:
        """Recover only locally owned runs whose recorded process is no longer alive.

        A shared state database may be used by a gateway and a local CLI at the
        same time.  Recovery must therefore not treat every foreign ``running``
        row as a crash.  Rows from a remote host are deliberately left for that
        host to recover; legacy rows without an owner remain recoverable.
        """
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT run_id, owner_host, owner_pid FROM hive_runs WHERE state='running'"
                + (" AND run_id=?" if run_id else ""),
                ((str(run_id),) if run_id else ()),
            ).fetchall()
            run_ids = [
                str(row["run_id"])
                for row in rows
                if not str(row["owner_host"])
                or (
                    str(row["owner_host"]) == self._hostname
                    and not self._process_is_alive(int(row["owner_pid"]))
                )
            ]
            recovered = 0
            for run_id in run_ids:
                cursor = self._db.execute(
                    "UPDATE hive_runs SET state='cancelled', ended_ts=?, error=? "
                    "WHERE run_id=? AND state='running'",
                    (self._clock(), "process ended before run completion", run_id),
                )
                recovered += max(0, int(cursor.rowcount))
        return recovered

    def close(self) -> None:
        with self._lock:
            self._db.close()
