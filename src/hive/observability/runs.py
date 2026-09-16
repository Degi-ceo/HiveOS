"""Durable, redacted execution records for operator-visible Hive runs.

``EventBus`` intentionally remains synchronous and in-memory.  This module is
its durable counterpart: it records a small, safe event envelope so an
operator can inspect a completed or interrupted run after the process exits.
It is not a transcript store and must never retain model reasoning or raw tool
payloads.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from hive.core.events import Event, EventBus, EventType
from hive.core.process import process_is_alive as _process_is_alive
from hive.core.redact import redact_value

_TERMINAL_STATES = frozenset({"ok", "error", "cancelled"})
_LOCAL_INTERRUPTION_ERROR = "process ended before run completion"


class RunLedger:
    """Append-only event ledger and lifecycle state for one HiveOS database."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        process_id: int | None = None,
        hostname: str | None = None,
        process_is_alive: Callable[[int], bool] = _process_is_alive,
    ) -> None:
        self._path = str(db_path)
        self._clock = clock
        self._process_id = os.getpid() if process_id is None else int(process_id)
        self._hostname = socket.gethostname() if hostname is None else str(hostname)
        self._process_is_alive = process_is_alive
        self._lock = threading.RLock()
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS hive_runs(
                  run_id TEXT PRIMARY KEY,
                  kind TEXT NOT NULL,
                  session_id TEXT NOT NULL DEFAULT '',
                  parent_run_id TEXT NOT NULL DEFAULT '',
                  state TEXT NOT NULL CHECK(state IN ('running', 'ok', 'error', 'cancelled')),
                  started_ts REAL NOT NULL,
                  ended_ts REAL,
                  error TEXT NOT NULL DEFAULT '',
                  owner_host TEXT NOT NULL DEFAULT '',
                  owner_pid INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_hive_runs_started ON hive_runs(started_ts DESC);
                CREATE INDEX IF NOT EXISTS idx_hive_runs_session ON hive_runs(session_id, started_ts DESC);
                CREATE TABLE IF NOT EXISTS hive_run_events(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT NOT NULL,
                  ts REAL NOT NULL,
                  event_type TEXT NOT NULL,
                  data_json TEXT NOT NULL,
                  FOREIGN KEY(run_id) REFERENCES hive_runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_hive_run_events_run ON hive_run_events(run_id, id);
                """
            )
            columns = {str(row[1]) for row in self._db.execute("PRAGMA table_info(hive_runs)")}
            if "owner_host" not in columns:
                self._db.execute("ALTER TABLE hive_runs ADD COLUMN owner_host TEXT NOT NULL DEFAULT ''")
            if "owner_pid" not in columns:
                self._db.execute("ALTER TABLE hive_runs ADD COLUMN owner_pid INTEGER NOT NULL DEFAULT 0")
            if "parent_run_id" not in columns:
                self._db.execute("ALTER TABLE hive_runs ADD COLUMN parent_run_id TEXT NOT NULL DEFAULT ''")

    def attach(self, bus: EventBus) -> "RunLedger":
        """Persist every event that carries a non-empty ``run_id``."""
        for event_type in EventType:
            bus.subscribe(event_type, self.record_event)
        return self

    def begin(self, run_id: str, *, kind: str, session_id: str = "") -> None:
        """Record a new running operation. Reusing an id is deliberately rejected."""
        normalized = str(run_id).strip()
        if not normalized:
            raise ValueError("run_id must not be empty")
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO hive_runs(run_id, kind, session_id, state, started_ts, owner_host, owner_pid) "
                "VALUES (?, ?, ?, 'running', ?, ?, ?)",
                (normalized, str(kind or "unknown"), str(session_id or ""), self._clock(),
                 self._hostname, self._process_id),
            )

    def finish(self, run_id: str, *, state: str, error: str = "") -> None:
        """Finish a started run exactly once; an old terminal result cannot change."""
        if state not in _TERMINAL_STATES:
            raise ValueError(f"unsupported terminal run state: {state}")
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE hive_runs SET state=?, ended_ts=?, error=? "
                "WHERE run_id=? AND state='running'",
                (state, self._clock(), str(redact_value(error)), str(run_id)),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"run {run_id!r} is missing or already terminal")

    def record_event(self, event: Event) -> None:
        """Persist a redacted event only when it is correlated to a known run."""
        run_id = str(event.data.get("run_id") or "")
        if not run_id:
            return
        payload = redact_value(dict(event.data))
        with self._lock, self._db:
            exists = self._db.execute(
                "SELECT 1 FROM hive_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if exists is None:
                return
            child_run_id = str(payload.get("subagent_run_id") or "")
            if event.event_type is EventType.SUBAGENT_STARTED and child_run_id:
                parent = self._db.execute(
                    "SELECT session_id FROM hive_runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if parent is not None:
                    self._db.execute(
                        "INSERT OR IGNORE INTO hive_runs("
                        "run_id, kind, session_id, parent_run_id, state, started_ts, owner_host, owner_pid"
                        ") VALUES (?, 'subagent', ?, ?, 'running', ?, ?, ?)",
                        (child_run_id, str(parent["session_id"]), run_id, float(event.timestamp),
                         self._hostname, self._process_id),
                    )
                    self._db.execute(
                        "INSERT INTO hive_run_events(run_id, ts, event_type, data_json) VALUES (?, ?, ?, ?)",
                        (child_run_id, float(event.timestamp), event.event_type.value,
                         json.dumps(payload, sort_keys=True, default=str)),
                    )
            elif event.event_type in {EventType.SUBAGENT_COMPLETED, EventType.SUBAGENT_FAILED} and child_run_id:
                child_state = "ok" if event.event_type is EventType.SUBAGENT_COMPLETED else "error"
                self._db.execute(
                    "UPDATE hive_runs SET state=?, ended_ts=?, error=? "
                    "WHERE run_id=? AND state='running'",
                    (child_state, float(event.timestamp), "" if child_state == "ok" else "subagent failed",
                     child_run_id),
                )
                self._db.execute(
                    "INSERT INTO hive_run_events(run_id, ts, event_type, data_json) "
                    "SELECT ?, ?, ?, ? WHERE EXISTS(SELECT 1 FROM hive_runs WHERE run_id=?)",
                    (child_run_id, float(event.timestamp), event.event_type.value,
                     json.dumps(payload, sort_keys=True, default=str), child_run_id),
                )
            self._db.execute(
                "INSERT INTO hive_run_events(run_id, ts, event_type, data_json) VALUES (?, ?, ?, ?)",
                (run_id, float(event.timestamp), event.event_type.value,
                 json.dumps(payload, sort_keys=True, default=str)),
            )

    def record_operator_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Append an already-public operator envelope for later safe replay.

        Re-project the input through ``public_operator_event`` at this sink as
        defence in depth: a future caller cannot turn the durable ledger into
        a raw tool-payload store by passing a hand-built dictionary.
        """
        run_id = str(event.get("run_id") or "")
        event_type = str(event.get("type") or "status")
        from hive.observability.operator_events import _PUBLIC_EVENT_TYPES, public_operator_event

        if not run_id or event_type not in _PUBLIC_EVENT_TYPES:
            return None
        with self._lock, self._db:
            run = self._db.execute(
                "SELECT session_id FROM hive_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                return None
            last = self._db.execute(
                "SELECT data_json FROM hive_run_events WHERE run_id=? AND event_type LIKE 'operator.%' "
                "ORDER BY id DESC LIMIT 1", (run_id,),
            ).fetchone()
            try:
                prior = json.loads(str(last["data_json"])) if last is not None else {}
                sequence = max(0, int(prior.get("sequence", 0))) + 1
            except (TypeError, ValueError):
                sequence = 1
            payload = public_operator_event(
                event, run_id=run_id, session_id=str(run["session_id"]), sequence=sequence,
                timestamp=float(event.get("timestamp") or self._clock()),
            )
            duration = event.get("duration_ms")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                payload["duration_ms"] = max(0, round(duration))
            payload = redact_value(payload)
            self._db.execute(
                "INSERT INTO hive_run_events(run_id, ts, event_type, data_json) VALUES (?, ?, ?, ?)",
                (run_id, float(payload.get("timestamp") or self._clock()),
                 f"operator.{event_type}", json.dumps(payload, sort_keys=True, default=str)),
            )
        return payload

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT run_id, kind, session_id, parent_run_id, state, started_ts, ended_ts, error "
                "FROM hive_runs WHERE run_id=?", (str(run_id),)
            ).fetchone()
        return dict(row) if row is not None else None

    def recent(self, *, limit: int = 20, session_id: str | None = None) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        sql = "SELECT run_id, kind, session_id, parent_run_id, state, started_ts, ended_ts, error FROM hive_runs"
        params: tuple[Any, ...] = ()
        if session_id is not None:
            sql += " WHERE session_id=?"
            params = (str(session_id),)
        sql += " ORDER BY started_ts DESC LIMIT ?"
        with self._lock:
            rows = self._db.execute(sql, params + (safe_limit,)).fetchall()
        return [dict(row) for row in rows]

    def children(self, parent_run_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return durable child runs for one parent without exposing tool payloads."""
        safe_limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._db.execute(
                "SELECT run_id, kind, session_id, parent_run_id, state, started_ts, ended_ts, error "
                "FROM hive_runs WHERE parent_run_id=? ORDER BY started_ts ASC LIMIT ?",
                (str(parent_run_id), safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def events(self, run_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 1000))
        with self._lock:
            rows = self._db.execute(
                "SELECT id, ts, event_type, data_json FROM hive_run_events "
                "WHERE run_id=? ORDER BY id ASC LIMIT ?", (str(run_id), safe_limit),
            ).fetchall()
        return [
            {"id": int(row["id"]), "ts": float(row["ts"]), "type": str(row["event_type"]),
             "data": json.loads(str(row["data_json"]))}
            for row in rows
        ]

    def public_events(self, run_id: str, *, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        """Return a cursorable, durable replay of already-public event envelopes.

        This deliberately reads only the ``operator.*`` projection.  The full
        run-event table contains redacted execution records but is not a public
        terminal or gateway transport contract.
        """
        safe_after = max(0, int(after_id))
        safe_limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._db.execute(
                "SELECT id, ts, event_type, data_json FROM hive_run_events "
                "WHERE run_id=? AND event_type LIKE 'operator.%' AND id>? "
                "ORDER BY id ASC LIMIT ?",
                (str(run_id), safe_after, safe_limit),
            ).fetchall()
        return self._project_public_events(rows)

    @staticmethod
    def _project_public_data(event_type: str, raw: object) -> dict[str, Any]:
        """Allowlist operator fields for the execution-observability transport."""
        source = raw if isinstance(raw, dict) else {}

        def text(name: str, limit: int = 96) -> str:
            return str(source.get(name) or "")[:limit]

        def number(name: str) -> int:
            value = source.get(name)
            return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0

        data: dict[str, Any] = {"sequence": number("sequence")}
        if event_type == "model_decision":
            data["turn"] = number("turn")
            calls = source.get("tool_calls")
            data["tool_calls"] = [
                {"id": str(call.get("id") or "")[:128], "name": str(call.get("name") or "tool")[:96]}
                for call in (calls if isinstance(calls, list) else []) if isinstance(call, dict)
            ][:100]
        elif event_type in {"tool_call_start", "tool_call_end"}:
            data.update({"turn": number("turn"), "id": text("id", 128), "name": text("name")})
            if event_type == "tool_call_end":
                status = text("status", 32)
                data["status"] = status
                data["duration_ms"] = max(0, min(number("duration_ms"), 86_400_000))
                data["summary"] = {
                    "ok": "completed", "approved": "completed", "pending": "awaiting approval",
                }.get(status.casefold(), "failed" if status.casefold() in {"error", "failed"} else "finished")
        elif event_type in {"final", "max_turns"}:
            # Final text is a conversation payload, not execution progress.
            data.update({"turn": number("turn"), "tool_calls": number("tool_calls")})
        elif event_type == "loop_guard":
            data.update({"turn": number("turn"), "name": text("name")})
        elif event_type == "error":
            data["class"] = text("class", 120)
        elif event_type in {"subagent_start", "subagent_end"}:
            data.update({"turn": number("turn"), "id": text("id", 128), "agent": text("agent", 64)})
            if event_type == "subagent_end":
                data["status"] = text("status", 32)
        elif event_type == "operator_action":
            data.update({"name": text("name"), "status": text("status", 32)})
        elif event_type == "specialist_lifecycle":
            states = {"queued", "running", "review_required", "completed", "failed", "cancelled", "interrupted"}
            status = text("status", 32).casefold()
            data.update({
                "id": text("id", 128), "agent": text("agent", 64),
                "status": status if status in states else "failed",
                "attempt": max(0, min(number("attempt"), 1000)),
            })
        elif event_type == "candidate_check":
            states = {"started", "passed", "failed", "cancelled"}
            status = text("status", 32).casefold()
            data.update({
                "edit_id": text("edit_id", 128), "delegation_id": text("delegation_id", 128),
                "check_kind": text("check_kind", 32) if text("check_kind", 32) in {"pytest", "compileall", "ruff"} else "pytest",
                "status": status if status in states else "failed",
                "duration_ms": max(0, min(number("duration_ms"), 86_400_000)),
            })
        return data

    @classmethod
    def _project_public_events(cls, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        """Project selected operator rows through the execution-public boundary."""
        events: list[dict[str, Any]] = []
        for row in rows:
            try:
                data = json.loads(str(row["data_json"]))
            except (TypeError, ValueError):
                data = {}
            event_type = str(row["event_type"])[len("operator."):]
            events.append({
                "id": int(row["id"]),
                "ts": float(row["ts"]),
                "type": event_type,
                "data": cls._project_public_data(event_type, data),
            })
        return events

    def _recent_public_events(self, run_id: str, *, limit: int) -> list[dict[str, Any]]:
        """Return the latest bounded public window for current-state derivation."""
        safe_limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._db.execute(
                "SELECT id, ts, event_type, data_json FROM hive_run_events "
                "WHERE run_id=? AND event_type LIKE 'operator.%' "
                "ORDER BY id DESC LIMIT ?",
                (str(run_id), safe_limit),
            ).fetchall()
        return self._project_public_events(list(reversed(rows)))

    def _public_run(self, row: dict[str, Any]) -> dict[str, Any]:
        """Project durable run state without session, owner, or error contents."""
        started = float(row["started_ts"])
        ended = row.get("ended_ts")
        return {
            "run_id": str(row["run_id"]),
            "kind": str(row["kind"]),
            "parent_run_id": str(row.get("parent_run_id") or ""),
            "state": str(row["state"]),
            "started_ts": started,
            "ended_ts": float(ended) if ended is not None else None,
            "duration_ms": max(0, round(((float(ended) if ended is not None else self._clock()) - started) * 1000)),
            "interrupted_local": (
                str(row["state"]) == "cancelled"
                and str(row.get("error") or "") == _LOCAL_INTERRUPTION_ERROR
            ),
        }

    def snapshot(self, run_id: str) -> dict[str, Any] | None:
        """Return a safe, computed execution snapshot for one durable run."""
        run = self.get(run_id)
        if run is None:
            return None
        public_events = self._recent_public_events(run_id, limit=500)
        children = self.children(run_id, limit=500)
        phase = "running"
        active_tool = ""
        for event in public_events:
            data = event["data"]
            event_type = event["type"]
            if event_type == "model_decision":
                phase = "planning"
            elif event_type == "tool_call_start":
                phase = "executing_tool"
                active_tool = str(data.get("name") or "")[:96]
            elif event_type == "tool_call_end":
                active_tool = ""
                phase = "waiting_approval" if str(data.get("status")) == "pending" else "running"
            elif event_type == "subagent_start":
                phase = "waiting_subagent"
            elif event_type == "subagent_end":
                phase = "running"
        state = str(run["state"])
        if state == "ok":
            phase = "completed"
        elif state == "error":
            phase = "failed"
        elif state == "cancelled":
            phase = "cancelled"
        child_states = {name: 0 for name in ("running", "ok", "error", "cancelled")}
        for child in children:
            child_state = str(child.get("state") or "")
            if child_state in child_states:
                child_states[child_state] += 1
        snapshot = self._public_run(run)
        snapshot.update({
            "phase": phase,
            "active_tool": active_tool,
            "tool_event_count": sum(event["type"] == "tool_call_end" for event in public_events),
            "child_runs": {"total": len(children), **child_states},
            "last_event_id": public_events[-1]["id"] if public_events else 0,
        })
        return snapshot

    def tree(self, run_id: str, *, max_depth: int = 8, max_nodes: int = 200) -> dict[str, Any] | None:
        """Return a bounded, cycle-safe public tree rooted at ``run_id``."""
        root = self.snapshot(run_id)
        if root is None:
            return None
        depth_limit = max(0, min(int(max_depth), 16))
        node_limit = max(1, min(int(max_nodes), 500))
        seen = {str(run_id)}
        remaining = node_limit - 1
        truncated = False

        def build(node: dict[str, Any], depth: int) -> dict[str, Any]:
            nonlocal remaining, truncated
            result = dict(node)
            result["children"] = []
            if depth >= depth_limit:
                if self.children(node["run_id"], limit=1):
                    truncated = True
                return result
            for child in self.children(node["run_id"], limit=500):
                child_id = str(child["run_id"])
                if remaining <= 0:
                    truncated = True
                    break
                if child_id in seen:
                    truncated = True
                    continue
                child_snapshot = self.snapshot(child_id)
                if child_snapshot is None:
                    continue
                seen.add(child_id)
                remaining -= 1
                result["children"].append(build(child_snapshot, depth + 1))
            return result

        return {"root": build(root, 0), "truncated": truncated, "node_count": len(seen)}

    def recover_interrupted(self, run_id: str | None = None) -> int:
        """Recover only locally owned runs whose recorded process is no longer alive.

        A shared state database may be used by a gateway and a local CLI at the
        same time.  Recovery must therefore not treat every foreign ``running``
        row as a crash.  Rows from a remote host are deliberately left for that
        host to recover; legacy rows without an owner remain recoverable.
        """
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT run_id, owner_host, owner_pid FROM hive_runs WHERE state='running'"
                + (" AND run_id=?" if run_id else ""),
                ((str(run_id),) if run_id else ()),
            ).fetchall()
            run_ids = [
                str(row["run_id"])
                for row in rows
                if not str(row["owner_host"])
                or (
                    str(row["owner_host"]) == self._hostname
                    and not self._process_is_alive(int(row["owner_pid"]))
                )
            ]
            recovered = 0
            for run_id in run_ids:
                cursor = self._db.execute(
                    "UPDATE hive_runs SET state='cancelled', ended_ts=?, error=? "
                    "WHERE run_id=? AND state='running'",
                    (self._clock(), _LOCAL_INTERRUPTION_ERROR, run_id),
                )
                recovered += max(0, int(cursor.rowcount))
        return recovered

    def close(self) -> None:
        with self._lock:
            self._db.close()
