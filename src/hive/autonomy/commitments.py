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

    def get(self, commitment_id: int) -> Commitment | None:
        """Return a single commitment by ID, or None if not found."""
        row = self._db.execute(
            "SELECT * FROM hive_commitments WHERE id=?", (commitment_id,)
        ).fetchone()
        if row is None:
            return None
        return Commitment(id=row["id"], description=row["description"],
                          cadence_seconds=row["cadence_seconds"],
                          task_kind=row["task_kind"],
                          payload=_loads(row["payload"]),
                          active=bool(row["active"]),
                          last_fulfilled=row["last_fulfilled"],
                          created_ts=row["created_ts"])

    def remove(self, commitment_id: int) -> bool:
        """Delete a commitment permanently. Returns False if not found."""
        cur = self._db.execute("DELETE FROM hive_commitments WHERE id=?", (commitment_id,))
        self._db.commit()
        return cur.rowcount > 0

    def reschedule(self, commitment_id: int, cadence_seconds: float) -> bool:
        """Change the cadence of an existing commitment. Returns False if not found."""
        cur = self._db.execute(
            "UPDATE hive_commitments SET cadence_seconds=? WHERE id=?",
            (cadence_seconds, commitment_id),
        )
        self._db.commit()
        return cur.rowcount > 0

    def fulfill(self, commitment_id: int) -> bool:
        """Manually mark a commitment as fulfilled now (resets its overdue clock).
        Returns False if not found."""
        now = self._clock()
        cur = self._db.execute(
            "UPDATE hive_commitments SET last_fulfilled=? WHERE id=?", (now, commitment_id)
        )
        self._db.commit()
        return cur.rowcount > 0

    def overdue(self, now: float | None = None) -> list[Commitment]:
        """Return all active commitments that are currently overdue (never fulfilled or
        past their cadence). Does NOT enqueue tasks; use due_and_enqueue() for that."""
        now = self._clock() if now is None else now
        rows = self._db.execute(
            "SELECT * FROM hive_commitments WHERE active=1"
        ).fetchall()
        result = []
        for r in rows:
            last = r["last_fulfilled"]
            if last is None or (now - last) >= r["cadence_seconds"]:
                result.append(Commitment(
                    id=r["id"], description=r["description"],
                    cadence_seconds=r["cadence_seconds"], task_kind=r["task_kind"],
                    payload=_loads(r["payload"]), active=bool(r["active"]),
                    last_fulfilled=r["last_fulfilled"], created_ts=r["created_ts"],
                ))
        return result

    def count(self, *, active_only: bool = False) -> int:
        """Return the total count of commitments, optionally filtering to active only."""
        q = "SELECT COUNT(*) FROM hive_commitments"
        if active_only:
            q += " WHERE active=1"
        row = self._db.execute(q).fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        self._db.close()


def _loads(text: str) -> dict[str, Any]:
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}
