"""Durable, fenced lifecycle records for delegated specialist work."""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from hive.agents.profiles import specialist_profile
from hive.core.process import process_is_alive
from hive.core.redact import redact_value

QUEUED, RUNNING, REVIEW_REQUIRED, COMPLETED, FAILED, CANCELLED = (
    "queued", "running", "review_required", "completed", "failed", "cancelled",
)


@dataclass(frozen=True, slots=True)
class DelegationRecord:
    id: str
    parent_run_id: str
    child_run_id: str
    role: str
    state: str
    attempts: int
    max_attempts: int
    created_ts: float
    updated_ts: float
    safe_summary: str = ""
    owner_host: str = ""
    owner_pid: int = 0
    parent_delegation_id: str = ""
    root_delegation_id: str = ""
    depth: int = 0
    max_depth: int = 0
    max_children: int = 0
    target_machine_id: str = ""
    granted_tools: tuple[str, ...] = ()
    max_worker_turns: int = 0
    max_worker_tool_calls: int = 0
    max_worker_seconds: int = 0
    branch_max_turns: int = 0
    branch_reserved_turns: int = 0
    branch_max_tool_calls: int = 0
    branch_reserved_tool_calls: int = 0
    branch_max_seconds: int = 0
    branch_reserved_seconds: int = 0
    max_active_children: int = 0
    capability_id: str = ""
    capability_state: str = ""
    capability_deadline_ts: float = 0.0


