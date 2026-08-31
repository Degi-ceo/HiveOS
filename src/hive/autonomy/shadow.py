"""Read-only autonomy shadow-run evidence collection.

This module intentionally does *not* construct :class:`hive.runtime.HiveOS`.
Consequently it cannot invoke a planner, ToolExecutor, Telegram channel, memory
projection, scheduler, or self-modification path. It only reads a state database
and records an operator-facing snapshot in a separate evidence database.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from hive.core.sqlite_ops import verify_database


@dataclass(frozen=True, slots=True)
class ShadowReport:
    """A non-effectful inventory of work visible to a shadow observer."""

    observed_at: float
    source_db: str
    evidence_db: str
    integrity_ok: bool
    task_states: dict[str, int]
    due_tasks: int
    notes: tuple[str, ...]


def run_shadow(source_db: str | Path, evidence_db: str | Path, *, now: float | None = None) -> ShadowReport:
    """Read ``source_db`` without mutation and durably save a report elsewhere."""
    source = Path(source_db)
    evidence = Path(evidence_db)
    if source.resolve() == evidence.resolve():
        raise ValueError("shadow evidence database must differ from the source database")

    integrity_ok, integrity_details = verify_database(source)
    if not integrity_ok:
        raise sqlite3.DatabaseError(
            "refusing shadow run on invalid source database: " + "; ".join(integrity_details)
        )

    observed_at = time.time() if now is None else now
    task_states, due_tasks, notes = _read_work_inventory(source, observed_at)
    report = ShadowReport(
        observed_at=observed_at,
        source_db=str(source.resolve()),
        evidence_db=str(evidence.resolve()),
        integrity_ok=True,
        task_states=task_states,
        due_tasks=due_tasks,
        notes=tuple(notes),
    )
    _store_evidence(evidence, report)
    return report


def _read_work_inventory(source: Path, now: float) -> tuple[dict[str, int], int, list[str]]:
    conn: sqlite3.Connection | None = None
    try:
        uri = source.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("PRAGMA query_only=ON")
        has_tasks = conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='hive_tasks'"
        ).fetchone()
        if not has_tasks:
            return {}, 0, ["hive_tasks table is absent; no executable work was inspected"]
        states = {
            str(row[0]): int(row[1])
            for row in conn.execute("SELECT state, COUNT(*) FROM hive_tasks GROUP BY state")
        }
        due = int(conn.execute(
            "SELECT COUNT(*) FROM hive_tasks WHERE state='pending' AND scheduled_for<=?", (now,)
        ).fetchone()[0])
        return states, due, [
            "read-only inventory only; no tasks were claimed, planned, or executed",
            "self-modification, Telegram, memory projection, and tool execution are excluded",
        ]
    finally:
        if conn is not None:
            conn.close()


def _store_evidence(evidence: Path, report: ShadowReport) -> None:
    evidence.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(evidence))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hive_shadow_runs(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              observed_at REAL NOT NULL,
              source_db TEXT NOT NULL,
              integrity_ok INTEGER NOT NULL,
              task_states_json TEXT NOT NULL,
              due_tasks INTEGER NOT NULL,
              notes_json TEXT NOT NULL)
            """
        )
        conn.execute(
            """INSERT INTO hive_shadow_runs(
                 observed_at, source_db, integrity_ok, task_states_json, due_tasks, notes_json
               ) VALUES(?,?,?,?,?,?)""",
            (report.observed_at, report.source_db, int(report.integrity_ok),
             json.dumps(report.task_states, sort_keys=True), report.due_tasks,
             json.dumps(report.notes)),
        )
        conn.commit()
    finally:
        conn.close()


def as_json(report: ShadowReport) -> str:
    """Return stable, human- and machine-readable CLI output."""
    return json.dumps(asdict(report), sort_keys=True)
