"""Durable, content-free evidence for offline safe-learning evaluations."""
from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True, slots=True)
class EvaluationEvidence:
    run_id: str
    suite_id: str
    suite_version: int
    manifest_digest: str
    total: int
    passed: int
    failed: int
    errored: int
    offline_only: bool
    started_ts: float
    finished_ts: float

    @property
    def all_passed(self) -> bool:
        return self.total > 0 and self.failed == 0 and self.errored == 0 and self.passed == self.total

    def as_dict(self) -> dict[str, str | int | float | bool]:
        """Expose aggregate evidence without a scenario prompt or model output."""
        return {
            "run_id": self.run_id,
            "suite_id": self.suite_id,
            "suite_version": self.suite_version,
            "manifest_digest": self.manifest_digest,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "errored": self.errored,
            "offline_only": self.offline_only,
            "started_ts": self.started_ts,
            "finished_ts": self.finished_ts,
        }


class EvaluationEvidenceStore:
    """SQLite ledger of aggregate eval evidence; never stores conversation content."""

    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._clock = clock
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS evaluation_evidence(
          run_id TEXT PRIMARY KEY, suite_id TEXT NOT NULL, suite_version INTEGER NOT NULL,
          manifest_digest TEXT NOT NULL, total INTEGER NOT NULL, passed INTEGER NOT NULL,
          failed INTEGER NOT NULL, errored INTEGER NOT NULL, offline_only INTEGER NOT NULL,
          started_ts REAL NOT NULL, finished_ts REAL NOT NULL
        )""")
        self._db.execute("CREATE INDEX IF NOT EXISTS evaluation_evidence_recent ON evaluation_evidence(suite_id, suite_version, finished_ts DESC)")
        self._db.commit()

    def record(self, *, suite_id: str, suite_version: int, manifest_digest: str,
               total: int, passed: int, failed: int, errored: int,
               offline_only: bool, started_ts: float, finished_ts: float | None = None) -> EvaluationEvidence:
        if not suite_id or suite_version < 1 or not manifest_digest:
            raise ValueError("suite identity and manifest digest are required")
        values = (total, passed, failed, errored)
        if any(value < 0 for value in values) or passed + failed + errored != total:
            raise ValueError("invalid evaluation summary")
        run_id = f"eval_{uuid.uuid4().hex}"
        completed = self._clock() if finished_ts is None else float(finished_ts)
        with self._db:
            self._db.execute(
                "INSERT INTO evaluation_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, suite_id[:100], int(suite_version), manifest_digest[:128], *values,
                 int(bool(offline_only)), float(started_ts), completed),
            )
        return self.get(run_id)

    def get(self, run_id: str) -> EvaluationEvidence:
        row = self._db.execute("SELECT * FROM evaluation_evidence WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        data = dict(row)
        data["offline_only"] = bool(data["offline_only"])
        return EvaluationEvidence(**data)

    def latest(self, suite_id: str, suite_version: int) -> EvaluationEvidence | None:
        row = self._db.execute(
            "SELECT run_id FROM evaluation_evidence WHERE suite_id=? AND suite_version=? ORDER BY finished_ts DESC, run_id DESC LIMIT 1",
            (suite_id, int(suite_version)),
        ).fetchone()
        return self.get(str(row["run_id"])) if row else None

    def has_fresh_pass(self, suite_id: str, suite_version: int, *, max_age_seconds: float,
                       now: float | None = None) -> bool:
        if max_age_seconds < 0:
            return False
        latest = self.latest(suite_id, suite_version)
        current = self._clock() if now is None else float(now)
        return bool(latest and latest.offline_only and latest.all_passed and current - latest.finished_ts <= max_age_seconds)