class DelegationLedger:
    """SQLite state machine which stores no raw task prompt or worker output."""

    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time,
                 process_id: int | None = None, hostname: str | None = None,
                 machine_identity: str | None = None,
                 process_is_alive: Callable[[int], bool] = process_is_alive) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._db.execute("PRAGMA busy_timeout=5000")
        self._configure_journal_mode()
        self._clock = clock
        self._process_id = os.getpid() if process_id is None else int(process_id)
        self._hostname = socket.gethostname() if hostname is None else str(hostname)
        self._machine_identity = (
            _default_machine_identity() if machine_identity is None else str(machine_identity)
        )
        self._owner_instance_id = uuid.uuid4().hex
        self._process_is_alive = process_is_alive
        self._initialize_schema()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO hive_delegation_owners("
                "owner_instance_id, owner_machine_id, owner_host, owner_pid, registered_ts"
                ") VALUES(?,?,?,?,?)",
                (self._owner_instance_id, self._machine_identity, self._hostname,
                 self._process_id, self._clock()),
            )

    def _configure_journal_mode(self) -> None:
        """Enable WAL without turning a simultaneous cold start into a hard failure."""
        for attempt in range(3):
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as exc:
                transient = "locked" in str(exc).casefold() or "busy" in str(exc).casefold()
                if not transient or attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def _initialize_schema(self) -> None:
        """Run additive schema changes under one SQLite writer transaction."""
        for attempt in range(3):
            try:
                with self._lock:
                    self._db.execute("BEGIN IMMEDIATE")
                    self._db.execute(
                        "CREATE TABLE IF NOT EXISTS hive_delegations("
                        "id TEXT PRIMARY KEY, parent_run_id TEXT NOT NULL, child_run_id TEXT NOT NULL, "
                        "role TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
                        "max_attempts INTEGER NOT NULL, created_ts REAL NOT NULL, updated_ts REAL NOT NULL, "
                        "safe_summary TEXT NOT NULL DEFAULT '', owner_host TEXT NOT NULL DEFAULT '', "
                        "owner_pid INTEGER NOT NULL DEFAULT 0, owner_machine_id TEXT NOT NULL DEFAULT '', "
                        "owner_instance_id TEXT NOT NULL DEFAULT '', "
                        "parent_delegation_id TEXT NOT NULL DEFAULT '', "
                        "root_delegation_id TEXT NOT NULL DEFAULT '', depth INTEGER NOT NULL DEFAULT 0, "
                        "max_depth INTEGER NOT NULL DEFAULT 0, max_children INTEGER NOT NULL DEFAULT 0, "
                        "target_machine_id TEXT NOT NULL DEFAULT '', granted_tools TEXT NOT NULL DEFAULT '[]', "
                        "max_worker_turns INTEGER NOT NULL DEFAULT 0, max_worker_tool_calls INTEGER NOT NULL DEFAULT 0, "
                        "max_worker_seconds INTEGER NOT NULL DEFAULT 0, branch_max_turns INTEGER NOT NULL DEFAULT 0, "
                        "branch_reserved_turns INTEGER NOT NULL DEFAULT 0, branch_max_tool_calls INTEGER NOT NULL DEFAULT 0, "
                        "branch_reserved_tool_calls INTEGER NOT NULL DEFAULT 0, branch_max_seconds INTEGER NOT NULL DEFAULT 0, "
                        "branch_reserved_seconds INTEGER NOT NULL DEFAULT 0, max_active_children INTEGER NOT NULL DEFAULT 0)"
                    )
                    self._db.execute(
                        "CREATE INDEX IF NOT EXISTS hive_delegations_parent "
                        "ON hive_delegations(parent_run_id, created_ts)"
                    )
                    self._db.execute(
                        "CREATE TABLE IF NOT EXISTS hive_delegation_events("
                        "id INTEGER PRIMARY KEY AUTOINCREMENT, delegation_id TEXT NOT NULL, "
                        "ts REAL NOT NULL, event_type TEXT NOT NULL, data_json TEXT NOT NULL)"
                    )
                    self._db.execute(
                        "CREATE TABLE IF NOT EXISTS hive_delegation_owners("
                        "owner_instance_id TEXT PRIMARY KEY, owner_machine_id TEXT NOT NULL, "
                        "owner_host TEXT NOT NULL, owner_pid INTEGER NOT NULL, registered_ts REAL NOT NULL)"
                    )
                    columns = {str(row[1]) for row in self._db.execute("PRAGMA table_info(hive_delegations)")}
                    for name, definition in (
                        ("owner_host", "TEXT NOT NULL DEFAULT ''"),
                        ("owner_pid", "INTEGER NOT NULL DEFAULT 0"),
                        ("owner_machine_id", "TEXT NOT NULL DEFAULT ''"),
                        ("owner_instance_id", "TEXT NOT NULL DEFAULT ''"),
                        ("parent_delegation_id", "TEXT NOT NULL DEFAULT ''"),
                        ("root_delegation_id", "TEXT NOT NULL DEFAULT ''"),
                        ("depth", "INTEGER NOT NULL DEFAULT 0"),
                        ("max_depth", "INTEGER NOT NULL DEFAULT 0"),
                        ("max_children", "INTEGER NOT NULL DEFAULT 0"),
                        ("target_machine_id", "TEXT NOT NULL DEFAULT ''"),
                        ("granted_tools", "TEXT NOT NULL DEFAULT '[]'"),
                        ("max_worker_turns", "INTEGER NOT NULL DEFAULT 0"),
                        ("max_worker_tool_calls", "INTEGER NOT NULL DEFAULT 0"),
                        ("max_worker_seconds", "INTEGER NOT NULL DEFAULT 0"),
                        ("branch_max_turns", "INTEGER NOT NULL DEFAULT 0"),
                        ("branch_reserved_turns", "INTEGER NOT NULL DEFAULT 0"),
                        ("branch_max_tool_calls", "INTEGER NOT NULL DEFAULT 0"),
                        ("branch_reserved_tool_calls", "INTEGER NOT NULL DEFAULT 0"),
                        ("branch_max_seconds", "INTEGER NOT NULL DEFAULT 0"),
                        ("branch_reserved_seconds", "INTEGER NOT NULL DEFAULT 0"),
                        ("max_active_children", "INTEGER NOT NULL DEFAULT 0"),
                        ("capability_id", "TEXT NOT NULL DEFAULT ''"),
                        ("capability_state", "TEXT NOT NULL DEFAULT ''"),
                        ("capability_deadline_ts", "REAL NOT NULL DEFAULT 0"),
                    ):
                        if name not in columns:
                            self._db.execute(f"ALTER TABLE hive_delegations ADD COLUMN {name} {definition}")
                    self._db.execute(
                        "CREATE INDEX IF NOT EXISTS hive_delegations_parent_delegation "
                        "ON hive_delegations(parent_delegation_id, created_ts)"
                    )
                    self._db.commit()
                    return
            except sqlite3.OperationalError as exc:
                self._db.rollback()
                transient = "locked" in str(exc).casefold() or "busy" in str(exc).casefold()
                if not transient or attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def create(self, *, parent_run_id: str, child_run_id: str, role: str,
               parent_delegation_id: str = "") -> DelegationRecord:
        """Create one fenced delegation.

        Roots retain the M9-compatible behaviour.  A nested delegation is only
        admitted while its local parent holds a running claim and its closed
        profile explicitly grants the requested child role.  The graph itself,
        rather than a prompt, enforces depth and fan-out limits.
        """
        profile = specialist_profile(role)
        now = self._clock()
        delegation_id = str(uuid.uuid4())
        parent_id = str(parent_delegation_id or "")
        with self._lock:
            # SQLite's writer transaction makes validation, branch reservation,
            # and insertion one cross-process atomic operation.  A coordinator
            # cannot oversubscribe its child, execution, or elapsed-time budget.
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if parent_id:
                    if not self._machine_identity:
                        raise RuntimeError("nested delegation requires HIVE_STATE_HOST_ID")
                    parent = self._db.execute(
                        "SELECT * FROM hive_delegations WHERE id=?", (parent_id,)
                    ).fetchone()
                    if parent is None or str(parent["state"]) != RUNNING:
                        raise ValueError("parent delegation is not running")
                    if (str(parent["target_machine_id"]) != self._machine_identity
                            or str(parent["owner_machine_id"]) != self._machine_identity
                            or str(parent["owner_instance_id"]) != self._owner_instance_id):
                        raise ValueError("parent delegation is not locally owned")
                    parent_profile = specialist_profile(str(parent["role"]))
                    if profile.name not in parent_profile.allowed_child_roles:
                        raise ValueError("child role is not granted by parent profile")
                    depth = int(parent["depth"]) + 1
                    max_depth = int(parent["max_depth"])
                    if depth > max_depth:
                        raise ValueError("delegation depth exhausted")
                    children = self._db.execute(
                        "SELECT COUNT(*) FROM hive_delegations WHERE parent_delegation_id=?",
                        (parent_id,),
                    ).fetchone()[0]
                    if int(children) >= int(parent["max_children"]):
                        raise ValueError("delegation child budget exhausted")
                    root_id = str(parent["root_delegation_id"] or parent_id)
                    root = self._db.execute(
                        "SELECT * FROM hive_delegations WHERE id=?", (root_id,)
                    ).fetchone()
                    if root is None:
                        raise ValueError("delegation root is unavailable")
                    parent_tools = frozenset(_tools_from_row(parent))
                    granted_tools = tuple(sorted(parent_tools & profile.allowed_tools))
                    if not granted_tools:
                        raise ValueError("child has no inherited capabilities")
                    worker_turns = min(profile.max_worker_turns, int(root["branch_max_turns"])
                                       - int(root["branch_reserved_turns"]))
                    worker_tool_calls = min(profile.max_worker_tool_calls, int(root["branch_max_tool_calls"])
                                            - int(root["branch_reserved_tool_calls"]))
                    worker_seconds = min(profile.max_worker_seconds, int(root["branch_max_seconds"])
                                         - int(root["branch_reserved_seconds"]))
                    if min(worker_turns, worker_tool_calls, worker_seconds) < 1:
                        raise ValueError("delegation branch budget exhausted")
                    self._db.execute(
                        "UPDATE hive_delegations SET branch_reserved_turns=branch_reserved_turns+?, "
                        "branch_reserved_tool_calls=branch_reserved_tool_calls+?, "
                        "branch_reserved_seconds=branch_reserved_seconds+? WHERE id=?",
                        (worker_turns, worker_tool_calls, worker_seconds, root_id),
                    )
                    target_machine_id = self._machine_identity
                    child_max_depth = max_depth
                    child_max_children = 0
                    max_attempts = min(profile.max_attempts, int(parent["max_attempts"]))
                    branch_max_turns = branch_reserved_turns = 0
                    branch_max_tool_calls = branch_reserved_tool_calls = 0
                    branch_max_seconds = branch_reserved_seconds = 0
                    max_active_children = 0
                else:
                    depth = 0
                    root_id = delegation_id
                    target_machine_id = self._machine_identity if profile.max_delegation_depth > 0 else ""
                    if profile.max_delegation_depth > 0 and not target_machine_id:
                        raise RuntimeError("nested delegation requires HIVE_STATE_HOST_ID")
                    granted_tools = tuple(sorted(profile.allowed_tools))
                    worker_turns = profile.max_worker_turns
                    worker_tool_calls = profile.max_worker_tool_calls
                    worker_seconds = profile.max_worker_seconds
                    child_max_depth = profile.max_delegation_depth
                    child_max_children = profile.max_children
                    max_attempts = profile.max_attempts
                    branch_max_turns = max(profile.max_branch_turns, worker_turns)
                    branch_reserved_turns = worker_turns
                    branch_max_tool_calls = max(profile.max_branch_tool_calls, worker_tool_calls)
                    branch_reserved_tool_calls = worker_tool_calls
                    branch_max_seconds = max(profile.max_branch_seconds, worker_seconds)
                    branch_reserved_seconds = worker_seconds
                    max_active_children = profile.max_active_children
                self._db.execute(
                    "INSERT INTO hive_delegations("
                    "id, parent_run_id, child_run_id, role, state, attempts, max_attempts, created_ts, updated_ts, safe_summary, "
                    "parent_delegation_id, root_delegation_id, depth, max_depth, max_children, target_machine_id, granted_tools, "
                    "max_worker_turns, max_worker_tool_calls, max_worker_seconds, branch_max_turns, branch_reserved_turns, "
                    "branch_max_tool_calls, branch_reserved_tool_calls, branch_max_seconds, branch_reserved_seconds, max_active_children"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (delegation_id, str(parent_run_id), str(child_run_id), profile.name, QUEUED,
                     0, max_attempts, now, now, "", parent_id, root_id, depth, child_max_depth,
                     child_max_children, target_machine_id, json.dumps(granted_tools), worker_turns,
                     worker_tool_calls, worker_seconds, branch_max_turns, branch_reserved_turns,
                     branch_max_tool_calls, branch_reserved_tool_calls, branch_max_seconds,
                     branch_reserved_seconds, max_active_children),
                )
                # The capability snapshot is issued atomically with the
                # delegation.  It is never recomputed from a later profile.
                capability_id = uuid.uuid4().hex
                self._db.execute(
                    "UPDATE hive_delegations SET capability_id=?, capability_state='active', "
                    "capability_deadline_ts=? WHERE id=?",
                    (capability_id, now + float(worker_seconds), delegation_id),
                )
                self._event(delegation_id, "grant.issued", {"role": profile.name})
                self._event(delegation_id, "queued", {"role": profile.name, "depth": depth})
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return self.get(delegation_id)  # type: ignore[return-value]

    def claim(self, delegation_id: str) -> int | None:
        now = self._clock()
        with self._lock:
            # Re-check every mutable relationship while holding SQLite's writer
            # lock.  A shared host identity alone is not authority to claim a
            # child created by another live Hive runtime.
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT target_machine_id, parent_delegation_id, root_delegation_id "
                    "FROM hive_delegations WHERE id=?", (str(delegation_id),)
                ).fetchone()
                if row is None:
                    self._db.rollback()
                    return None
                target_machine_id = str(row["target_machine_id"])
                if target_machine_id and (not self._machine_identity or target_machine_id != self._machine_identity):
                    self._db.rollback()
                    return None
                parent_id = str(row["parent_delegation_id"])
                if parent_id:
                    parent = self._db.execute(
                        "SELECT state, target_machine_id, owner_machine_id, owner_instance_id "
                        "FROM hive_delegations WHERE id=?", (parent_id,)
                    ).fetchone()
                    if (parent is None or str(parent["state"]) != RUNNING
                            or str(parent["target_machine_id"]) != self._machine_identity
                            or str(parent["owner_machine_id"]) != self._machine_identity
                            or str(parent["owner_instance_id"]) != self._owner_instance_id):
                        self._db.rollback()
                        return None
                if not self._ancestors_running(str(delegation_id)):
                    self._db.rollback()
                    return None
                if parent_id:
                    # A branch has a durable concurrency budget as well as a
                    # creation budget.  Claiming is serialized so sibling workers
                    # cannot start simultaneously after a process restart.
                    root_id = str(row["root_delegation_id"] or parent_id)
                    root = self._db.execute(
                        "SELECT max_active_children FROM hive_delegations WHERE id=?", (root_id,)
                    ).fetchone()
                    if root is None or int(root["max_active_children"]) < 1:
                        self._db.rollback()
                        return None
                    active = self._db.execute(
                        "SELECT COUNT(*) FROM hive_delegations WHERE root_delegation_id=? "
                        "AND parent_delegation_id<>'' AND state=?",
                        (root_id, RUNNING),
                    ).fetchone()[0]
                    if int(active) >= int(root["max_active_children"]):
                        self._db.rollback()
                        return None
                cur = self._db.execute(
                    "UPDATE hive_delegations SET state=?, attempts=attempts+1, updated_ts=?, owner_host=?, owner_pid=?, "
                    "owner_machine_id=?, owner_instance_id=? "
                    "WHERE id=? AND state=? AND attempts < max_attempts",
                    (RUNNING, now, self._hostname, self._process_id, self._machine_identity,
                     self._owner_instance_id, str(delegation_id), QUEUED),
                )
                if cur.rowcount != 1:
                    self._db.rollback()
                    return None
                row = self._db.execute(
                    "SELECT attempts FROM hive_delegations WHERE id=?", (str(delegation_id),)
                ).fetchone()
                attempt = int(row["attempts"])
                self._event(str(delegation_id), "started", {"attempt": attempt})
                self._db.commit()
                return attempt
            except Exception:
                self._db.rollback()
                raise

    def finish(self, delegation_id: str, *, attempt: int, success: bool, summary: str = "",
               require_review: bool = False) -> bool:
        state = REVIEW_REQUIRED if success and require_review else COMPLETED if success else FAILED
        safe = str(redact_value(summary))[:500]
        with self._lock:
            cur = self._db.execute(
                "UPDATE hive_delegations SET state=?, updated_ts=?, safe_summary=?, owner_host='', owner_pid=0, "
                "owner_machine_id='', owner_instance_id='' "
                "WHERE id=? AND state=? AND attempts=?",
                (state, self._clock(), safe, str(delegation_id), RUNNING, int(attempt)),
            )
            if cur.rowcount:
                self._revoke_descendant_grants(str(delegation_id), reason="parent_terminal")
                self._event(str(delegation_id), state, {"summary": safe})
            self._db.commit()
            return cur.rowcount == 1

    def cancel(self, delegation_id: str, *, attempt: int, summary: str = "cancelled") -> bool:
        """Record a cancellation only for the attempt currently holding the claim."""
        safe = str(redact_value(summary))[:500]
        with self._lock:
            cur = self._db.execute(
                "UPDATE hive_delegations SET state=?, updated_ts=?, safe_summary=?, owner_host='', owner_pid=0, "
                "owner_machine_id='', owner_instance_id='' "
                "WHERE id=? AND state=? AND attempts=?",
                (CANCELLED, self._clock(), safe, str(delegation_id), RUNNING, int(attempt)),
            )
            if cur.rowcount:
                self._revoke_descendant_grants(str(delegation_id), reason="parent_cancelled")
                self._event(str(delegation_id), CANCELLED, {"summary": safe})
            self._db.commit()
            return cur.rowcount == 1

    def revoke(self, delegation_id: str, *, attempt: int, reason: str = "parent_revoked") -> bool:
        """Locally revoke one currently-owned grant and its descendants."""
        with self._lock:
            row = self._db.execute(
                "SELECT owner_machine_id, owner_instance_id FROM hive_delegations "
                "WHERE id=? AND state=? AND attempts=?",
                (str(delegation_id), RUNNING, int(attempt)),
            ).fetchone()
            if (row is None or str(row["owner_machine_id"]) != self._machine_identity
                    or str(row["owner_instance_id"]) != self._owner_instance_id):
                return False
            self._revoke_descendant_grants(str(delegation_id), reason=reason, include_root=True)
            self._db.commit()
            return True

    def authorize_attempt(self, delegation_id: str, *, capability_id: str, role: str,
                          attempt: int) -> bool:
        """Fail closed before a parent brokers a worker operation."""
        with self._lock:
            row = self._db.execute(
                "SELECT role, state, attempts, capability_id, capability_state, capability_deadline_ts "
                "FROM hive_delegations WHERE id=?", (str(delegation_id),),
            ).fetchone()
            now = self._clock()
            expired = bool(row is not None and str(row["capability_state"]) == "active"
                           and float(row["capability_deadline_ts"]) < now)
            if expired:
                self._db.execute("UPDATE hive_delegations SET capability_state='expired' "
                                 "WHERE id=? AND capability_state='active'", (str(delegation_id),))
                self._event(str(delegation_id), "grant.expired", {"reason": "deadline"})
            allowed = bool(row is not None and str(row["role"]) == str(role)
                           and str(row["state"]) == RUNNING and int(row["attempts"]) == int(attempt)
                           and str(row["capability_id"]) == str(capability_id)
                           and str(row["capability_state"]) == "active"
                           and not expired
                           and float(row["capability_deadline_ts"]) >= now
                           and self._ancestors_running(str(delegation_id)))
            if expired or not allowed:
                self._event(str(delegation_id), "grant.denied", {"reason": "unauthorized"})
                self._db.commit()
            return allowed

    def get(self, delegation_id: str) -> DelegationRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM hive_delegations WHERE id=?", (str(delegation_id),)).fetchone()
            return _record(row) if row is not None else None

    def for_parent(self, parent_run_id: str, *, limit: int = 100) -> list[DelegationRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM hive_delegations WHERE parent_run_id=? ORDER BY created_ts LIMIT ?",
                (str(parent_run_id), max(1, min(int(limit), 500))),
            ).fetchall()
            return [_record(row) for row in rows]

    def recent(self, *, limit: int = 100) -> list[DelegationRecord]:
        """Return a bounded, durable page of redacted delegation metadata."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM hive_delegations ORDER BY created_ts DESC, id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [_record(row) for row in rows]

    def children(self, delegation_id: str, *, limit: int = 100) -> list[DelegationRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM hive_delegations WHERE parent_delegation_id=? ORDER BY created_ts, id LIMIT ?",
                (str(delegation_id), max(1, min(int(limit), 500))),
            ).fetchall()
        return [_record(row) for row in rows]

    def tree(self, delegation_id: str, *, max_depth: int = 8, max_nodes: int = 200) -> dict | None:
        """Return a bounded, cycle-safe metadata tree without worker payloads."""
        root = self.get(delegation_id)
        if root is None:
            return None
        depth_limit = max(0, min(int(max_depth), 16))
        remaining = max(1, min(int(max_nodes), 500)) - 1
        seen = {root.id}
        truncated = False

        def build(item: DelegationRecord, depth: int) -> dict:
            nonlocal remaining, truncated
            node = _public_record(item)
            node["children"] = []
            if depth >= depth_limit:
                if self.children(item.id, limit=1):
                    truncated = True
                return node
            for child in self.children(item.id, limit=500):
                if remaining <= 0:
                    truncated = True
                    break
                if child.id in seen:
                    truncated = True
                    continue
                seen.add(child.id)
                remaining -= 1
                node["children"].append(build(child, depth + 1))
            return node

        return {"root": build(root, 0), "node_count": len(seen), "truncated": truncated}

    def failed_page(self, *, limit: int = 100,
                    before_updated_ts: float | None = None,
                    before_id: str = "") -> list[DelegationRecord]:
        """Return one ordered page of failed metadata, never worker inputs/output."""
        safe_limit = max(1, min(int(limit), 500))
        with self._lock:
            if before_updated_ts is None:
                rows = self._db.execute(
                    "SELECT * FROM hive_delegations WHERE state=? "
                    "ORDER BY updated_ts DESC, id DESC LIMIT ?",
                    (FAILED, safe_limit),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM hive_delegations WHERE state=? AND "
                    "(updated_ts < ? OR (updated_ts = ? AND id < ?)) "
                    "ORDER BY updated_ts DESC, id DESC LIMIT ?",
                    (FAILED, float(before_updated_ts), float(before_updated_ts), str(before_id), safe_limit),
                ).fetchall()
            return [_record(row) for row in rows]

    def recover_interrupted(self) -> int:
        """Mark only proven interrupted, locally owned worker attempts as failed.

        Delegation inputs and worker output are intentionally never persisted, so
        this method cannot safely replay a worker.  It only converts a proven
        local interruption into durable, redacted failure evidence.  Remote,
        live, and legacy unowned records are left untouched.
        """
        # A hostname or MAC address is not a trustworthy ownership boundary:
        # cloned hosts can share both.  Automatic recovery therefore requires a
        # deployment-provided, durable local identity.  Recording still works
        # without one, but restart reconciliation fails closed.
        if not self._machine_identity:
            return 0
        with self._lock:
            rows = self._db.execute(
                "SELECT id, attempts, owner_pid, owner_instance_id FROM hive_delegations "
                "WHERE state=? AND owner_machine_id=? AND owner_instance_id<>''",
                (RUNNING, self._machine_identity),
            ).fetchall()
            recovered = 0
            summary = "worker interrupted; recovery requires replanning"
            for row in rows:
                owner_pid = int(row["owner_pid"])
                owner_instance_id = str(row["owner_instance_id"])
                if owner_instance_id == self._owner_instance_id:
                    continue
                if self._process_is_alive(owner_pid):
                    active_owner = self._active_owner_instance(owner_pid)
                    if active_owner in (None, owner_instance_id):
                        continue
                cur = self._db.execute(
                    "UPDATE hive_delegations SET state=?, updated_ts=?, safe_summary=?, owner_host='', owner_pid=0, "
                    "owner_machine_id='', owner_instance_id='' WHERE id=? AND state=? AND attempts=? "
                    "AND owner_machine_id=? AND owner_pid=? AND owner_instance_id=?",
                    (FAILED, self._clock(), summary, str(row["id"]), RUNNING, int(row["attempts"]),
                     self._machine_identity, owner_pid, owner_instance_id),
                )
                if cur.rowcount:
                    self._event(str(row["id"]), "interrupted", {"summary": summary})
                    recovered += 1
            self._db.commit()
            return recovered

    def close(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM hive_delegation_owners WHERE owner_instance_id=?",
                             (self._owner_instance_id,))
            self._db.commit()
            self._db.close()

    def _active_owner_instance(self, owner_pid: int) -> str | None:
        row = self._db.execute(
            "SELECT owner_instance_id FROM hive_delegation_owners "
            "WHERE owner_machine_id=? AND owner_pid=? ORDER BY rowid DESC LIMIT 1",
            (self._machine_identity, owner_pid),
        ).fetchone()
        return str(row["owner_instance_id"]) if row is not None else None

    def _ancestors_running(self, delegation_id: str) -> bool:
        """Fail closed if any known ancestor is terminal or the lineage cycles."""
        current = self._db.execute(
            "SELECT parent_delegation_id FROM hive_delegations WHERE id=?", (delegation_id,)
        ).fetchone()
        parent_id = str(current["parent_delegation_id"]) if current is not None else ""
        seen = {delegation_id}
        while parent_id:
            if parent_id in seen:
                return False
            seen.add(parent_id)
            row = self._db.execute(
                "SELECT parent_delegation_id, state FROM hive_delegations WHERE id=?", (parent_id,)
            ).fetchone()
            if row is None or str(row["state"]) != RUNNING:
                return False
            parent_id = str(row["parent_delegation_id"])
        return True

    def _revoke_descendant_grants(self, delegation_id: str, *, reason: str,
                                  include_root: bool = False) -> None:
        """Invalidate an active local subtree without recording payload data."""
        query = (
            "WITH RECURSIVE descendants(id) AS ("
            "SELECT id FROM hive_delegations WHERE parent_delegation_id=? "
            "UNION ALL SELECT d.id FROM hive_delegations d JOIN descendants p "
            "ON d.parent_delegation_id=p.id) "
            "UPDATE hive_delegations SET capability_state='revoked' "
            "WHERE capability_state='active' AND id IN (SELECT id FROM descendants)"
        )
        self._db.execute(query, (str(delegation_id),))
        if include_root:
            self._db.execute("UPDATE hive_delegations SET capability_state='revoked' "
                             "WHERE id=? AND capability_state='active'", (str(delegation_id),))
        self._event(str(delegation_id), "grant.revoked", {"reason": str(reason)[:64]})

    def _event(self, delegation_id: str, event_type: str, data: dict) -> None:
        self._db.execute(
            "INSERT INTO hive_delegation_events(delegation_id, ts, event_type, data_json) VALUES(?,?,?,?)",
            (delegation_id, self._clock(), event_type, json.dumps(redact_value(data), sort_keys=True)),
        )


def _record(row: sqlite3.Row) -> DelegationRecord:
    return DelegationRecord(
        id=str(row["id"]), parent_run_id=str(row["parent_run_id"]), child_run_id=str(row["child_run_id"]),
        role=str(row["role"]), state=str(row["state"]), attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]), created_ts=float(row["created_ts"]),
        updated_ts=float(row["updated_ts"]), safe_summary=str(row["safe_summary"]),
        owner_host=str(row["owner_host"]), owner_pid=int(row["owner_pid"]),
        parent_delegation_id=str(row["parent_delegation_id"]),
        root_delegation_id=str(row["root_delegation_id"]), depth=int(row["depth"]),
        max_depth=int(row["max_depth"]), max_children=int(row["max_children"]),
        target_machine_id=str(row["target_machine_id"]),
        granted_tools=_tools_from_row(row), max_worker_turns=int(row["max_worker_turns"]),
        max_worker_tool_calls=int(row["max_worker_tool_calls"]),
        max_worker_seconds=int(row["max_worker_seconds"]),
        branch_max_turns=int(row["branch_max_turns"]),
        branch_reserved_turns=int(row["branch_reserved_turns"]),
        branch_max_tool_calls=int(row["branch_max_tool_calls"]),
        branch_reserved_tool_calls=int(row["branch_reserved_tool_calls"]),
        branch_max_seconds=int(row["branch_max_seconds"]),
        branch_reserved_seconds=int(row["branch_reserved_seconds"]),
        max_active_children=int(row["max_active_children"]),
        capability_id=str(row["capability_id"]), capability_state=str(row["capability_state"]),
        capability_deadline_ts=float(row["capability_deadline_ts"]),
    )


def _tools_from_row(row: sqlite3.Row) -> tuple[str, ...]:
    """Decode persisted capability grants defensively and fail closed."""
    try:
        raw = json.loads(str(row["granted_tools"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        return ()
    return tuple(sorted(set(raw)))


def _public_record(item: DelegationRecord) -> dict:
    """Operator-safe projection: no run/session/task/output/error/owner fields."""
    return {
        "id": item.id,
        "role": item.role,
        "state": item.state,
        "attempts": item.attempts,
        "max_attempts": item.max_attempts,
        "created_ts": item.created_ts,
        "updated_ts": item.updated_ts,
        "depth": item.depth,
        "capability_state": item.capability_state,
    }


def _default_machine_identity() -> str:
    """Return the deployment-provided identity used for local recovery fencing.

    Missing configuration is intentional: recovery must not infer ownership from
    cloneable host attributes such as hostname or MAC address.
    """
    return os.getenv("HIVE_STATE_HOST_ID", "").strip()
