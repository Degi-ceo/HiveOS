"""Durable observability ledger shared by telemetry and self-mod history.

The ledger is append-only for inference and proposal outcomes. It keeps the
accounting source of truth in the existing state database without making core
depend on observability: callers receive plain aggregate mappings.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from hive.core.redact import redact_value
from hive.core.run_context import current_run_id

_PR_STANDDOWN_REASONS = frozenset({
    "round_cap", "repair_failed", "ci_unreproducible", "feedback_ambiguous",
    "approval_required", "policy_rejected", "infra_failure",
    "stale_head", "secret_detected", "evaluation_failed",
    "review_ambiguous", "review_round_cap", "review_failed", "review_uncertain",
})


class ObservabilityLedger:
    """Persist inference telemetry and self-mod proposal outcomes in SQLite."""

    def __init__(self, db_path: str | Path, *, run_id: str | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._path = str(db_path)
        self._clock = clock
        self.run_id = run_id or str(uuid.uuid4())
        self._lock = threading.RLock()
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        # Cold-start schema changes can overlap across Hive processes. Runtime
        # writes switch back to the short retry window after migration.
        self._db.execute("PRAGMA busy_timeout=5000")
        for attempt in range(6):
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                transient = "locked" in str(exc).casefold() or "busy" in str(exc).casefold()
                if not transient or attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
        self._init_schema()
        self._db.execute("PRAGMA busy_timeout=250")

    def _init_schema(self) -> None:
        with self._lock, self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS telemetry(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT NOT NULL,
                  ts REAL NOT NULL,
                  day TEXT NOT NULL,
                  model TEXT NOT NULL,
                  input_tokens INTEGER NOT NULL,
                  output_tokens INTEGER NOT NULL,
                  cost_usd REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_telemetry_day ON telemetry(day);
                CREATE TABLE IF NOT EXISTS spend_reservations(
                  id TEXT PRIMARY KEY,
                  ts REAL NOT NULL,
                  day TEXT NOT NULL,
                  reserved_usd REAL NOT NULL,
                  state TEXT NOT NULL CHECK(state IN ('pending', 'settled', 'released'))
                );
                CREATE INDEX IF NOT EXISTS idx_spend_reservations_day_state
                  ON spend_reservations(day, state);
                CREATE TABLE IF NOT EXISTS selfmod_history(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT NOT NULL,
                  ts REAL NOT NULL,
                  title TEXT NOT NULL,
                  dry_run INTEGER NOT NULL,
                  tier TEXT NOT NULL,
                  branch TEXT,
                  pr_url TEXT,
                  outcome TEXT NOT NULL,
                  ok INTEGER NOT NULL,
                  repair_attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_selfmod_history_ts ON selfmod_history(ts DESC);
                CREATE INDEX IF NOT EXISTS idx_selfmod_history_run_id
                  ON selfmod_history(run_id);
                CREATE INDEX IF NOT EXISTS idx_selfmod_history_branch
                  ON selfmod_history(branch);
                CREATE INDEX IF NOT EXISTS idx_selfmod_history_pr_url
                  ON selfmod_history(pr_url);
                CREATE TABLE IF NOT EXISTS selfmod_pr_identity(
                  pr_url TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL,
                  pr_number INTEGER NOT NULL,
                  branch TEXT NOT NULL,
                  pushed_sha TEXT NOT NULL,
                  pr_id INTEGER NOT NULL DEFAULT 0,
                  author_id INTEGER NOT NULL DEFAULT 0,
                  head_repo_id INTEGER NOT NULL DEFAULT 0,
                  base_repo_id INTEGER NOT NULL DEFAULT 0,
                  head_ref TEXT NOT NULL DEFAULT '',
                  base_ref TEXT NOT NULL DEFAULT '',
                  bound_ts REAL,
                  created_pr_id INTEGER NOT NULL DEFAULT 0,
                  created_author_id INTEGER NOT NULL DEFAULT 0,
                  created_head_repo_id INTEGER NOT NULL DEFAULT 0,
                  created_base_repo_id INTEGER NOT NULL DEFAULT 0,
                  created_head_ref TEXT NOT NULL DEFAULT '',
                  created_head_sha TEXT NOT NULL DEFAULT '',
                  created_base_ref TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_selfmod_pr_identity_run
                  ON selfmod_pr_identity(run_id, pr_number);
                CREATE TABLE IF NOT EXISTS selfmod_pr_feedback_rounds(
                  pr_url TEXT NOT NULL,
                  round INTEGER NOT NULL CHECK(round BETWEEN 1 AND 2),
                  run_id TEXT NOT NULL,
                  expected_sha TEXT NOT NULL,
                  feedback_key TEXT NOT NULL,
                  state TEXT NOT NULL CHECK(state IN ('reserved', 'pushed', 'failed', 'uncertain')),
                  new_sha TEXT NOT NULL DEFAULT '',
                  reserved_ts REAL NOT NULL,
                  updated_ts REAL NOT NULL,
                  PRIMARY KEY(pr_url, round),
                  UNIQUE(pr_url, feedback_key)
                );
                CREATE TABLE IF NOT EXISTS selfmod_pr_standdown(
                  pr_url TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL,
                  marker TEXT NOT NULL,
                  reason_code TEXT NOT NULL,
                  state TEXT NOT NULL CHECK(state IN ('reserved', 'posted', 'uncertain')),
                  reserved_ts REAL NOT NULL,
                  updated_ts REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS selfmod_pr_observations(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT NOT NULL,
                  ts REAL NOT NULL,
                  pr_number INTEGER NOT NULL,
                  pr_url TEXT NOT NULL,
                  status TEXT NOT NULL,
                  checks_total INTEGER NOT NULL,
                  checks_failed INTEGER NOT NULL,
                  checks_pending INTEGER NOT NULL,
                  review_state TEXT NOT NULL,
                  changes_requested INTEGER NOT NULL,
                  evidence_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_selfmod_pr_observations_run
                  ON selfmod_pr_observations(run_id, id DESC);
                CREATE TABLE IF NOT EXISTS selfmod_pr_poll_state(
                  run_id TEXT NOT NULL,
                  pr_number INTEGER NOT NULL,
                  next_poll_ts REAL NOT NULL,
                  PRIMARY KEY(run_id, pr_number)
                );
                CREATE TABLE IF NOT EXISTS selfmod_pr_rate_limit(
                  id INTEGER PRIMARY KEY CHECK(id=1),
                  until_ts REAL NOT NULL
                );
                """
            )
            # executescript commits before running its idempotent DDL. Take a
            # writer lock around introspection and ALTER so a second process
            # cannot make the same migration decision from stale schema.
            self._db.execute("BEGIN IMMEDIATE")
            columns = {str(row[1]) for row in self._db.execute("PRAGMA table_info(selfmod_history)")}
            if "repair_attempts" not in columns:
                self._db.execute(
                    "ALTER TABLE selfmod_history ADD COLUMN repair_attempts INTEGER NOT NULL DEFAULT 0"
                )
            pr_columns = {str(row[1]) for row in self._db.execute(
                "PRAGMA table_info(selfmod_pr_observations)"
            )}
            if "evidence_json" not in pr_columns:
                self._db.execute(
                    "ALTER TABLE selfmod_pr_observations ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}'"
                )
            identity_columns = {str(row[1]) for row in self._db.execute(
                "PRAGMA table_info(selfmod_pr_identity)"
            )}
            for name, column_type, default in (
                ("created_pr_id", "INTEGER", "0"),
                ("created_author_id", "INTEGER", "0"),
                ("created_head_repo_id", "INTEGER", "0"),
                ("created_base_repo_id", "INTEGER", "0"),
                ("created_head_ref", "TEXT", "''"),
                ("created_head_sha", "TEXT", "''"),
                ("created_base_ref", "TEXT", "''"),
            ):
                if name not in identity_columns:
                    self._db.execute(
                        f"ALTER TABLE selfmod_pr_identity ADD COLUMN {name} "
                        f"{column_type} NOT NULL DEFAULT {default}"
                    )

    @staticmethod
    def _day(ts: float) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(ts))

    @staticmethod
    def _nonnegative_finite(value: object, *, default: float = 0.0) -> float:
        try:
            parsed = float(value or 0.0)
        except (TypeError, ValueError):
            return default
        return parsed if math.isfinite(parsed) and parsed >= 0.0 else default

    @staticmethod
    def _bounded_int(value: object) -> int:
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError, OverflowError):
            return 0
        return max(0, min(parsed, 2**63 - 1))

    def _write(self, operation: Callable[[], int | None]) -> int | None:
        """Retry a transient SQLite lock; never silently drop a ledger record."""
        for attempt in range(6):
            try:
                with self._lock, self._db:
                    return operation()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
        raise AssertionError("unreachable")  # pragma: no cover

    def record_inference(self, data: dict[str, Any]) -> None:
        """Append one completed inference using the already-calculated cost."""
        ts = self._nonnegative_finite(data.get("ts", self._clock()), default=self._clock())

        def insert() -> int:
            self._db.execute(
                """
                INSERT INTO telemetry
                  (run_id, ts, day, model, input_tokens, output_tokens, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(data.get("run_id") or current_run_id() or self.run_id),
                    ts,
                    self._day(ts),
                    str(data.get("model", "?") or "?"),
                    self._bounded_int(data.get("input_tokens", 0)),
                    self._bounded_int(data.get("output_tokens", 0)),
                    self._nonnegative_finite(data.get("cost_usd", 0.0)),
                ),
            )
            reservation_id = str(data.get("spend_reservation_id", "") or "")
            if reservation_id:
                self._db.execute(
                    "UPDATE spend_reservations SET state='settled' WHERE id=? AND state='pending'",
                    (reservation_id,),
                )
            return 0
        self._write(insert)

    def reserve_spend(self, *, amount_usd: object, cap_usd: object,
                      ts: float | None = None) -> str | None:
        """Atomically reserve a conservative request ceiling within a daily cap.

        Pending reservations intentionally survive a crash. If the provider may have
        charged for an interrupted request but its finalized telemetry is unavailable,
        retaining the reservation is conservative and therefore fails closed.
        """
        amount = self._nonnegative_finite(amount_usd)
        cap = self._nonnegative_finite(cap_usd)
        if cap <= 0.0 or amount <= 0.0:
            return ""
        now = self._nonnegative_finite(ts if ts is not None else self._clock(),
                                       default=self._clock())
        day = self._day(now)
        reservation_id = str(uuid.uuid4())

        def reserve() -> str | None:
            actual = float(self._db.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM telemetry WHERE day=?", (day,)
            ).fetchone()[0])
            pending = float(self._db.execute(
                "SELECT COALESCE(SUM(reserved_usd), 0.0) FROM spend_reservations "
                "WHERE day=? AND state='pending'", (day,)
            ).fetchone()[0])
            if actual + pending + amount > cap:
                return None
            self._db.execute(
                "INSERT INTO spend_reservations(id, ts, day, reserved_usd, state) "
                "VALUES (?, ?, ?, ?, 'pending')",
                (reservation_id, now, day, amount),
            )
            return reservation_id

        return self._write(reserve)  # type: ignore[return-value]

    def release_spend_reservation(self, reservation_id: str) -> None:
        """Release a request reservation when no provider call completed."""
        if not reservation_id:
            return

        def release() -> int:
            self._db.execute(
                "UPDATE spend_reservations SET state='released' WHERE id=? AND state='pending'",
                (reservation_id,),
            )
            return 0

        self._write(release)

    def telemetry_totals(self, *, day: str | None = None) -> dict[str, Any]:
        """Return a JSON-safe aggregate, optionally restricted to one local day."""
        where = "WHERE day=?" if day else ""
        params: tuple[str, ...] = (day,) if day else ()
        with self._lock:
            row = self._db.execute(
                f"""
                SELECT COUNT(*) AS inference_calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cost_usd), 0.0) AS cost_usd
                FROM telemetry {where}
                """,
                params,
            ).fetchone()
            model_rows = self._db.execute(
                f"""
                SELECT model, COUNT(*) AS calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cost_usd), 0.0) AS cost_usd
                FROM telemetry {where} GROUP BY model
                """,
                params,
            ).fetchall()
        by_model = {str(item["model"]): int(item["calls"]) for item in model_rows}
        cost_by_model = {str(item["model"]): float(item["cost_usd"]) for item in model_rows}
        tokens_by_model = {
            str(item["model"]): {
                "input": int(item["input_tokens"]),
                "output": int(item["output_tokens"]),
            }
            for item in model_rows
        }
        return {
            "inference_calls": int(row["inference_calls"]),
            "input_tokens": int(row["input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
            "cost_usd": float(row["cost_usd"]),
            "by_model": by_model,
            "cost_by_model": cost_by_model,
            "tokens_by_model": tokens_by_model,
        }

    def record_selfmod(self, record: dict[str, Any]) -> int:
        """Append a terminal self-mod proposal record."""
        safe_record = redact_value(record)
        pr_url = str(safe_record.get("pr_url") or "")
        branch = str(safe_record.get("branch") or "")
        head_sha = str(safe_record.get("head_sha") or "")
        parsed = urlparse(pr_url)
        pr_match = re.fullmatch(r"/[^/]+/[^/]+/pull/([1-9][0-9]*)", parsed.path)
        local_pr_number = (
            int(pr_match.group(1)) if parsed.scheme == "https"
            and parsed.netloc.casefold() == "github.com" and not parsed.query
            and not parsed.fragment and pr_match is not None
            and branch.startswith("hive/auto-")
            and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", head_sha)
            and str(safe_record.get("outcome", safe_record.get("stage"))) == "pushed"
            and bool(safe_record.get("ok")) else 0
        )
        creation = safe_record.get("pr_creation")
        created: tuple[int, int, int, int, str, str, str] | None = None
        if local_pr_number and isinstance(creation, dict):
            id_names = ("pr_id", "author_id", "head_repo_id", "base_repo_id")
            ids = tuple(creation.get(name) for name in id_names)
            if (
                creation.get("url") == pr_url
                and type(creation.get("number")) is int
                and creation["number"] == local_pr_number
                and all(type(value) is int and value > 0 for value in ids)
                and ids[2] == ids[3]
                and creation.get("head_ref") == branch
                and creation.get("head_sha") == head_sha.lower()
                and creation.get("base_ref") == "main"
            ):
                created = (*ids, branch, head_sha.lower(), "main")

        def insert() -> int:
            cursor = self._db.execute(
                """
                INSERT INTO selfmod_history
                  (run_id, ts, title, dry_run, tier, branch, pr_url, outcome, ok, repair_attempts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(safe_record.get("run_id") or self.run_id),
                    self._nonnegative_finite(safe_record.get("ts", self._clock()), default=self._clock()),
                    str(safe_record.get("title", "")),
                    int(bool(safe_record.get("dry_run"))),
                    str(safe_record.get("tier", "auto")),
                    safe_record.get("branch"),
                    safe_record.get("pr_url"),
                    str(safe_record.get("outcome", safe_record.get("stage", "unknown"))),
                    int(bool(safe_record.get("ok"))),
                    self._bounded_int(safe_record.get("repair_attempts", 0)),
                ),
            )
            if local_pr_number:
                self._db.execute(
                    """
                    INSERT OR IGNORE INTO selfmod_pr_identity
                      (pr_url, run_id, pr_number, branch, pushed_sha,
                       created_pr_id, created_author_id, created_head_repo_id,
                       created_base_repo_id, created_head_ref, created_head_sha,
                       created_base_ref)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (pr_url, str(safe_record.get("run_id") or self.run_id),
                     local_pr_number, branch, head_sha.lower(),
                     *(created or (0, 0, 0, 0, "", "", ""))),
                )
            return int(cursor.lastrowid)
        row_id = self._write(insert)
        assert isinstance(row_id, int)  # narrow _write's generic return for type checkers
        return row_id

    def get_pr_identity(self, pr_url: str) -> dict[str, Any] | None:
        """Return local PR provenance; legacy URL-only history never qualifies."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM selfmod_pr_identity WHERE pr_url=?", (pr_url,),
            ).fetchone()
        if row is None:
            return None
        return {
            "pr_url": str(row["pr_url"]), "run_id": str(row["run_id"]),
            "pr_number": int(row["pr_number"]), "branch": str(row["branch"]),
            "pushed_sha": str(row["pushed_sha"]), "pr_id": int(row["pr_id"]),
            "author_id": int(row["author_id"]),
            "head_repo_id": int(row["head_repo_id"]),
            "base_repo_id": int(row["base_repo_id"]),
            "head_ref": str(row["head_ref"]), "base_ref": str(row["base_ref"]),
            "created_pr_id": int(row["created_pr_id"]),
            "created_author_id": int(row["created_author_id"]),
            "created_head_repo_id": int(row["created_head_repo_id"]),
            "created_base_repo_id": int(row["created_base_repo_id"]),
            "created_head_ref": str(row["created_head_ref"]),
            "created_head_sha": str(row["created_head_sha"]),
            "created_base_ref": str(row["created_base_ref"]),
            "bound": row["bound_ts"] is not None,
        }

    @staticmethod
    def _pr_identity_matches(
        row: sqlite3.Row | None, run_id: str, pr_url: str,
        observation: dict[str, Any], *, expected_base: str,
        require_bound: bool,
    ) -> bool:
        """Compare a live GET with immutable create evidence and current local head."""
        if row is None or not isinstance(observation, dict):
            return False
        if observation.get("state") != "open" or observation.get("url") != pr_url:
            return False
        if require_bound and row["bound_ts"] is None:
            return False
        def positive(name: str) -> int:
            value = observation.get(name)
            return value if type(value) is int and value > 0 else 0
        number = positive("number")
        pr_id, author_id = positive("pr_id"), positive("author_id")
        head_repo_id = positive("head_repo_id")
        base_repo_id = positive("base_repo_id")
        head_sha = observation.get("head_sha")
        if not isinstance(head_sha, str) or re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}", head_sha,
        ) is None:
            return False
        if not all((number, pr_id, author_id, head_repo_id, base_repo_id)):
            return False
        if head_repo_id != base_repo_id or observation.get("base_ref") != expected_base:
            return False
        if (
            str(row["run_id"]) != run_id or str(row["pr_url"]) != pr_url
            or int(row["pr_number"]) != number
            or str(row["branch"]) != observation.get("head_ref")
            or str(row["pushed_sha"]) != head_sha
            or int(row["created_pr_id"]) != pr_id
            or int(row["created_author_id"]) != author_id
            or int(row["created_head_repo_id"]) != head_repo_id
            or int(row["created_base_repo_id"]) != base_repo_id
            or str(row["created_head_ref"]) != observation.get("head_ref")
            or str(row["created_base_ref"]) != expected_base
        ):
            return False
        initial_sha = str(row["created_head_sha"])
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", initial_sha) is None:
            return False
        if row["bound_ts"] is None and initial_sha != head_sha:
            return False
        if row["bound_ts"] is not None and (
            int(row["pr_id"]) != pr_id or int(row["author_id"]) != author_id
            or int(row["head_repo_id"]) != head_repo_id
            or int(row["base_repo_id"]) != base_repo_id
            or str(row["head_ref"]) != observation.get("head_ref")
            or str(row["base_ref"]) != expected_base
        ):
            return False
        return True

    def bind_pr_identity(
        self, run_id: str, pr_url: str, observation: dict[str, Any],
        *, expected_base: str = "main",
    ) -> bool:
        """Bind a current GitHub GET to the locally pushed commit exactly once.

        A later changed branch/SHA/owner cannot replace this evidence. External
        writes must use this separately verified identity, never URL history.
        """
        def bind() -> int:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT * FROM selfmod_pr_identity WHERE pr_url=?", (pr_url,),
            ).fetchone()
            if not self._pr_identity_matches(
                row, run_id, pr_url, observation,
                expected_base=expected_base, require_bound=False,
            ):
                return 0
            if row["bound_ts"] is not None:
                return 1
            self._db.execute(
                """
                UPDATE selfmod_pr_identity SET
                  pr_id=?, author_id=?, head_repo_id=?, base_repo_id=?,
                  head_ref=?, base_ref=?, bound_ts=? WHERE pr_url=? AND bound_ts IS NULL
                """,
                (observation["pr_id"], observation["author_id"],
                 observation["head_repo_id"], observation["base_repo_id"],
                 observation["head_ref"], expected_base, self._clock(), pr_url),
            )
            return 1

        return bool(self._write(bind))

    def validate_pr_identity(
        self, run_id: str, pr_url: str, observation: dict[str, Any],
        *, expected_base: str = "main",
    ) -> bool:
        """Read-only exact identity check for a future writer; never binds a GET."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM selfmod_pr_identity WHERE pr_url=?", (pr_url,),
            ).fetchone()
        return self._pr_identity_matches(
            row, run_id, pr_url, observation,
            expected_base=expected_base, require_bound=True,
        )

    def reserve_pr_feedback_round(
        self, run_id: str, pr_url: str, observation: dict[str, Any],
        *, feedback_key: str,
    ) -> dict[str, Any] | None:
        """Reserve one of two durable rounds; an interrupted round is never reclaimed."""
        if not isinstance(feedback_key, str) or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", feedback_key,
        ) is None:
            return None
        key_digest = hashlib.sha256(feedback_key.encode("utf-8")).hexdigest()

        def reserve() -> dict[str, Any] | None:
            self._db.execute("BEGIN IMMEDIATE")
            identity = self._db.execute(
                "SELECT * FROM selfmod_pr_identity WHERE pr_url=?", (pr_url,),
            ).fetchone()
            if not self._pr_identity_matches(
                identity, run_id, pr_url, observation,
                expected_base="main", require_bound=True,
            ):
                return None
            if self._db.execute(
                "SELECT 1 FROM selfmod_pr_standdown WHERE pr_url=?", (pr_url,),
            ).fetchone() is not None:
                return None
            previous = self._db.execute(
                "SELECT round, state FROM selfmod_pr_feedback_rounds "
                "WHERE pr_url=? ORDER BY round DESC LIMIT 1", (pr_url,),
            ).fetchone()
            if previous is not None and (
                int(previous["round"]) >= 2 or previous["state"] != "pushed"
            ):
                return None
            number = 1 if previous is None else int(previous["round"]) + 1
            if self._db.execute(
                "SELECT 1 FROM selfmod_pr_feedback_rounds "
                "WHERE pr_url=? AND feedback_key=?", (pr_url, key_digest),
            ).fetchone() is not None:
                return None
            now = self._clock()
            expected_sha = str(identity["pushed_sha"])
            self._db.execute(
                "INSERT INTO selfmod_pr_feedback_rounds "
                "(pr_url,round,run_id,expected_sha,feedback_key,state,reserved_ts,updated_ts) "
                "VALUES (?,?,?,?,?,'reserved',?,?)",
                (pr_url, number, run_id, expected_sha, key_digest, now, now),
            )
            return {"round": number, "expected_sha": expected_sha,
                    "feedback_key": key_digest, "state": "reserved"}

        return self._write(reserve)  # type: ignore[return-value]

    def finish_pr_feedback_round(
        self, pr_url: str, round_number: int, *, state: str,
        new_sha: str = "",
    ) -> bool:
        """Finalize once. A confirmed push atomically advances the expected PR head."""
        if state not in {"pushed", "failed", "uncertain"}:
            return False
        if state == "pushed":
            if not isinstance(new_sha, str) or re.fullmatch(
                r"[0-9a-f]{40}|[0-9a-f]{64}", new_sha,
            ) is None:
                return False
        elif new_sha:
            return False

        def finish() -> int:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT expected_sha,state FROM selfmod_pr_feedback_rounds "
                "WHERE pr_url=? AND round=?", (pr_url, round_number),
            ).fetchone()
            if row is None or row["state"] != "reserved":
                return 0
            if state == "pushed":
                if new_sha == row["expected_sha"]:
                    return 0
                moved = self._db.execute(
                    "UPDATE selfmod_pr_identity SET pushed_sha=? "
                    "WHERE pr_url=? AND pushed_sha=? AND bound_ts IS NOT NULL",
                    (new_sha, pr_url, row["expected_sha"]),
                )
                if moved.rowcount != 1:
                    return 0
            self._db.execute(
                "UPDATE selfmod_pr_feedback_rounds SET state=?,new_sha=?,updated_ts=? "
                "WHERE pr_url=? AND round=? AND state='reserved'",
                (state, new_sha, self._clock(), pr_url, round_number),
            )
            return 1

        return bool(self._write(finish))

    def get_pr_feedback_rounds(self, pr_url: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT round,run_id,expected_sha,feedback_key,state,new_sha,reserved_ts,updated_ts "
                "FROM selfmod_pr_feedback_rounds WHERE pr_url=? ORDER BY round", (pr_url,),
            ).fetchall()
        return [dict(row) for row in rows]

    def reserve_pr_standdown(
        self, run_id: str, pr_url: str, observation: dict[str, Any],
        *, reason_code: str,
    ) -> str | None:
        """Reserve one public stand-down message; never automatically retry it."""
        if not isinstance(reason_code, str) or reason_code not in _PR_STANDDOWN_REASONS:
            return None

        def reserve() -> str | None:
            self._db.execute("BEGIN IMMEDIATE")
            identity = self._db.execute(
                "SELECT * FROM selfmod_pr_identity WHERE pr_url=?", (pr_url,),
            ).fetchone()
            if not self._pr_identity_matches(
                identity, run_id, pr_url, observation,
                expected_base="main", require_bound=True,
            ):
                return None
            if self._db.execute(
                "SELECT 1 FROM selfmod_pr_standdown WHERE pr_url=?", (pr_url,),
            ).fetchone() is not None:
                return None
            if self._db.execute(
                "SELECT 1 FROM selfmod_pr_feedback_rounds "
                "WHERE pr_url=? AND state='reserved' LIMIT 1", (pr_url,),
            ).fetchone() is not None:
                return None
            marker = str(uuid.uuid4())
            now = self._clock()
            self._db.execute(
                "INSERT INTO selfmod_pr_standdown "
                "(pr_url,run_id,marker,reason_code,state,reserved_ts,updated_ts) "
                "VALUES (?,?,?,?,'reserved',?,?)",
                (pr_url, run_id, marker, reason_code, now, now),
            )
            return marker

        return self._write(reserve)  # type: ignore[return-value]

    def mark_pr_standdown(self, pr_url: str, marker: str, *, state: str) -> bool:
        if state not in {"posted", "uncertain"}:
            return False

        def mark() -> int:
            self._db.execute("BEGIN IMMEDIATE")
            cursor = self._db.execute(
                "UPDATE selfmod_pr_standdown SET state=?,updated_ts=? "
                "WHERE pr_url=? AND marker=? AND state='reserved'",
                (state, self._clock(), pr_url, marker),
            )
            return int(cursor.rowcount == 1)

        return bool(self._write(mark))

    def get_pr_standdown(self, pr_url: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT run_id,marker,reason_code,state,reserved_ts,updated_ts "
                "FROM selfmod_pr_standdown WHERE pr_url=?", (pr_url,),
            ).fetchone()
        return dict(row) if row is not None else None

    def selfmod_history(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return terminal proposal outcomes newest first."""
        with self._lock:
            rows = self._db.execute(
                """
                SELECT id, run_id, ts, title, dry_run, tier, branch, pr_url, outcome, ok, repair_attempts
                FROM selfmod_history ORDER BY id DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [
            {
                "_ledger_id": int(row["id"]),
                "run_id": str(row["run_id"]),
                "ts": float(row["ts"]),
                "title": str(row["title"]),
                "dry_run": bool(row["dry_run"]),
                "tier": str(row["tier"]),
                "branch": row["branch"],
                "pr_url": row["pr_url"],
                "stage": str(row["outcome"]),
                "outcome": str(row["outcome"]),
                "ok": bool(row["ok"]),
                "repair_attempts": int(row["repair_attempts"]),
            }
            for row in rows
        ]

    def find_selfmod_run_id(self, *, branch: str | None = None,
                            pr_url: str | None = None) -> str | None:
        """Resolve an exact self-mod branch or PR URL to its full run id."""
        if bool(branch) == bool(pr_url):
            raise ValueError("provide exactly one of branch or pr_url")
        column, value = ("branch", branch) if branch else ("pr_url", pr_url)
        with self._lock:
            row = self._db.execute(
                f"SELECT run_id FROM selfmod_history WHERE {column}=? "
                "ORDER BY id DESC LIMIT 1",
                (value,),
            ).fetchone()
        return str(row["run_id"]) if row and row["run_id"] else None

    def claim_pr_poll(self, run_id: str, pr_number: int, *, interval: float = 900.0) -> bool:
        """Atomically reserve one GET-only poll across restarts and local processes."""
        if not run_id or pr_number <= 0 or interval <= 0:
            return False

        def claim() -> int:
            self._db.execute("BEGIN IMMEDIATE")
            now = self._clock()
            limited = self._db.execute(
                "SELECT until_ts FROM selfmod_pr_rate_limit WHERE id=1"
            ).fetchone()
            if limited is not None and float(limited["until_ts"]) > now:
                return 0
            cursor = self._db.execute(
                "INSERT INTO selfmod_pr_poll_state(run_id, pr_number, next_poll_ts) VALUES(?,?,?) "
                "ON CONFLICT(run_id, pr_number) DO UPDATE SET next_poll_ts=excluded.next_poll_ts "
                "WHERE selfmod_pr_poll_state.next_poll_ts<=?",
                (str(run_id), int(pr_number), now + float(interval), now),
            )
            if cursor.rowcount:
                self._db.execute(
                    "DELETE FROM selfmod_pr_poll_state WHERE rowid NOT IN "
                    "(SELECT rowid FROM selfmod_pr_poll_state "
                    "ORDER BY next_poll_ts DESC LIMIT 250)"
                )
            return cursor.rowcount

        return self._write(claim) == 1

    def defer_pr_polls(self, retry_at: float) -> None:
        """Honor a GitHub rate-limit deadline globally, including after restart."""

        def defer() -> int:
            self._db.execute("BEGIN IMMEDIATE")
            now = self._clock()
            until = max(now + 60.0, float(retry_at))
            if not math.isfinite(until):
                until = now + 60.0
            self._db.execute(
                "INSERT INTO selfmod_pr_rate_limit(id, until_ts) VALUES(1,?) "
                "ON CONFLICT(id) DO UPDATE SET until_ts=MAX(until_ts, excluded.until_ts)",
                (until,),
            )
            return 1

        self._write(defer)

    @staticmethod
    def _safe_pr_evidence(observation: dict[str, Any]) -> dict[str, Any]:
        def text(value: object, limit: int) -> str:
            return str(redact_value(str(value or ""))).replace("\x00", "")[:limit]

        checks = [{
            "name": text(item.get("name"), 120),
            "status": text(item.get("status"), 32),
            "conclusion": text(item.get("conclusion"), 32),
        } for item in (observation.get("checks") or [])[:30] if isinstance(item, dict)]
        notes = []
        for item in (observation.get("review_notes") or [])[:10]:
            if not isinstance(item, dict):
                continue
            body = item.get("body")
            body_text = body.get("text") if isinstance(body, dict) else body
            if body_text:
                note = {"kind": text(item.get("kind"), 16),
                        "id": ObservabilityLedger._bounded_int(item.get("id")),
                        "body": {"trust": "untrusted", "text": text(body_text, 500)}}
                if "author_id" in item:
                    note["author_id"] = ObservabilityLedger._bounded_int(item["author_id"])
                for key, limit in (("path", 300), ("commit_id", 64), ("created_at", 40)):
                    if key in item:
                        note[key] = text(item[key], limit)
                notes.append(note)
        body = observation.get("pr_body")
        body_text = body.get("text") if isinstance(body, dict) else body
        ci_state = observation.get("ci_state")
        return {"checks": checks, "review_notes": notes,
                "pr_body": {"trust": "untrusted", "text": text(body_text, 500)} if body_text else None,
                "ci_state": ci_state if ci_state in {
                    "unknown", "incomplete", "pending", "failed", "passed",
                } else "unknown",
                "ownership_verified": observation.get("ownership_verified") is True}

    def record_pr_observation(self, run_id: str, observation: dict[str, Any]) -> int:
        """Persist the latest safe snapshot per run/PR with bounded retention."""
        evidence_json = json.dumps(self._safe_pr_evidence(observation), sort_keys=True)

        def insert() -> int:
            safe_run_id = str(run_id)
            pr_number = self._bounded_int(observation.get("number"))
            self._db.execute(
                "DELETE FROM selfmod_pr_observations WHERE run_id=? AND pr_number=?",
                (safe_run_id, pr_number),
            )
            cursor = self._db.execute(
                """INSERT INTO selfmod_pr_observations
                   (run_id, ts, pr_number, pr_url, status, checks_total, checks_failed,
                    checks_pending, review_state, changes_requested, evidence_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (safe_run_id, self._clock(), pr_number,
                 str(redact_value(str(observation.get("url", ""))))[:300],
                 str(observation.get("status", "unknown"))[:32],
                 self._bounded_int(observation.get("checks_total")),
                 self._bounded_int(observation.get("checks_failed")),
                 self._bounded_int(observation.get("checks_pending")),
                 str(observation.get("review_state", "waiting"))[:32],
                 self._bounded_int(observation.get("changes_requested")), evidence_json),
            )
            self._db.execute(
                "DELETE FROM selfmod_pr_observations WHERE id NOT IN "
                "(SELECT id FROM selfmod_pr_observations ORDER BY id DESC LIMIT 250)"
            )
            return int(cursor.lastrowid)
        row_id = self._write(insert)
        assert isinstance(row_id, int)
        return row_id

    def pr_observations(self, run_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """SELECT ts, pr_number, pr_url, status, checks_total, checks_failed,
                          checks_pending, review_state, changes_requested, evidence_json
                   FROM selfmod_pr_observations WHERE run_id=? ORDER BY id DESC LIMIT ?""",
                (str(run_id), max(1, min(int(limit), 100))),
            ).fetchall()
        return [{**{key: row[key] for key in row.keys() if key != "evidence_json"},
                 **json.loads(str(row["evidence_json"]))} for row in rows]

    def clear_selfmod_history(self) -> int:
        """Clear persisted proposal records and return the deleted count."""
        with self._lock, self._db:
            count = int(self._db.execute("SELECT COUNT(*) FROM selfmod_history").fetchone()[0])
            self._db.execute("DELETE FROM selfmod_history")
            self._db.execute("DELETE FROM selfmod_pr_identity")
            self._db.execute("DELETE FROM selfmod_pr_feedback_rounds")
            self._db.execute("DELETE FROM selfmod_pr_standdown")
            self._db.execute("DELETE FROM selfmod_pr_observations")
            self._db.execute("DELETE FROM selfmod_pr_poll_state")
        return count

    def close(self) -> None:
        with self._lock:
            self._db.close()
