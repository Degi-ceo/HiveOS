"""
tasks.py — durable work board (M3 #au-2).

SQLite-first (OpenClaw rule): the heartbeat's task queue lives in the state DB, not
in memory, so queued work survives a restart and is drained on the next tick. This
is the single source of "what to do" — the planner, cron, and commitments all
enqueue here; the heartbeat claims, dispatches, and completes.

DAG: autonomy layer. Depends on core only (stdlib sqlite + json).
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"


@dataclass(slots=True)
class TaskRecord:
    id: int
    kind: str
    payload: dict[str, Any]
    state: str
    created_ts: float
    updated_ts: float
    scheduled_for: float
    source: str
    attempts: int
    last_error: str | None = None


class TaskBoard:
    """Durable FIFO-ish queue of work items in the state DB (WAL)."""

    def __init__(self, db_path: str | Path, *,
                 clock: Callable[[], float] = time.time) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._clock = clock
        self._init_schema()

    def _init_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS hive_tasks(
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              kind          TEXT NOT NULL,
              payload       TEXT NOT NULL DEFAULT '{}',
              state         TEXT NOT NULL DEFAULT 'pending',
              created_ts    REAL NOT NULL,
              updated_ts    REAL NOT NULL,
              scheduled_for REAL NOT NULL DEFAULT 0,
              source        TEXT NOT NULL DEFAULT '',
              attempts      INTEGER NOT NULL DEFAULT 0,
              last_error    TEXT);
            CREATE INDEX IF NOT EXISTS hive_tasks_ready
              ON hive_tasks(state, scheduled_for);
            """
        )
        self._db.commit()

    def enqueue(self, kind: str, payload: dict[str, Any] | None = None, *,
                scheduled_for: float = 0.0, source: str = "") -> int:
        now = self._clock()
        cur = self._db.execute(
            "INSERT INTO hive_tasks(kind, payload, state, created_ts, updated_ts,"
            " scheduled_for, source) VALUES(?,?,?,?,?,?,?)",
            (kind, json.dumps(payload or {}), PENDING, now, now,
             scheduled_for, source),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def due(self, now: float | None = None, *, limit: int = 50) -> list[TaskRecord]:
        now = self._clock() if now is None else now
        rows = self._db.execute(
            "SELECT * FROM hive_tasks WHERE state=? AND scheduled_for<=? "
            "ORDER BY id LIMIT ?",
            (PENDING, now, limit),
        ).fetchall()
        return [_row(r) for r in rows]

    def claim(self, task_id: int) -> bool:
        """pending -> running. Returns False if it wasn't pending (already claimed)."""
        now = self._clock()
        cur = self._db.execute(
            "UPDATE hive_tasks SET state=?, updated_ts=?, attempts=attempts+1 "
            "WHERE id=? AND state=?",
            (RUNNING, now, task_id, PENDING),
        )
        self._db.commit()
        return cur.rowcount > 0

    def complete(self, task_id: int) -> None:
        self._set_state(task_id, DONE)

    def fail(self, task_id: int, error: str = "") -> None:
        self._db.execute(
            "UPDATE hive_tasks SET state=?, updated_ts=?, last_error=? WHERE id=?",
            (FAILED, self._clock(), error[:500], task_id),
        )
        self._db.commit()

    def pending_count(self) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM hive_tasks WHERE state=?", (PENDING,)).fetchone()
        return int(row["n"])

    def get(self, task_id: int) -> TaskRecord | None:
        row = self._db.execute("SELECT * FROM hive_tasks WHERE id=?", (task_id,)).fetchone()
        return _row(row) if row else None

    def all(self, state: str | None = None) -> list[TaskRecord]:
        if state is None:
            rows = self._db.execute("SELECT * FROM hive_tasks ORDER BY id").fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM hive_tasks WHERE state=? ORDER BY id", (state,)).fetchall()
        return [_row(r) for r in rows]

    def cancel(self, task_id: int) -> bool:
        """Cancel a pending task. Returns False if task was not in pending state."""
        now = self._clock()
        cur = self._db.execute(
            "UPDATE hive_tasks SET state=?, updated_ts=? WHERE id=? AND state=?",
            (FAILED, now, task_id, PENDING),
        )
        self._db.commit()
        return cur.rowcount > 0

    def retry(self, task_id: int) -> bool:
        """Reset a failed task back to pending. Returns False if task was not failed."""
        now = self._clock()
        cur = self._db.execute(
            "UPDATE hive_tasks SET state=?, updated_ts=?, last_error=NULL "
            "WHERE id=? AND state=?",
            (PENDING, now, task_id, FAILED),
        )
        self._db.commit()
        return cur.rowcount > 0

    def requeue_running(self) -> int:
        """Reset all RUNNING tasks back to PENDING (recovery after crash/restart).
        Returns the number of tasks requeued."""
        now = self._clock()
        cur = self._db.execute(
            "UPDATE hive_tasks SET state=?, updated_ts=? WHERE state=?",
            (PENDING, now, RUNNING),
        )
        self._db.commit()
        return cur.rowcount

    def recent_failures(self, *, limit: int = 10) -> list[TaskRecord]:
        """Return the most recently failed tasks, newest first."""
        rows = self._db.execute(
            "SELECT * FROM hive_tasks WHERE state=? ORDER BY id DESC LIMIT ?",
            (FAILED, limit),
        ).fetchall()
        return [_row(r) for r in rows]

    def statistics(self) -> dict:
        """Return counts by state and basic timing stats."""
        rows = self._db.execute(
            "SELECT state, COUNT(*) AS n, AVG(attempts) AS avg_attempts "
            "FROM hive_tasks GROUP BY state"
        ).fetchall()
        counts = {r["state"]: {"count": r["n"], "avg_attempts": round(r["avg_attempts"] or 0, 2)}
                  for r in rows}
        total = sum(v["count"] for v in counts.values())
        return {"total": total, "by_state": counts}

    def search(self, *, kind: str | None = None, source: str | None = None,
               state: str | None = None, limit: int = 50) -> list[TaskRecord]:
        """Filter tasks by optional kind, source, and/or state."""
        clauses, params = [], []
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind)
        if source is not None:
            clauses.append("source=?")
            params.append(source)
        if state is not None:
            clauses.append("state=?")
            params.append(state)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._db.execute(
            f"SELECT * FROM hive_tasks {where} ORDER BY id DESC LIMIT ?", params
        ).fetchall()
        return [_row(r) for r in rows]

    def retry_all_failed(self) -> int:
        """Reset all FAILED tasks back to PENDING. Returns the count retried."""
        now = self._clock()
        cur = self._db.execute(
            "UPDATE hive_tasks SET state=?, updated_ts=?, last_error=NULL WHERE state=?",
            (PENDING, now, FAILED),
        )
        self._db.commit()
        return cur.rowcount

    def purge_done(self, max_age_seconds: float = 86_400) -> int:
        """Delete DONE tasks older than max_age_seconds. Returns count deleted."""
        cutoff = self._clock() - max_age_seconds
        cur = self._db.execute(
            "DELETE FROM hive_tasks WHERE state=? AND updated_ts<?",
            (DONE, cutoff),
        )
        self._db.commit()
        return cur.rowcount

    def failed_count(self) -> int:
        """Return the number of FAILED tasks."""
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM hive_tasks WHERE state=?", (FAILED,)
        ).fetchone()
        return int(row["n"]) if row else 0

    def oldest_pending(self) -> "TaskRecord | None":
        """Return the oldest PENDING task (by enqueue time), or None if empty."""
        row = self._db.execute(
            "SELECT * FROM hive_tasks WHERE state=? ORDER BY id ASC LIMIT 1", (PENDING,)
        ).fetchone()
        return _row(row) if row else None

    def _set_state(self, task_id: int, state: str) -> None:
        self._db.execute("UPDATE hive_tasks SET state=?, updated_ts=? WHERE id=?",
                         (state, self._clock(), task_id))
        self._db.commit()

    def close(self) -> None:
        self._db.close()


def _row(r: sqlite3.Row) -> TaskRecord:
    try:
        payload = json.loads(r["payload"])
    except (json.JSONDecodeError, TypeError):
        payload = {}
    return TaskRecord(
        id=r["id"], kind=r["kind"], payload=payload if isinstance(payload, dict) else {},
        state=r["state"], created_ts=r["created_ts"], updated_ts=r["updated_ts"],
        scheduled_for=r["scheduled_for"], source=r["source"], attempts=r["attempts"],
        last_error=r["last_error"],
    )
