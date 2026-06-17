"""
curator.py — deterministic skill lifecycle (M2 #si-2).

Ported from Hermes agent/curator.py, trimmed to its safety-critical invariants. The
Curator ages out skills Hive stops using so the toolset/prompt stays lean, WITHOUT
ever losing anything:

  - State machine: active -> stale (idle > stale_after) -> archived (idle > archive_after).
  - NEVER deletes — archived rows stay in the store and are restorable.
  - Pinned skills are exempt from every transition.
  - Only AGENT-CREATED skills are touched; human/bundled skills are left alone.
  - Pre-run backup: the full table is snapshotted before any transition, so a bad
    run is always recoverable (a backup is a named product artifact, so a file is
    allowed here despite the SQLite-first rule).

Deterministic + offline: no LLM. (Hermes also runs an LLM "umbrella consolidation"
pass; that is deferred — the lifecycle state machine is the valuable, safe core.)

DAG: memory layer, imports core + memory only.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from hive.memory.skill_usage import (
    STATE_ACTIVE,
    STATE_ARCHIVED,
    STATE_STALE,
    SkillUsage,
    SkillUsageStore,
)

log = logging.getLogger("hive.memory.curator")

_DAY = 86_400.0


@dataclass(slots=True)
class CuratorConfig:
    stale_after_days: float = 30.0
    archive_after_days: float = 90.0


@dataclass(slots=True)
class Transition:
    name: str
    from_state: str
    to_state: str


class Curator:
    def __init__(self, store: SkillUsageStore, *, config: CuratorConfig | None = None,
                 backup_dir: str | Path | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._cfg = config or CuratorConfig()
        self._backup_dir = Path(backup_dir) if backup_dir else None
        self._clock = clock

    def run(self) -> dict:
        """Apply deterministic transitions. Returns a report dict. Backs up first."""
        skills = self._store.all()
        backup_path = self._backup(skills)
        transitions: list[Transition] = []
        now = self._clock()
        stale_s = self._cfg.stale_after_days * _DAY
        archive_s = self._cfg.archive_after_days * _DAY

        for s in skills:
            if s.pinned or not s.agent_created or s.state == STATE_ARCHIVED:
                continue  # pinned/human/already-archived are left alone
            idle = s.idle_seconds(now)
            target = self._target_state(s, idle, stale_s, archive_s)
            if target != s.state:
                self._store.set_state(
                    s.name, target,
                    archived_ts=now if target == STATE_ARCHIVED else None)
                transitions.append(Transition(s.name, s.state, target))

        log.info("curator: %d skills, %d transitions", len(skills), len(transitions))
        return {
            "skills": len(skills),
            "transitions": [
                {"name": t.name, "from_state": t.from_state, "to_state": t.to_state}
                for t in transitions
            ],
            "backup": str(backup_path) if backup_path else None,
        }

    @staticmethod
    def _target_state(s: SkillUsage, idle: float, stale_s: float, archive_s: float) -> str:
        # archived takes precedence over stale; both gate on idle time.
        if idle >= archive_s:
            return STATE_ARCHIVED
        if idle >= stale_s:
            return STATE_STALE
        return s.state if s.state != STATE_STALE else STATE_STALE

    def restore(self, name: str) -> bool:
        """Bring an archived/stale skill back to active (never-delete means it's
        always still there to restore)."""
        s = self._store.get(name)
        if s is None:
            return False
        self._store.set_state(name, STATE_ACTIVE, archived_ts=None)
        return True

    def _backup(self, skills: list[SkillUsage]) -> Path | None:
        if self._backup_dir is None:
            return None
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        ts = int(self._clock())
        path = self._backup_dir / f"skills-{ts}.json"
        payload = [
            {"name": s.name, "created_ts": s.created_ts, "last_used_ts": s.last_used_ts,
             "use_count": s.use_count, "pinned": s.pinned, "state": s.state,
             "agent_created": s.agent_created, "archived_ts": s.archived_ts}
            for s in skills
        ]
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path
