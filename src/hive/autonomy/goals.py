"""Durable, redacted operator-goal lifecycle state.

This module deliberately records the state of a goal without planning or executing
it.  A later heartbeat integration can claim a goal, use the existing planner, and
attach its existing TaskBoard rows without creating a second work queue.
"""
from __future__ import annotations

import hashlib
import socket
import sqlite3
import threading
import time
import uuid
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from hive.agents.delegations import _default_machine_identity
from hive.core import credentials
from hive.core.redact import redact_value

OPEN = "open"
PLANNING = "planning"
EXECUTING = "executing"
EVALUATING = "evaluating"
REPLANNING = "replanning"
COMPLETED = "completed"
BLOCKED = "blocked"
CANCELLED = "cancelled"

STATUSES = frozenset({
    OPEN, PLANNING, EXECUTING, EVALUATING, REPLANNING, COMPLETED, BLOCKED, CANCELLED,
})
PLANNABLE_STATUSES = frozenset({OPEN, REPLANNING})
ACTIVE_STATUSES = frozenset({OPEN, PLANNING, EXECUTING, EVALUATING, REPLANNING})
REPLAN_ELIGIBLE_STATUSES = frozenset({PLANNING, EXECUTING, EVALUATING})
DEFAULT_MAX_REPLANS = 2
_INTENT_SERVICE_PREFIX = "HiveOS.goal-intents"


class OwnerIntentStore:
    """Owner-only goal intent in the native keyring, never in SQLite or env."""

    def __init__(self) -> None:
        root = str(credentials.config.get_config().root.resolve()).encode("utf-8")
        self._service = f"{_INTENT_SERVICE_PREFIX}.{hashlib.sha256(root).hexdigest()[:16]}"

    def put(self, goal_id: str, intent: str) -> None:
        try:
            credentials._keyring().set_password(self._service, str(goal_id), intent)
        except Exception as exc:  # noqa: BLE001
            raise credentials.CredentialStoreError("goal intent write to OS keyring failed") from exc

    def get(self, goal_id: str) -> str | None:
        try:
            return credentials._keyring().get_password(self._service, str(goal_id))
        except Exception as exc:  # noqa: BLE001
            raise credentials.CredentialStoreError("goal intent read from OS keyring failed") from exc

    def delete(self, goal_id: str) -> None:
        try:
            credentials._keyring().delete_password(self._service, str(goal_id))
        except Exception:  # absent values are safe to leave alone
            return


@dataclass(frozen=True, slots=True)
class GoalRecord:
    """One durable operator goal and its current plan generation."""

    goal_id: str
    summary: str
    status: str
    plan_generation: int
    replan_count: int
    max_replans: int
    last_reason: str
    created_ts: float
    updated_ts: float
    owner_host: str = ""
    owner_machine_id: str = ""
    task_ids: tuple[int, ...] = ()


