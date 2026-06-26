"""
storage.py — SQLite helpers for the learning loop (SPRINT_6 P-F).

Two tables:
- ``learning_traces`` — one row per tool-call observation (tracer writes)
- ``learning_loops``  — one row per learning-loop run (loop writes verdict)

Schema is created lazily on first call to ``ensure_schema()``. The DB path
comes from ``hive.config.state_db_path`` (existing convention from
``autonomy/tasks.py:TaskBoard``).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from hive.core.types import VERDICT_ACCEPT, VERDICT_REJECT, LoopOutcome, TraceRow


def ensure_schema(db_path: str | Path) -> None:
    """Create the two learning tables if they don't exist (idempotent)."""
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS learning_traces(
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          ts            REAL NOT NULL,
          session_id    TEXT NOT NULL,
          tool          TEXT NOT NULL,
          args_json     TEXT NOT NULL DEFAULT '{}',
          outcome       TEXT NOT NULL,
          latency_ms    REAL NOT NULL DEFAULT 0,
          error_class   TEXT,
          error_message TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_traces_session ON learning_traces(session_id);
        CREATE INDEX IF NOT EXISTS idx_traces_ts      ON learning_traces(ts);

        CREATE TABLE IF NOT EXISTS learning_loops(
          id                 INTEGER PRIMARY KEY AUTOINCREMENT,
          ts                 REAL NOT NULL,
          symptom            TEXT NOT NULL,
          verdict            TEXT NOT NULL,
          pytest_baseline    REAL NOT NULL DEFAULT 0.0,
          pytest_candidate   REAL NOT NULL DEFAULT 0.0,
          evals_baseline     REAL NOT NULL DEFAULT 0.0,
          evals_candidate    REAL NOT NULL DEFAULT 0.0,
          worktree_branch    TEXT,
          pr_url             TEXT,
          reject_reason      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_loops_ts ON learning_loops(ts);
        """
    )
    conn.commit()
    conn.close()


def insert_trace(db_path: str | Path, row: TraceRow) -> int:
    """Persist a tracer observation. Returns the new row id."""
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    cur = conn.execute(
        """
        INSERT INTO learning_traces
          (ts, session_id, tool, args_json, outcome, latency_ms, error_class, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.ts,
            row.session_id,
            row.tool,
            json.dumps(row.args, default=str),
            row.outcome,
            row.latency_ms,
            row.error_class,
            row.error_message,
        ),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return int(rid) if rid is not None else 0


def query_traces(
    db_path: str | Path,
    *,
    outcome: str | None = None,
    since_ts: float | None = None,
    limit: int = 50,
) -> list[TraceRow]:
    """Read recent traces, newest first. Filters are AND-combined."""
    clauses = []
    params: list[Any] = []
    if outcome is not None:
        clauses.append("outcome = ?")
        params.append(outcome)
    if since_ts is not None:
        clauses.append("ts >= ?")
        params.append(since_ts)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT * FROM learning_traces {where} ORDER BY ts DESC LIMIT ?",
        params,
    ).fetchall()
    out = [
        TraceRow(
            id=int(r["id"]),
            ts=float(r["ts"]),
            session_id=str(r["session_id"]),
            tool=str(r["tool"]),
            args=json.loads(r["args_json"]) if r["args_json"] else {},
            outcome=str(r["outcome"]),
            latency_ms=float(r["latency_ms"]),
            error_class=r["error_class"],
            error_message=r["error_message"],
        )
        for r in rows
    ]
    conn.close()
    return out


def insert_loop(db_path: str | Path, outcome: LoopOutcome) -> int:
    """Persist a learning-loop run. Returns the new row id."""
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    cur = conn.execute(
        """
        INSERT INTO learning_loops
          (ts, symptom, verdict, pytest_baseline, pytest_candidate,
           evals_baseline, evals_candidate, worktree_branch, pr_url, reject_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            outcome.ts,
            outcome.symptom,
            outcome.verdict,
            outcome.pytest_baseline,
            outcome.pytest_candidate,
            outcome.evals_baseline,
            outcome.evals_candidate,
            outcome.worktree_branch,
            outcome.pr_url,
            outcome.reject_reason,
        ),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return int(rid) if rid is not None else 0


def query_loops(db_path: str | Path, *, limit: int = 50) -> list[LoopOutcome]:
    """Read recent loop runs, newest first."""
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM learning_loops ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = [
        LoopOutcome(
            id=int(r["id"]),
            ts=float(r["ts"]),
            symptom=str(r["symptom"]),
            verdict=str(r["verdict"]),
            pytest_baseline=float(r["pytest_baseline"]),
            pytest_candidate=float(r["pytest_candidate"]),
            evals_baseline=float(r["evals_baseline"]),
            evals_candidate=float(r["evals_candidate"]),
            worktree_branch=r["worktree_branch"],
            pr_url=r["pr_url"],
            reject_reason=r["reject_reason"],
        )
        for r in rows
    ]
    conn.close()
    return out


def count_by_verdict(db_path: str | Path) -> dict[str, int]:
    """Aggregate counts grouped by verdict — used by ``hive learning status``."""
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    rows = conn.execute(
        "SELECT verdict, COUNT(*) AS n FROM learning_loops GROUP BY verdict"
    ).fetchall()
    counts = {VERDICT_ACCEPT: 0, VERDICT_REJECT: 0}
    for verdict, n in rows:
        counts[str(verdict)] = int(n)
    conn.close()
    return counts
