"""
commitments.py — recurring promises Hive keeps (M3 #au-2).

A commitment is a user-facing recurring promise ("check the deploy health every
morning", "summarize unread mail daily") — distinct from a cron job (a system
schedule). It has a cadence and a last-fulfilled time; when overdue it enqueues a
task on the board and marks itself fulfilled. SQLite-first, survives restarts.

Kept deliberately simpler than cron (fixed-interval cadence, not cron expressions):
promises are "every N", and the semantics — description + fulfilment tracking — are
what matter for the user, not arbitrary calendars.

DAG: autonomy layer; depends on core + autonomy.tasks.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from hive.autonomy.tasks import TaskBoard


@dataclass(slots=True)
class Commitment:
    id: int
    description: str
    cadence_seconds: float
    task_kind: str
    payload: dict[str, Any]
    active: bool
    last_fulfilled: float | None
    created_ts: float


class CommitmentBook:
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
            CREATE TABLE IF NOT EXISTS hive_commitments(
              id              INTEGER PRIMARY KEY AUTOINCREMENT,
              description     TEXT NOT NULL,
              cadence_seconds REAL NOT NULL,
              task_kind       TEXT NOT NULL DEFAULT 'commitment',
              payload         TEXT NOT NULL DEFAULT '{}',
              active          INTEGER NOT NULL DEFAULT 1,
              last_fulfilled  REAL,
              created_ts      REAL NOT NULL);
            """
        )
        self._db.commit()

    def add(self, description: str, cadence_seconds: float, *,
            task_kind: str = "commitment", payload: dict[str, Any] | None = None) -> int:
        now = self._clock()
        cur = self._db.execute(
            "INSERT INTO hive_commitments(description, cadence_seconds, task_kind,"
            " payload, created_ts) VALUES(?,?,?,?,?)",
            (description, cadence_seconds, task_kind, json.dumps(payload or {}), now),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def due_and_enqueue(self, now: float | None = None) -> int:
        """Enqueue a task for every active commitment that is overdue (never fulfilled,
        or now - last_fulfilled >= cadence); mark it fulfilled. Returns count."""
        now = self._clock() if now is None else now
        rows = self._db.execute(
            "SELECT * FROM hive_commitments WHERE active=1").fetchall()
        fired = 0
        for r in rows:
            last = r["last_fulfilled"]
            overdue = last is None or (now - last) >= r["cadence_seconds"]
            if not overdue:
                continue
            payload = dict(_loads(r["payload"]))
            payload.setdefault("description", r["description"])
            self._board.enqueue(r["task_kind"], payload, source=f"commitment:{r['id']}")
            self._db.execute(
                "UPDATE hive_commitments SET last_fulfilled=? WHERE id=?", (now, r["id"]))
            fired += 1
        if fired:
            self._db.commit()
        return fired

    def set_active(self, commitment_id: int, active: bool) -> None:
        self._db.execute("UPDATE hive_commitments SET active=? WHERE id=?",
                         (int(active), commitment_id))
        self._db.commit()

    def all(self, *, active_only: bool = False) -> list[Commitment]:
        q = "SELECT * FROM hive_commitments"
        if active_only:
            q += " WHERE active=1"
        rows = self._db.execute(q + " ORDER BY id").fetchall()
        return [
            Commitment(id=r["id"], description=r["description"],
                       cadence_seconds=r["cadence_seconds"], task_kind=r["task_kind"],
                       payload=_loads(r["payload"]), active=bool(r["active"]),
                       last_fulfilled=r["last_fulfilled"], created_ts=r["created_ts"])
            for r in rows
        ]

    def list_commitments(self, *, active_only: bool = False) -> list[Commitment]:
        """Return all commitments (alias for all())."""
        return self.all(active_only=active_only)

    def close(self) -> None:
        self._db.close()


def _loads(text: str) -> dict[str, Any]:
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}