class GoalLedger:
    """SQLite-backed goal lifecycle with cross-process planning claims.

    The ledger persists only redacted summaries, redacted transition reasons, and
    TaskBoard identifiers.  It intentionally has no access to raw task inputs,
    tool results, prompts, or a tool executor.
    """

    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time,
                 hostname: str | None = None, machine_identity: str | None = None) -> None:
        self._path = str(db_path)
        self._clock = clock
        self._owner_host = socket.gethostname() if hostname is None else str(hostname)
        self._owner_machine_id = _default_machine_identity() if machine_identity is None else str(machine_identity)
        self._lock = threading.RLock()
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=5000")
        self._configure_journal_mode()
        self._initialize_schema()

    def _configure_journal_mode(self) -> None:
        for attempt in range(3):
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as exc:
                if not self._is_transient_lock(exc) or attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def _initialize_schema(self) -> None:
        with self._write_transaction():
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS hive_goals(
                  goal_id         TEXT PRIMARY KEY,
                  summary         TEXT NOT NULL,
                  status          TEXT NOT NULL,
                  plan_generation INTEGER NOT NULL DEFAULT 0,
                  replan_count    INTEGER NOT NULL DEFAULT 0,
                  max_replans     INTEGER NOT NULL DEFAULT 2,
                  last_reason     TEXT NOT NULL DEFAULT '',
                  owner_host      TEXT NOT NULL DEFAULT '',
                  owner_machine_id TEXT NOT NULL DEFAULT '',
                  created_ts      REAL NOT NULL,
                  updated_ts      REAL NOT NULL
                )
                """
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS hive_goals_active "
                "ON hive_goals(status, created_ts, goal_id)"
            )
            columns = {str(row[1]) for row in self._db.execute("PRAGMA table_info(hive_goals)")}
            if "owner_host" not in columns:
                self._db.execute("ALTER TABLE hive_goals ADD COLUMN owner_host TEXT NOT NULL DEFAULT ''")
            if "owner_machine_id" not in columns:
                self._db.execute("ALTER TABLE hive_goals ADD COLUMN owner_machine_id TEXT NOT NULL DEFAULT ''")
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS hive_goal_tasks(
                  goal_id         TEXT NOT NULL,
                  plan_generation INTEGER NOT NULL,
                  task_id         INTEGER NOT NULL,
                  created_ts      REAL NOT NULL,
                  PRIMARY KEY(goal_id, plan_generation, task_id),
                  FOREIGN KEY(goal_id) REFERENCES hive_goals(goal_id)
                )
                """
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS hive_goal_tasks_goal "
                "ON hive_goal_tasks(goal_id, plan_generation, task_id)"
            )

    @staticmethod
    def _is_transient_lock(exc: sqlite3.OperationalError) -> bool:
        message = str(exc).casefold()
        return "locked" in message or "busy" in message

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        """Serialize state transitions across independent Hive processes."""
        with self._lock:
            for attempt in range(3):
                try:
                    self._db.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as exc:
                    if not self._is_transient_lock(exc) or attempt == 2:
                        raise
                    time.sleep(0.05 * (attempt + 1))
            try:
                yield
            except BaseException:
                self._db.rollback()
                raise
            else:
                self._db.commit()

    @staticmethod
    def _safe_text(value: object) -> str:
        # The durable operator ledger is lifecycle evidence, not a prompt store.
        # A digest lets operators correlate one goal without persisting its raw
        # request, including arbitrary sensitive text not recognized as a token.
        raw = str(value)
        masked = str(redact_value(raw)).replace("\n", " ").strip()
        if not masked:
            return ""
        digest = hashlib.sha256(masked.encode("utf-8")).hexdigest()[:16]
        return f"operator-managed goal [{digest}]"

    def create(self, summary: object) -> GoalRecord:
        """Persist one operator goal with the fixed, bounded replan budget."""
        if not self._owner_machine_id:
            raise RuntimeError("HIVE_STATE_HOST_ID is required for durable autonomous goals")
        safe_summary = self._safe_text(summary)
        if not safe_summary:
            raise ValueError("goal summary must not be empty")
        goal_id = str(uuid.uuid4())
        now = self._clock()
        with self._write_transaction():
            self._db.execute(
                "INSERT INTO hive_goals(goal_id, summary, status, max_replans, owner_host, owner_machine_id, created_ts, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (goal_id, safe_summary, OPEN, DEFAULT_MAX_REPLANS, self._owner_host,
                 self._owner_machine_id, now, now),
            )
        record = self.get(goal_id)
        if record is None:  # pragma: no cover - a committed local insert must be visible
            raise RuntimeError("created goal was not persisted")
        return record

    def get(self, goal_id: str) -> GoalRecord | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM hive_goals WHERE goal_id=?", (str(goal_id),)
            ).fetchone()
            return self._record(row) if row is not None else None

    def list(self, *, statuses: Collection[str] | None = None, limit: int = 100) -> list[GoalRecord]:
        """Return goals oldest first, optionally constrained to known statuses."""
        safe_limit = max(1, min(int(limit), 500))
        selected = tuple(sorted(set(statuses))) if statuses is not None else ()
        if any(status not in STATUSES for status in selected):
            raise ValueError("unknown goal status")
        with self._lock:
            if selected:
                placeholders = ", ".join("?" for _ in selected)
                rows = self._db.execute(
                    f"SELECT * FROM hive_goals WHERE status IN ({placeholders}) "
                    "ORDER BY created_ts, goal_id LIMIT ?",
                    (*selected, safe_limit),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM hive_goals ORDER BY created_ts, goal_id LIMIT ?", (safe_limit,)
                ).fetchall()
            return [self._record(row) for row in rows]

    def claim_planning(self, goal_id: str | None = None) -> GoalRecord | None:
        """Atomically claim one open/replanning goal and advance its generation."""
        if not self._owner_machine_id:
            return None
        now = self._clock()
        plannable = tuple(sorted(PLANNABLE_STATUSES))
        placeholders = ", ".join("?" for _ in plannable)
        with self._write_transaction():
            if goal_id is None:
                row = self._db.execute(
                    f"SELECT goal_id FROM hive_goals WHERE status IN ({placeholders}) AND owner_machine_id=? "
                    "ORDER BY created_ts, goal_id LIMIT 1",
                    (*plannable, self._owner_machine_id),
                ).fetchone()
                if row is None:
                    return None
                selected_id = str(row["goal_id"])
            else:
                selected_id = str(goal_id)
            cursor = self._db.execute(
                "UPDATE hive_goals SET status=?, plan_generation=plan_generation+1, updated_ts=? "
                f"WHERE goal_id=? AND owner_machine_id=? AND status IN ({placeholders})",
                (PLANNING, now, selected_id, self._owner_machine_id, *plannable),
            )
            if cursor.rowcount != 1:
                return None
        return self.get(selected_id)

    def begin_execution(
        self, goal_id: str, task_ids: Collection[int], *, expected_generation: int | None = None,
    ) -> GoalRecord | None:
        """Attach a plan's TaskBoard IDs and transition planning -> executing."""
        normalized_task_ids = tuple(sorted({int(task_id) for task_id in task_ids}))
        if not normalized_task_ids or any(task_id <= 0 for task_id in normalized_task_ids):
            raise ValueError("task_ids must contain positive task identifiers")
        now = self._clock()
        with self._write_transaction():
            row = self._db.execute(
                "SELECT plan_generation FROM hive_goals WHERE goal_id=? AND status=?",
                (str(goal_id), PLANNING),
            ).fetchone()
            if row is None:
                return None
            generation = int(row["plan_generation"])
            if expected_generation is not None and generation != int(expected_generation):
                return None
            self._db.executemany(
                "INSERT OR IGNORE INTO hive_goal_tasks(goal_id, plan_generation, task_id, created_ts) "
                "VALUES (?, ?, ?, ?)",
                [(str(goal_id), generation, task_id, now) for task_id in normalized_task_ids],
            )
            cursor = self._db.execute(
                "UPDATE hive_goals SET status=?, updated_ts=?, last_reason='' "
                "WHERE goal_id=? AND status=? AND plan_generation=?",
                (EXECUTING, now, str(goal_id), PLANNING, generation),
            )
            if cursor.rowcount != 1:
                return None
        return self.get(str(goal_id))

    def begin_evaluation(self, goal_id: str) -> GoalRecord | None:
        """Transition execution -> evaluation after task state inspection."""
        return self._transition(goal_id, from_statuses={EXECUTING}, to_status=EVALUATING)

    def complete(self, goal_id: str) -> GoalRecord | None:
        """Record that the caller's deterministic evaluation completed successfully."""
        return self._transition(goal_id, from_statuses={EVALUATING}, to_status=COMPLETED)

    def request_replan(self, goal_id: str, reason: object) -> GoalRecord | None:
        """Request one of exactly two bounded replans, else block the goal."""
        safe_reason = self._safe_text(reason)
        now = self._clock()
        allowed = tuple(sorted(REPLAN_ELIGIBLE_STATUSES))
        placeholders = ", ".join("?" for _ in allowed)
        with self._write_transaction():
            row = self._db.execute(
                f"SELECT replan_count, max_replans FROM hive_goals WHERE goal_id=? "
                f"AND status IN ({placeholders})",
                (str(goal_id), *allowed),
            ).fetchone()
            if row is None:
                return None
            exhausted = int(row["replan_count"]) >= int(row["max_replans"])
            status = BLOCKED if exhausted else REPLANNING
            increment = 0 if exhausted else 1
            self._db.execute(
                "UPDATE hive_goals SET status=?, replan_count=replan_count+?, last_reason=?, updated_ts=? "
                "WHERE goal_id=?",
                (status, increment, safe_reason, now, str(goal_id)),
            )
        return self.get(str(goal_id))

    def block(self, goal_id: str, reason: object) -> GoalRecord | None:
        """Block active planning without pretending that executing work was stopped."""
        return self._transition(
            goal_id, from_statuses=ACTIVE_STATUSES, to_status=BLOCKED, reason=reason,
        )

    def cancel(self, goal_id: str) -> GoalRecord | None:
        """Cancel only work that has not entered execution.

        Executing task cancellation must remain the TaskBoard/worker owner's job;
        a goal state write cannot safely interrupt an external side effect.
        """
        return self._transition(
            goal_id, from_statuses={OPEN, PLANNING, REPLANNING}, to_status=CANCELLED,
        )

    def resume(self, goal_id: str) -> GoalRecord | None:
        """Return a blocked or pre-execution cancelled goal to the operator queue.

        The bounded replan count deliberately remains intact: an operator resume
        cannot turn a repeatedly failing goal into an unlimited autonomous loop.
        """
        return self._transition(
            goal_id, from_statuses={BLOCKED, CANCELLED}, to_status=OPEN,
        )

    def task_references(self, goal_id: str, *, generation: int | None = None) -> tuple[int, ...]:
        """Return durable TaskBoard IDs for a goal generation, never task content."""
        with self._lock:
            if generation is None:
                row = self._db.execute(
                    "SELECT plan_generation FROM hive_goals WHERE goal_id=?", (str(goal_id),)
                ).fetchone()
                if row is None:
                    return ()
                generation = int(row["plan_generation"])
            rows = self._db.execute(
                "SELECT task_id FROM hive_goal_tasks WHERE goal_id=? AND plan_generation=? "
                "ORDER BY task_id",
                (str(goal_id), int(generation)),
            ).fetchall()
            return tuple(int(row["task_id"]) for row in rows)

    def _transition(
        self,
        goal_id: str,
        *,
        from_statuses: Collection[str],
        to_status: str,
        reason: object = "",
    ) -> GoalRecord | None:
        if to_status not in STATUSES or any(status not in STATUSES for status in from_statuses):
            raise ValueError("unknown goal status")
        allowed = tuple(sorted(set(from_statuses)))
        placeholders = ", ".join("?" for _ in allowed)
        safe_reason = self._safe_text(reason)
        with self._write_transaction():
            cursor = self._db.execute(
                f"UPDATE hive_goals SET status=?, last_reason=?, updated_ts=? WHERE goal_id=? "
                f"AND status IN ({placeholders})",
                (to_status, safe_reason, self._clock(), str(goal_id), *allowed),
            )
            if cursor.rowcount != 1:
                return None
        return self.get(str(goal_id))

    def _record(self, row: sqlite3.Row) -> GoalRecord:
        goal_id = str(row["goal_id"])
        return GoalRecord(
            goal_id=goal_id,
            summary=str(row["summary"]),
            status=str(row["status"]),
            plan_generation=int(row["plan_generation"]),
            replan_count=int(row["replan_count"]),
            max_replans=int(row["max_replans"]),
            last_reason=str(row["last_reason"]),
            created_ts=float(row["created_ts"]),
            updated_ts=float(row["updated_ts"]),
            owner_host=str(row["owner_host"]) if "owner_host" in row.keys() else "",
            owner_machine_id=str(row["owner_machine_id"]) if "owner_machine_id" in row.keys() else "",
            task_ids=self.task_references(goal_id, generation=int(row["plan_generation"])),
        )

    def close(self) -> None:
        with self._lock:
            self._db.close()
