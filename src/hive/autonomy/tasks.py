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
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELED = "canceled"
REQUIRES_REVIEW = "requires_review"
WAITING_APPROVAL = "waiting_approval"


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
    worker_id: str | None = None
    lease_until: float | None = None
    idempotency_key: str | None = None
    approval_id: str | None = None
    replay_safe: bool = False


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
              last_error    TEXT,
              worker_id     TEXT,
              lease_until   REAL,
              idempotency_key TEXT,
              approval_id TEXT,
              replay_safe INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS hive_tasks_ready
              ON hive_tasks(state, scheduled_for);
            CREATE TABLE IF NOT EXISTS hive_failure_cursors(
              consumer TEXT PRIMARY KEY,
              last_task_id INTEGER NOT NULL,
              initialized_ts REAL NOT NULL,
              updated_ts REAL NOT NULL);
            """
        )
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(hive_tasks)")}
        if "worker_id" not in columns:
            self._db.execute("ALTER TABLE hive_tasks ADD COLUMN worker_id TEXT")
        if "lease_until" not in columns:
            self._db.execute("ALTER TABLE hive_tasks ADD COLUMN lease_until REAL")
        if "idempotency_key" not in columns:
            self._db.execute("ALTER TABLE hive_tasks ADD COLUMN idempotency_key TEXT")
        if "approval_id" not in columns:
            self._db.execute("ALTER TABLE hive_tasks ADD COLUMN approval_id TEXT")
        if "replay_safe" not in columns:
            self._db.execute("ALTER TABLE hive_tasks ADD COLUMN replay_safe INTEGER NOT NULL DEFAULT 0")
        self._db.execute("CREATE INDEX IF NOT EXISTS hive_tasks_lease ON hive_tasks(state, lease_until)")
        self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS hive_tasks_idempotency ON hive_tasks(idempotency_key) WHERE idempotency_key IS NOT NULL")
        self._db.execute("CREATE INDEX IF NOT EXISTS hive_tasks_approval ON hive_tasks(approval_id) WHERE approval_id IS NOT NULL")
        self._db.commit()

    def enqueue(self, kind: str, payload: dict[str, Any] | None = None, *,
                scheduled_for: float = 0.0, source: str = "",
                idempotency_key: str | None = None, replay_safe: bool = False) -> int:
        """Durably enqueue one task, committing the board-owned transaction."""
        task_id = self.enqueue_in_transaction(
            self._db, kind, payload, scheduled_for=scheduled_for, source=source,
            idempotency_key=idempotency_key, replay_safe=replay_safe,
        )
        self._db.commit()
        return task_id

    def enqueue_in_transaction(self, conn: sqlite3.Connection, kind: str,
                               payload: dict[str, Any] | None = None, *,
                               scheduled_for: float = 0.0, source: str = "",
                               idempotency_key: str | None = None,
                               replay_safe: bool = False) -> int:
        """Insert on an already-open transaction without committing it.

        Schedulers use this with their own connection so task persistence and
        cursor movement share one SQLite commit.
        """
        now = self._clock()
        key = idempotency_key or uuid.uuid4().hex
        conn.execute(
            "INSERT OR IGNORE INTO hive_tasks(kind, payload, state, created_ts, updated_ts, "
            "scheduled_for, source, idempotency_key, replay_safe) VALUES(?,?,?,?,?,?,?,?,?)",
            (kind, json.dumps(payload or {}), PENDING, now, now, scheduled_for,
             source, key, int(replay_safe)),
        )
        row = conn.execute("SELECT id FROM hive_tasks WHERE idempotency_key=?", (key,)).fetchone()
        if row is None:
            raise RuntimeError("enqueue did not produce a task id")
        return int(row["id"])

    def due(self, now: float | None = None, *, limit: int = 50) -> list[TaskRecord]:
        now = self._clock() if now is None else now
        rows = self._db.execute(
            "SELECT * FROM hive_tasks WHERE state=? AND scheduled_for<=? "
            "ORDER BY id LIMIT ?",
            (PENDING, now, limit),
        ).fetchall()
        return [_row(r) for r in rows]

    def claim(self, task_id: int, *, worker_id: str = "legacy", lease_seconds: float = 300.0) -> bool:
        """Atomically claim a pending task for one worker until its lease expires."""
        now = self._clock()
        lease_until = now + max(1.0, lease_seconds)
        cur = self._db.execute(
            "UPDATE hive_tasks SET state=?, updated_ts=?, attempts=attempts+1, worker_id=?, lease_until=? "
            "WHERE id=? AND state=?",
            (RUNNING, now, worker_id, lease_until, task_id, PENDING),
        )
        self._db.commit()
        return cur.rowcount > 0

    def renew_lease(self, task_id: int, *, worker_id: str,
                    lease_seconds: float = 300.0) -> bool:
        """Extend an active worker lease without changing task ownership."""
        now = self._clock()
        cur = self._db.execute(
            "UPDATE hive_tasks SET updated_ts=?, lease_until=? "
            "WHERE id=? AND state=? AND worker_id=?",
            (now, now + max(1.0, lease_seconds), task_id, RUNNING, worker_id),
        )
        self._db.commit()
        return cur.rowcount > 0
    def complete(self, task_id: int, *, worker_id: str | None = None) -> bool:
        return self._set_state(task_id, DONE, worker_id=worker_id)

    def fail(self, task_id: int, error: str = "", *, worker_id: str | None = None) -> bool:
        now = self._clock()
        if worker_id is None:
            cur = self._db.execute(
                "UPDATE hive_tasks SET state=?, updated_ts=?, last_error=?, worker_id=NULL, lease_until=NULL WHERE id=?",
                (FAILED, now, error[:500], task_id),
            )
        else:
            cur = self._db.execute(
                "UPDATE hive_tasks SET state=?, updated_ts=?, last_error=?, worker_id=NULL, lease_until=NULL "
                "WHERE id=? AND state=? AND worker_id=?",
                (FAILED, now, error[:500], task_id, RUNNING, worker_id),
            )
        self._db.commit()
        return cur.rowcount > 0

    def wait_for_approval(self, task_id: int, approval_id: str | None = None, *,
                          worker_id: str | None = None) -> bool:
        """Persist that a claimed task is paused behind an approval decision."""
        detail = "awaiting approval"
        if approval_id:
            detail += f": {approval_id}"
        if worker_id is None:
            cur = self._db.execute(
                "UPDATE hive_tasks SET state=?, updated_ts=?, last_error=?, approval_id=?, worker_id=NULL, lease_until=NULL "
                "WHERE id=? AND state=?",
                (WAITING_APPROVAL, self._clock(), detail, approval_id, task_id, RUNNING),
            )
        else:
            cur = self._db.execute(
                "UPDATE hive_tasks SET state=?, updated_ts=?, last_error=?, approval_id=?, worker_id=NULL, lease_until=NULL "
                "WHERE id=? AND state=? AND worker_id=?",
                (WAITING_APPROVAL, self._clock(), detail, approval_id, task_id, RUNNING, worker_id),
            )
        self._db.commit()
        return cur.rowcount > 0
    def complete_approval(self, approval_id: str) -> bool:
        """Mark the one waiting task linked to an approved tool call as done."""
        cur = self._db.execute(
            "UPDATE hive_tasks SET state=?, updated_ts=?, last_error=NULL "
            "WHERE approval_id=? AND state=?",
            (DONE, self._clock(), approval_id, WAITING_APPROVAL),
        )
        self._db.commit()
        return cur.rowcount > 0

    def review_approval(self, approval_id: str, error: str) -> bool:
        """Quarantine an approved action with an uncertain result for operator review."""
        cur = self._db.execute(
            "UPDATE hive_tasks SET state=?, updated_ts=?, last_error=? "
            "WHERE approval_id=? AND state=?",
            (REQUIRES_REVIEW, self._clock(), error[:500], approval_id, WAITING_APPROVAL),
        )
        self._db.commit()
        return cur.rowcount > 0

    def cancel_approval(self, approval_id: str) -> bool:
        """Terminally cancel a task when its linked approval is denied or revoked."""
        cur = self._db.execute(
            "UPDATE hive_tasks SET state=?, updated_ts=?, last_error=NULL "
            "WHERE approval_id=? AND state=?",
            (CANCELED, self._clock(), approval_id, WAITING_APPROVAL),
        )
        self._db.commit()
        return cur.rowcount > 0
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
        """Cancel a pending task without classifying it as an execution failure."""
        now = self._clock()
        cur = self._db.execute(
            "UPDATE hive_tasks SET state=?, updated_ts=?, last_error=NULL WHERE id=? AND state=?",
            (CANCELED, now, task_id, PENDING),
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

    def requeue_running(self, now: float | None = None) -> int:
        """Recover expired leases only for explicitly replay-safe work.

        An expired task may have crossed an external-effect boundary.  The
        durable default is therefore deny: tasks without ``replay_safe`` are
        quarantined for review rather than invoked again.
        """
        cutoff = self._clock() if now is None else now
        eligible = "state=? AND worker_id IS NOT NULL AND lease_until IS NOT NULL AND lease_until<=?"
        cur = self._db.execute(
            "UPDATE hive_tasks SET state=?, updated_ts=?, worker_id=NULL, lease_until=NULL "
            f"WHERE {eligible} AND replay_safe=1",
            (PENDING, cutoff, RUNNING, cutoff),
        )
        recovered = cur.rowcount
        self._db.execute(
            "UPDATE hive_tasks SET state=?, updated_ts=?, last_error=COALESCE(last_error, ?), "
            "worker_id=NULL, lease_until=NULL "
            f"WHERE {eligible} AND replay_safe=0",
            (REQUIRES_REVIEW, cutoff, "lease expired; replay was not declared safe", RUNNING, cutoff),
        )
        self._db.commit()
        return recovered
    def autonomy_preflight(self) -> dict[str, Any]:
        """Return whether this board is safe for an autonomous heartbeat.

        Test-originated records are durable evidence, never executable work. A live
        heartbeat must refuse to run against such a board instead of interpreting
        their failures as symptoms for self-modification.
        """
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM hive_tasks "
            "WHERE source IN ('test', 'pytest') OR source LIKE 'test:%' OR source LIKE 'pytest:%'"
        ).fetchone()
        test_records = int(row["n"]) if row else 0
        blockers: list[str] = []
        if test_records:
            blockers.append(f"{test_records} test-origin task record(s) present")
        return {"ok": not blockers, "blockers": blockers, "test_records": test_records}
    def recent_failures(self, *, limit: int = 10) -> list[TaskRecord]:
        """Return the most recently failed tasks, newest first."""
        rows = self._db.execute(
            "SELECT * FROM hive_tasks WHERE state=? ORDER BY id DESC LIMIT ?",
            (FAILED, limit),
        ).fetchall()
        return [_row(r) for r in rows]

    def pending_failure_signals(self, consumer: str, *, limit: int = 10) -> list[TaskRecord]:
        """Return fresh production failures after a durable consumer cursor.

        The first call records the current high-water mark and returns no work,
        preventing historical or test-era failures from becoming live autonomy
        signals after an upgrade or restart. Subsequent calls retain unacknowledged
        production failures until the consumer durably acknowledges them.
        """
        if not consumer.strip():
            raise ValueError("consumer is required")
        self._db.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._db.execute(
                "SELECT last_task_id FROM hive_failure_cursors WHERE consumer=?", (consumer,)
            ).fetchone()
            latest = self._db.execute("SELECT COALESCE(MAX(id), 0) AS id FROM hive_tasks").fetchone()
            latest_id = int(latest["id"]) if latest else 0
            now = self._clock()
            if cursor is None:
                self._db.execute(
                    "INSERT INTO hive_failure_cursors(consumer,last_task_id,initialized_ts,updated_ts) VALUES(?,?,?,?)",
                    (consumer, latest_id, now, now),
                )
                self._db.commit()
                return []
            rows = self._db.execute(
                "SELECT * FROM hive_tasks WHERE state=? AND id>? AND "
                "(source='manual' OR source='planner' OR source LIKE 'cron:%' OR source LIKE 'commitment:%') "
                "ORDER BY id LIMIT ?",
                (FAILED, int(cursor["last_task_id"]), max(1, limit)),
            ).fetchall()
            self._db.commit()
            return [_row(row) for row in rows]
        except Exception:
            self._db.rollback()
            raise

    def acknowledge_failure_signals(self, consumer: str, through_task_id: int) -> bool:
        """Advance a consumer cursor only after it handled the reported failures."""
        if not consumer.strip() or through_task_id < 0:
            raise ValueError("consumer and non-negative through_task_id are required")
        cur = self._db.execute(
            "UPDATE hive_failure_cursors SET last_task_id=CASE WHEN last_task_id<? THEN ? ELSE last_task_id END, "
            "updated_ts=? WHERE consumer=?",
            (through_task_id, through_task_id, self._clock(), consumer),
        )
        self._db.commit()
        return cur.rowcount > 0


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

    def running_count(self) -> int:
        """Return the number of RUNNING tasks."""
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM hive_tasks WHERE state=?", (RUNNING,)
        ).fetchone()
        return int(row["n"]) if row else 0

    def last_failed(self) -> "TaskRecord | None":
        """Return the single most recently failed task, or None."""
        row = self._db.execute(
            "SELECT * FROM hive_tasks WHERE state=? ORDER BY id DESC LIMIT 1", (FAILED,)
        ).fetchone()
        return _row(row) if row else None

    def bulk_cancel_pending(self, kind: str | None = None) -> int:
        """Cancel all PENDING tasks, optionally filtered by kind. Returns count cancelled."""
        now = self._clock()
        if kind is not None:
            cur = self._db.execute(
                "UPDATE hive_tasks SET state=?, updated_ts=? WHERE state=? AND kind=?",
                (FAILED, now, PENDING, kind),
            )
        else:
            cur = self._db.execute(
                "UPDATE hive_tasks SET state=?, updated_ts=? WHERE state=?",
                (FAILED, now, PENDING),
            )
        self._db.commit()
        return cur.rowcount

    def bulk_purge_failed(self, max_age_seconds: float = 86_400) -> int:
        """Delete FAILED tasks older than max_age_seconds. Returns count deleted."""
        cutoff = self._clock() - max_age_seconds
        cur = self._db.execute(
            "DELETE FROM hive_tasks WHERE state=? AND updated_ts<?",
            (FAILED, cutoff),
        )
        self._db.commit()
        return cur.rowcount

    def count_by_kind(self) -> dict[str, int]:
        """Return task counts grouped by kind (e.g. 'tool', 'commitment', 'self_improve')."""
        rows = self._db.execute(
            "SELECT kind, COUNT(*) AS n FROM hive_tasks GROUP BY kind"
        ).fetchall()
        return {r["kind"]: r["n"] for r in rows}

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

    def pending_by_kind(self) -> dict[str, int]:
        """Return count of PENDING tasks grouped by kind."""
        rows = self._db.execute(
            "SELECT kind, COUNT(*) AS n FROM hive_tasks WHERE state=? GROUP BY kind", (PENDING,)
        ).fetchall()
        return {r["kind"]: r["n"] for r in rows}

    def average_age_pending(self, now: float | None = None) -> float:
        """Return the average age in seconds of all PENDING tasks (0.0 if none)."""
        now = self._clock() if now is None else now
        row = self._db.execute(
            "SELECT AVG(? - created_ts) AS avg_age FROM hive_tasks WHERE state=?",
            (now, PENDING),
        ).fetchone()
        v = row["avg_age"] if row else None
        return round(float(v), 3) if v is not None else 0.0

    def oldest_pending_age(self, now: float | None = None) -> float:
        """Return the age in seconds of the oldest PENDING task (0.0 if none)."""
        now = self._clock() if now is None else now
        row = self._db.execute(
            "SELECT MIN(created_ts) AS oldest FROM hive_tasks WHERE state=?", (PENDING,)
        ).fetchone()
        oldest = row["oldest"] if row else None
        return round(now - oldest, 3) if oldest is not None else 0.0

    def total_count(self) -> int:
        """Return the total number of tasks across all states."""
        row = self._db.execute("SELECT COUNT(*) AS n FROM hive_tasks").fetchone()
        return int(row["n"]) if row else 0

    def failure_rate_by_kind(self) -> dict[str, float]:
        """Return the fraction of tasks that failed, grouped by kind.

        A kind with no failed tasks is excluded. Returns {} if no tasks at all."""
        total_rows = self._db.execute(
            "SELECT kind, COUNT(*) AS n FROM hive_tasks GROUP BY kind"
        ).fetchall()
        if not total_rows:
            return {}
        failed_rows = self._db.execute(
            "SELECT kind, COUNT(*) AS n FROM hive_tasks WHERE state=? GROUP BY kind", (FAILED,)
        ).fetchall()
        total_by_kind = {r["kind"]: r["n"] for r in total_rows}
        failed_by_kind = {r["kind"]: r["n"] for r in failed_rows}
        return {
            kind: round(failed_by_kind.get(kind, 0) / total, 4)
            for kind, total in total_by_kind.items()
            if failed_by_kind.get(kind, 0) > 0
        }

    def _set_state(self, task_id: int, state: str, *, worker_id: str | None = None) -> bool:
        now = self._clock()
        if worker_id is None:
            cur = self._db.execute(
                "UPDATE hive_tasks SET state=?, updated_ts=?, worker_id=NULL, lease_until=NULL WHERE id=?",
                (state, now, task_id),
            )
        else:
            cur = self._db.execute(
                "UPDATE hive_tasks SET state=?, updated_ts=?, worker_id=NULL, lease_until=NULL "
                "WHERE id=? AND state=? AND worker_id=?",
                (state, now, task_id, RUNNING, worker_id),
            )
        self._db.commit()
        return cur.rowcount > 0
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
        worker_id=r["worker_id"], lease_until=r["lease_until"],
        idempotency_key=r["idempotency_key"],
        approval_id=r["approval_id"],
        replay_safe=bool(r["replay_safe"]),
    )
