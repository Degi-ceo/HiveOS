"""Append-only, content-free evidence records for local operator procedures.

The store intentionally accepts a fixed operation/outcome vocabulary and boolean or
numeric aggregate metrics only.  It cannot be used to persist database paths,
payloads, error text, account identifiers, or credentials.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

_OPERATIONS = frozenset({"state_backup", "restore_drill", "shadow_soak"})
_OUTCOMES = frozenset({"blocked", "started", "succeeded", "failed"})
_METRIC_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True, slots=True)
class OperationEvidence:
    """One privacy-preserving local operation result."""

    sequence: int
    recorded_at: float
    operation: str
    outcome: str
    metrics: dict[str, bool | int | float]


class OperationEvidenceStore:
    """A SQLite append-only ledger for safe operational aggregates.

    The database-level triggers reject updates and deletes through this store's
    connection.  Filesystem/database administrators can still replace a SQLite
    file, so callers must treat this as operational evidence, not a tamper-proof
    audit log.
    """

    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._clock = clock
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS operation_evidence(
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              recorded_at REAL NOT NULL,
              operation TEXT NOT NULL,
              outcome TEXT NOT NULL,
              metrics_json TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS operation_evidence_no_update
              BEFORE UPDATE ON operation_evidence BEGIN
                SELECT RAISE(ABORT, 'operation evidence is append-only');
              END;
            CREATE TRIGGER IF NOT EXISTS operation_evidence_no_delete
              BEFORE DELETE ON operation_evidence BEGIN
                SELECT RAISE(ABORT, 'operation evidence is append-only');
              END;
            """
        )
        self._db.commit()

    def record(
        self,
        *,
        operation: str,
        outcome: str,
        metrics: Mapping[str, bool | int | float] | None = None,
    ) -> OperationEvidence:
        """Append one strictly aggregate evidence record and return it."""
        if operation not in _OPERATIONS:
            raise ValueError("unsupported operation evidence type")
        if outcome not in _OUTCOMES:
            raise ValueError("unsupported operation evidence outcome")
        safe_metrics = _validate_metrics(metrics or {})
        recorded_at = float(self._clock())
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO operation_evidence(recorded_at, operation, outcome, metrics_json) VALUES(?,?,?,?)",
                (recorded_at, operation, outcome, json.dumps(safe_metrics, sort_keys=True)),
            )
        return OperationEvidence(
            sequence=int(cursor.lastrowid),
            recorded_at=recorded_at,
            operation=operation,
            outcome=outcome,
            metrics=safe_metrics,
        )

    def recent(self, *, limit: int = 100) -> list[OperationEvidence]:
        """Return recent aggregates only; a non-positive limit returns no records."""
        if limit <= 0:
            return []
        rows = self._db.execute(
            "SELECT sequence, recorded_at, operation, outcome, metrics_json "
            "FROM operation_evidence ORDER BY sequence DESC LIMIT ?",
            (min(int(limit), 1000),),
        ).fetchall()
        return [
            OperationEvidence(
                sequence=int(row["sequence"]),
                recorded_at=float(row["recorded_at"]),
                operation=str(row["operation"]),
                outcome=str(row["outcome"]),
                metrics=json.loads(str(row["metrics_json"])),
            )
            for row in rows
        ]

    def close(self) -> None:
        """Close the local evidence connection without changing any records."""
        self._db.close()


def _validate_metrics(metrics: Mapping[str, bool | int | float]) -> dict[str, bool | int | float]:
    if len(metrics) > 20:
        raise ValueError("operation evidence accepts at most 20 aggregate metrics")
    validated: dict[str, bool | int | float] = {}
    for key, value in metrics.items():
        if not isinstance(key, str) or not _METRIC_KEY.fullmatch(key):
            raise ValueError("operation evidence metric names must be safe identifiers")
        if isinstance(value, bool):
            validated[key] = value
        elif isinstance(value, int):
            validated[key] = value
        elif isinstance(value, float) and math.isfinite(value):
            validated[key] = value
        else:
            raise ValueError("operation evidence metrics must be finite numeric aggregates or booleans")
    return validated
