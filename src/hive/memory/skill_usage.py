"""
skill_usage.py — SQLite-backed skill usage tracking (M2 #si-2).

Ported from Hermes tools/skill_usage.py, but SQLite-first (OpenClaw rule) instead of
the Hermes `.usage.json` sidecar. Tracks, per named skill: provenance (was it
agent-created?), a pin flag, use count, created/last-used timestamps, and lifecycle
state. The Curator (memory/curator.py) reads this to drive active->stale->archived
transitions.

Placement note: the plan listed tools/skill_usage.py, but memory may import only
core (DAG), and the Curator that consumes this lives in memory next to the keeper
(both are sleep-time consolidation). So the store lives in memory/ too. It tracks
skills by NAME only — it never imports the tool registry, staying decoupled.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Lifecycle states. active -> stale -> archived (never deleted). Pinned skips all.
STATE_ACTIVE = "active"
STATE_STALE = "stale"
STATE_ARCHIVED = "archived"


@dataclass(slots=True)
class SkillUsage:
    name: str
    created_ts: float
    last_used_ts: float
    use_count: int
    pinned: bool
    state: str
    agent_created: bool
    archived_ts: float | None = None

    def idle_seconds(self, now: float) -> float:
        return max(0.0, now - self.last_used_ts)


class SkillUsageStore:
    """One SQLite table tracking skill usage + lifecycle. WAL like the other stores."""

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
            CREATE TABLE IF NOT EXISTS skill_usage(
              name         TEXT PRIMARY KEY,
              created_ts   REAL NOT NULL,
              last_used_ts REAL NOT NULL,
              use_count    INTEGER NOT NULL DEFAULT 0,
              pinned       INTEGER NOT NULL DEFAULT 0,
              state        TEXT NOT NULL DEFAULT 'active',
              agent_created INTEGER NOT NULL DEFAULT 1,
              archived_ts  REAL);
            """
        )
        self._db.commit()

    def register(self, name: str, *, agent_created: bool = True,
                 pinned: bool = False) -> None:
        """Idempotent: create the row if new; leave an existing row untouched."""
        now = self._clock()
        self._db.execute(
            "INSERT OR IGNORE INTO skill_usage"
            "(name, created_ts, last_used_ts, use_count, pinned, state, agent_created)"
            " VALUES(?,?,?,0,?,?,?)",
            (name, now, now, int(pinned), STATE_ACTIVE, int(agent_created)),
        )
        self._db.commit()

    def record_use(self, name: str) -> None:
        """Bump use count + last-used, auto-registering an unknown skill. A used
        skill is never stale: a stale row is reactivated (archived stays archived
        until an explicit restore — usage alone doesn't un-archive)."""
        now = self._clock()
        cur = self._db.execute("SELECT state FROM skill_usage WHERE name=?", (name,))
        row = cur.fetchone()
        if row is None:
            self.register(name)
        new_state_clause = ""
        if row is not None and row["state"] == STATE_STALE:
            new_state_clause = f", state='{STATE_ACTIVE}'"
        self._db.execute(
            f"UPDATE skill_usage SET use_count=use_count+1, last_used_ts=?{new_state_clause}"
            " WHERE name=?",
            (now, name),
        )
        self._db.commit()

    def set_pinned(self, name: str, pinned: bool) -> None:
        self._db.execute("UPDATE skill_usage SET pinned=? WHERE name=?",
                         (int(pinned), name))
        self._db.commit()

    def set_state(self, name: str, state: str, *, archived_ts: float | None = None) -> None:
        self._db.execute("UPDATE skill_usage SET state=?, archived_ts=? WHERE name=?",
                         (state, archived_ts, name))
        self._db.commit()

    def get(self, name: str) -> SkillUsage | None:
        row = self._db.execute("SELECT * FROM skill_usage WHERE name=?", (name,)).fetchone()
        return _row_to_usage(row) if row else None

    def all(self) -> list[SkillUsage]:
        rows = self._db.execute("SELECT * FROM skill_usage ORDER BY name").fetchall()
        return [_row_to_usage(r) for r in rows]

    def by_state(self, state: str) -> list[SkillUsage]:
        """Return skills in a given lifecycle state (active|stale|archived)."""
        rows = self._db.execute(
            "SELECT * FROM skill_usage WHERE state=? ORDER BY name", (state,)
        ).fetchall()
        return [_row_to_usage(r) for r in rows]

    def top_used(self, limit: int = 10) -> list[SkillUsage]:
        """Return the most frequently used skills, descending."""
        rows = self._db.execute(
            "SELECT * FROM skill_usage ORDER BY use_count DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_usage(r) for r in rows]

    def stats(self) -> dict:
        """Summary counts by state."""
        total_row = self._db.execute("SELECT COUNT(*) AS n FROM skill_usage").fetchone()
        state_rows = self._db.execute(
            "SELECT state, COUNT(*) AS n FROM skill_usage GROUP BY state"
        ).fetchall()
        return {
            "total": int(total_row["n"]),
            "by_state": {r["state"]: r["n"] for r in state_rows},
        }

    def names(self, state: str | None = None) -> list[str]:
        """Return skill names, optionally filtered to a lifecycle state."""
        if state is not None:
            rows = self._db.execute(
                "SELECT name FROM skill_usage WHERE state=? ORDER BY name", (state,)
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT name FROM skill_usage ORDER BY name"
            ).fetchall()
        return [r["name"] for r in rows]

    def pin(self, name: str) -> bool:
        """Mark a skill as pinned (prevents archiving). Returns False if skill not found."""
        cur = self._db.execute(
            "UPDATE skill_usage SET pinned=1 WHERE name=?", (name,)
        )
        self._db.commit()
        return cur.rowcount > 0

    def pinned_names(self) -> list[str]:
        """Return names of skills currently flagged as pinned (SPRINT_6 P-I T2.3)."""
        rows = self._db.execute(
            "SELECT name FROM skill_usage WHERE pinned=1 ORDER BY name"
        ).fetchall()
        return [r["name"] for r in rows]

    def unpin(self, name: str) -> bool:
        """Remove the pin from a skill. Returns False if skill not found."""
        cur = self._db.execute(
            "UPDATE skill_usage SET pinned=0 WHERE name=?", (name,)
        )
        self._db.commit()
        return cur.rowcount > 0

    def recently_used(self, limit: int = 10) -> list[SkillUsage]:
        """Return skills with at least one use, ordered by most recently used (DESC)."""
        rows = self._db.execute(
            "SELECT * FROM skill_usage WHERE use_count > 0 "
            "ORDER BY last_used_ts DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [_row_to_usage(r) for r in rows]

    def unused_skills(self) -> list[SkillUsage]:
        """Return active (non-archived) skills that have never been used (use_count == 0)."""
        rows = self._db.execute(
            "SELECT * FROM skill_usage WHERE use_count=0 AND state != 'archived' ORDER BY name"
        ).fetchall()
        return [_row_to_usage(r) for r in rows]

    def archived_count(self) -> int:
        """Return the number of skills with state='archived'."""
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM skill_usage WHERE state='archived'"
        ).fetchone()
        return int(row["n"]) if row else 0

    def delete(self, name: str) -> bool:
        """Remove a skill from tracking permanently. Returns False if not found."""
        cur = self._db.execute("DELETE FROM skill_usage WHERE name=?", (name,))
        self._db.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        self._db.close()


def _row_to_usage(row: sqlite3.Row) -> SkillUsage:
    return SkillUsage(
        name=row["name"], created_ts=row["created_ts"], last_used_ts=row["last_used_ts"],
        use_count=row["use_count"], pinned=bool(row["pinned"]), state=row["state"],
        agent_created=bool(row["agent_created"]), archived_ts=row["archived_ts"],
    )
