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
    task_kinds: dict[str, int]
    task_sources: dict[str, int]
    due_tasks: int
    review_tasks: int
    expired_running_tasks: int
    unleased_running_tasks: int
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
    inventory = _read_work_inventory(source, observed_at)
    report = ShadowReport(
        observed_at=observed_at,
        source_db=str(source.resolve()),
        evidence_db=str(evidence.resolve()),
        integrity_ok=True,
        **inventory,
    )
    _store_evidence(evidence, report)
    return report


def _read_work_inventory(source: Path, now: float) -> dict:
    conn: sqlite3.Connection | None = None
    try:
        uri = source.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("PRAGMA query_only=ON")
        has_tasks = conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='hive_tasks'"
        ).fetchone()
        if not has_tasks:
            return {
                "task_states": {}, "task_kinds": {}, "task_sources": {},
                "due_tasks": 0, "review_tasks": 0, "expired_running_tasks": 0,
                "unleased_running_tasks": 0,
                "notes": ("hive_tasks table is absent; no executable work was inspected",),
            }
        states = _counts(conn, "state")
        kinds = _counts(conn, "kind")
        sources = _counts(conn, "source")
        due = _count(conn, "SELECT COUNT(*) FROM hive_tasks WHERE state='pending' AND scheduled_for<=?", now)
        review = _count(conn, "SELECT COUNT(*) FROM hive_tasks WHERE state='requires_review'")
        expired = _count(
            conn,
            "SELECT COUNT(*) FROM hive_tasks WHERE state='running' AND lease_until IS NOT NULL AND lease_until<=?",
            now,
        )
        unleased = _count(
            conn,
            "SELECT COUNT(*) FROM hive_tasks WHERE state='running' AND lease_until IS NULL",
        )
        return {
            "task_states": states, "task_kinds": kinds, "task_sources": sources,
            "due_tasks": due, "review_tasks": review, "expired_running_tasks": expired,
            "unleased_running_tasks": unleased,
            "notes": (
                "read-only aggregate inventory only; no task payloads or error details were read",
                "no tasks were claimed, planned, executed, or replayed",
                "self-modification, Telegram, memory projection, and tool execution are excluded",
            ),
        }
    finally:
        if conn is not None:
            conn.close()


def _counts(conn: sqlite3.Connection, column: str) -> dict[str, int]:
    rows = conn.execute(f"SELECT {column}, COUNT(*) FROM hive_tasks GROUP BY {column}").fetchall()
    return {_safe_label(row[0]): int(row[1]) for row in rows}


def _count(conn: sqlite3.Connection, query: str, *params: object) -> int:
    return int(conn.execute(query, params).fetchone()[0])


def _safe_label(value: object) -> str:
    """Keep schema labels bounded so evidence never becomes an unbounded data export."""
    text = str(value or "(unset)").replace("\r", " ").replace("\n", " ").strip()
    return text[:80] or "(unset)"


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
              task_kinds_json TEXT NOT NULL DEFAULT '{}',
              task_sources_json TEXT NOT NULL DEFAULT '{}',
              due_tasks INTEGER NOT NULL,
              review_tasks INTEGER NOT NULL DEFAULT 0,
              expired_running_tasks INTEGER NOT NULL DEFAULT 0,
              unleased_running_tasks INTEGER NOT NULL DEFAULT 0,
              notes_json TEXT NOT NULL)
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(hive_shadow_runs)")}
        for name, ddl in (
            ("task_kinds_json", "TEXT NOT NULL DEFAULT '{}" + "'"),
            ("task_sources_json", "TEXT NOT NULL DEFAULT '{}" + "'"),
            ("review_tasks", "INTEGER NOT NULL DEFAULT 0"),
            ("expired_running_tasks", "INTEGER NOT NULL DEFAULT 0"),
            ("unleased_running_tasks", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE hive_shadow_runs ADD COLUMN {name} {ddl}")
        conn.execute(
            """INSERT INTO hive_shadow_runs(
                 observed_at, source_db, integrity_ok, task_states_json, task_kinds_json,
                 task_sources_json, due_tasks, review_tasks, expired_running_tasks,
                 unleased_running_tasks, notes_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                report.observed_at, report.source_db, int(report.integrity_ok),
                json.dumps(report.task_states, sort_keys=True),
                json.dumps(report.task_kinds, sort_keys=True),
                json.dumps(report.task_sources, sort_keys=True), report.due_tasks,
                report.review_tasks, report.expired_running_tasks,
                report.unleased_running_tasks, json.dumps(report.notes),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def as_json(report: ShadowReport) -> str:
    """Return stable, human- and machine-readable CLI output."""
    return json.dumps(asdict(report), sort_keys=True)
