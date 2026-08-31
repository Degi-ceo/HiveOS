"""Durable, operator-reviewable evidence for self-development proposals."""
from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_STATES = {"requires_review", "approved", "rejected", "candidate_failed", "draft_pr_opened", "requires_attention"}


@dataclass(frozen=True, slots=True)
class SelfDevelopmentRun:
    run_id: str
    symptom_digest: str
    discovery_decision_id: str | None
    state: str
    risk: str
    plan: str
    rationale: str
    branch: str | None
    pr_url: str | None
    test_summary: str
    lesson: str
    created_ts: float
    updated_ts: float


class SelfDevelopmentStore:
    """Append-only proposal ledger. It never creates worktrees, PRs, or approvals."""

    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._clock = clock
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS selfdev_runs(
          run_id TEXT PRIMARY KEY, symptom_digest TEXT NOT NULL, discovery_decision_id TEXT,
          state TEXT NOT NULL, risk TEXT NOT NULL, plan TEXT NOT NULL, rationale TEXT NOT NULL,
          branch TEXT, pr_url TEXT, test_summary TEXT NOT NULL, lesson TEXT NOT NULL,
          created_ts REAL NOT NULL, updated_ts REAL NOT NULL)""")
        self._db.execute("CREATE INDEX IF NOT EXISTS selfdev_runs_recent ON selfdev_runs(updated_ts DESC)")
        self._db.commit()

    @staticmethod
    def _bound(value: object, limit: int = 1000) -> str:
        return str(value or "").strip()[:limit]

    def propose(self, *, symptom: str, plan: str, rationale: str, risk: str = "review",
                discovery_decision_id: str | None = None) -> SelfDevelopmentRun:
        if not self._bound(symptom) or not self._bound(plan):
            raise ValueError("symptom and plan are required")
        if risk not in {"low", "review", "high"}:
            raise ValueError("invalid risk")
        now = self._clock()
        run_id = f"selfdev_{uuid.uuid4().hex}"
        digest = hashlib.sha256(symptom.encode()).hexdigest()
        with self._db:
            self._db.execute(
                "INSERT INTO selfdev_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, digest, self._bound(discovery_decision_id, 200) or None,
                 "requires_review", risk, self._bound(plan), self._bound(rationale),
                 None, None, "", "", now, now),
            )
        return self.get(run_id)

    def record_evidence(self, run_id: str, *, state: str, branch: str | None = None,
                        pr_url: str | None = None, test_summary: str = "", lesson: str = "") -> SelfDevelopmentRun:
        if state not in _STATES - {"requires_review"}:
            raise ValueError("invalid terminal state")
        prior = self.get(run_id)
        if prior.state != "requires_review":
            raise ValueError("proposal is already decided")
        now = self._clock()
        with self._db:
            self._db.execute(
                "UPDATE selfdev_runs SET state=?, branch=?, pr_url=?, test_summary=?, lesson=?, updated_ts=? WHERE run_id=?",
                (state, self._bound(branch, 300) or None, self._bound(pr_url, 500) or None,
                 self._bound(test_summary), self._bound(lesson), now, run_id),
            )
        return self.get(run_id)

    def get(self, run_id: str) -> SelfDevelopmentRun:
        row = self._db.execute("SELECT * FROM selfdev_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return SelfDevelopmentRun(**dict(row))

    def recent(self, *, limit: int = 10) -> list[SelfDevelopmentRun]:
        rows = self._db.execute("SELECT * FROM selfdev_runs ORDER BY updated_ts DESC, run_id DESC LIMIT ?",
                                (max(1, min(int(limit), 50)),)).fetchall()
        return [SelfDevelopmentRun(**dict(row)) for row in rows]
