"""
cron.py — scheduled jobs (M3 #au-1).

Ported in spirit from Hermes cron/{jobs,scheduler}.py, SQLite-first (no jobs.json).
A job has a schedule and a task template; when due, it enqueues a task on the
TaskBoard and advances its next-run. `croniter` is used for full cron expressions
when installed (HAS_CRONITER, like Hermes); without it, a small built-in vocabulary
covers the common cases so HiveOS stays functional with zero extra deps:

  "@hourly"      every hour            "@daily"  every 24h
  "@weekly"      every 7 days          "every <N>s|m|h|d"  fixed interval

DAG: autonomy layer; depends on core + autonomy.tasks.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from hive.autonomy.tasks import TaskBoard

try:  # full cron support is optional
    from croniter import croniter  # type: ignore
    HAS_CRONITER = True
except ImportError:  # pragma: no cover - exercised by the fallback path
    HAS_CRONITER = False

_INTERVAL_RE = re.compile(r"^every\s+(\d+)\s*([smhd])$", re.IGNORECASE)
_ALIASES = {"@hourly": 3600.0, "@daily": 86_400.0, "@weekly": 604_800.0}
_UNIT = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86_400.0}


def next_run(schedule: str, after: float) -> float | None:
    """Next run time strictly after `after`, or None if the schedule is unparseable."""
    sched = schedule.strip()
    if sched in _ALIASES:
        return after + _ALIASES[sched]
    m = _INTERVAL_RE.match(sched)
    if m:
        return after + int(m.group(1)) * _UNIT[m.group(2).lower()]
    if HAS_CRONITER:
        try:
            return float(croniter(sched, after).get_next(float))
        except (ValueError, KeyError):
            return None
    return None


@dataclass(slots=True)
class CronJob:
    id: int
    schedule: str
    task_kind: str
    payload: dict[str, Any]
    enabled: bool
    last_run: float | None
    next_run: float | None


class CronScheduler:
    def __init__(self, db_path: str | Path, board: TaskBoard, *,
                 clock: Callable[[], float] = time.time) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._board = board
        self._clock = clock
        self._init_schema()

    def _init_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS hive_cron(
              id        INTEGER PRIMARY KEY AUTOINCREMENT,
              schedule  TEXT NOT NULL,
              task_kind TEXT NOT NULL,
              payload   TEXT NOT NULL DEFAULT '{}',
              enabled   INTEGER NOT NULL DEFAULT 1,
              last_run  REAL,
              next_run  REAL);
            """
        )
        self._db.commit()

    def add(self, schedule: str, task_kind: str, payload: dict[str, Any] | None = None,
            *, enabled: bool = True) -> int:
        import json
        nr = next_run(schedule, self._clock())
        cur = self._db.execute(
            "INSERT INTO hive_cron(schedule, task_kind, payload, enabled, next_run)"
            " VALUES(?,?,?,?,?)",
            (schedule, task_kind, json.dumps(payload or {}), int(enabled), nr),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def due_and_enqueue(self, now: float | None = None) -> int:
        """Enqueue a task for every enabled job whose next_run has passed; advance
        each fired job's next_run. Returns the number enqueued."""
        import json
        now = self._clock() if now is None else now
        rows = self._db.execute(
            "SELECT * FROM hive_cron WHERE enabled=1 AND next_run IS NOT NULL "
            "AND next_run<=?", (now,)).fetchall()
        fired = 0
        for r in rows:
            payload = _loads(r["payload"])
            self._board.enqueue(r["task_kind"], payload, source=f"cron:{r['id']}")
            nr = next_run(r["schedule"], now)
            self._db.execute(
                "UPDATE hive_cron SET last_run=?, next_run=? WHERE id=?",
                (now, nr, r["id"]))
            fired += 1
        if fired:
            self._db.commit()
        return fired

    def jobs(self) -> list[CronJob]:
        rows = self._db.execute("SELECT * FROM hive_cron ORDER BY id").fetchall()
        return [
            CronJob(id=r["id"], schedule=r["schedule"], task_kind=r["task_kind"],
                    payload=_loads(r["payload"]), enabled=bool(r["enabled"]),
                    last_run=r["last_run"], next_run=r["next_run"])
            for r in rows
        ]

    def list_jobs(self) -> list[CronJob]:
        """Return all scheduled jobs (alias for jobs())."""
        return self.jobs()

    def get(self, job_id: int) -> CronJob | None:
        """Return a single cron job by ID, or None if not found."""
        import json
        row = self._db.execute("SELECT * FROM hive_cron WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        return CronJob(id=row["id"], schedule=row["schedule"], task_kind=row["task_kind"],
                       payload=_loads(row["payload"]), enabled=bool(row["enabled"]),
                       last_run=row["last_run"], next_run=row["next_run"])

    def set_enabled(self, job_id: int, enabled: bool) -> None:
        self._db.execute("UPDATE hive_cron SET enabled=? WHERE id=?",
                         (int(enabled), job_id))
        self._db.commit()

    def remove(self, job_id: int) -> bool:
        """Delete a scheduled job. Returns False if the job_id was not found."""
        cur = self._db.execute("DELETE FROM hive_cron WHERE id=?", (job_id,))
        self._db.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        self._db.close()


def _loads(text: str) -> dict[str, Any]:
    import json
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}
