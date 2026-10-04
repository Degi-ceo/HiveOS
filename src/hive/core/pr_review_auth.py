"""Dormant, one-use authorization ledger for future existing-PR code repair.

This store does not authenticate an operator or push a branch. A later gateway
boundary must authenticate the out-of-band approver before calling ``decide``;
a later repair path must revalidate the live PR and tested candidate before
calling ``consume`` immediately before a non-force push.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path

_BRANCH = re.compile(r"hive/auto-(?:[a-z0-9]{1,8}-)?[0-9a-f]{32}\Z")
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REPO_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_PATH_PART = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_TTL_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class PrReviewBinding:
    """Exact identity and immutable candidate approved for one push attempt."""

    owner: str
    repo: str
    pr_number: int
    pr_url: str
    pr_id: int
    author_id: int
    head_repo_id: int
    base_repo_id: int
    branch: str
    base_ref: str
    expected_head: str
    path: str
    operation: str
    candidate_tree: str
    candidate_digest: str
    run_id: str
    feedback_round: int
    feedback_key_digest: str

    def __post_init__(self) -> None:
        if not all(_REPO_PART.fullmatch(value) for value in (self.owner, self.repo)):
            raise ValueError("invalid repository identity")
        if self.pr_url != f"https://github.com/{self.owner}/{self.repo}/pull/{self.pr_number}":
            raise ValueError("invalid PR URL")
        if any(type(value) is not int or value <= 0 for value in (
            self.pr_number, self.pr_id, self.author_id,
            self.head_repo_id, self.base_repo_id,
        )) or self.head_repo_id != self.base_repo_id:
            raise ValueError("invalid PR identity")
        if _BRANCH.fullmatch(self.branch) is None or self.base_ref != "main":
            raise ValueError("invalid PR branch or base")
        if _OID.fullmatch(self.expected_head) is None or _OID.fullmatch(self.candidate_tree) is None:
            raise ValueError("invalid Git object identity")
        if _DIGEST.fullmatch(self.candidate_digest) is None:
            raise ValueError("invalid candidate digest")
        expected_digest = hashlib.sha256(
            f"git-tree\0{self.candidate_tree}".encode("utf-8")
        ).hexdigest()
        if self.candidate_digest != expected_digest:
            raise ValueError("candidate digest does not match Git tree")
        if _DIGEST.fullmatch(self.feedback_key_digest) is None:
            raise ValueError("invalid feedback reservation")
        if self.operation != "PATCH_CODE" or type(self.feedback_round) is not int or self.feedback_round not in (1, 2):
            raise ValueError("invalid repair operation or round")
        try:
            if str(uuid.UUID(self.run_id)) != self.run_id:
                raise ValueError
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("invalid run identity") from exc
        if "\\" in self.path or not self.path.endswith(".py"):
            raise ValueError("invalid candidate path")
        parts = self.path.split("/")
        if (
            (parts[:2] != ["src", "hive"] and parts[0] != "tests")
            or len(parts) < (3 if parts[0] == "src" else 2)
            or any(part in (".", "..") or _PATH_PART.fullmatch(part) is None for part in parts)
        ):
            raise ValueError("invalid candidate path")

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class PrReviewAuthorizationStore:
    """Durable, idempotent request and atomic one-use decision ledger."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        if self._path == ":memory:":
            raise ValueError("review authorizations require durable storage")
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS pr_review_authorizations(
                  id TEXT PRIMARY KEY,
                  pr_id INTEGER NOT NULL,
                  pr_url TEXT NOT NULL,
                  expected_head TEXT NOT NULL,
                  feedback_round INTEGER NOT NULL,
                  binding_json TEXT NOT NULL,
                  binding_digest TEXT NOT NULL UNIQUE,
                  state TEXT NOT NULL CHECK(state IN
                    ('pending', 'approved', 'denied', 'expired', 'revoked', 'consumed')),
                  created_at REAL NOT NULL,
                  expires_at REAL NOT NULL,
                  decided_at REAL,
                  consumed_at REAL,
                  approver_principal TEXT NOT NULL DEFAULT '',
                  UNIQUE(pr_id, expected_head, feedback_round)
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pr_review_auth_state "
                "ON pr_review_authorizations(state, expires_at)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _binding_digest(binding: PrReviewBinding) -> str:
        return hashlib.sha256(binding.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def _stored_binding_matches(cls, binding_json: str, digest: str) -> bool:
        try:
            binding = PrReviewBinding(**json.loads(binding_json))
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            return False
        return binding.canonical_json() == binding_json and cls._binding_digest(binding) == digest

    def request(self, binding: PrReviewBinding) -> str | None:
        """Reserve this exact candidate once; never recreate a spent request."""
        if not isinstance(binding, PrReviewBinding):
            raise TypeError("binding must be a PrReviewBinding")
        now = time.time()
        binding_json = binding.canonical_json()
        digest = self._binding_digest(binding)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE pr_review_authorizations SET state='expired' "
                    "WHERE state IN ('pending', 'approved') AND expires_at<=?",
                    (now,),
                )
                existing = conn.execute(
                    "SELECT id, state, expires_at, binding_json FROM pr_review_authorizations "
                    "WHERE binding_digest=?", (digest,),
                ).fetchone()
                if existing is not None:
                    result = (
                        str(existing["id"])
                        if existing["state"] in ("pending", "approved")
                        and float(existing["expires_at"]) > now
                        and existing["binding_json"] == binding_json else None
                    )
                else:
                    reserved = conn.execute(
                        "SELECT 1 FROM pr_review_authorizations WHERE "
                        "pr_id=? AND expected_head=? AND feedback_round=?",
                        (binding.pr_id, binding.expected_head, binding.feedback_round),
                    ).fetchone()
                    if reserved is not None:
                        conn.commit()
                        return None
                    result = str(uuid.uuid4())
                    conn.execute(
                        "INSERT INTO pr_review_authorizations "
                        "(id, pr_id, pr_url, expected_head, feedback_round, binding_json, "
                        "binding_digest, state, created_at, expires_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                        (result, binding.pr_id, binding.pr_url, binding.expected_head,
                         binding.feedback_round, binding_json, digest, now, now + _TTL_SECONDS),
                    )
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def decide(self, request_id: str, binding_digest: str, *, approved: bool,
               principal: str) -> bool:
        """Record one externally authenticated decision; this store is not auth."""
        if principal != "human:approver" or type(approved) is not bool or _DIGEST.fullmatch(binding_digest) is None:
            return False
        now = time.time()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT binding_json FROM pr_review_authorizations "
                    "WHERE id=? AND binding_digest=?", (request_id, binding_digest),
                ).fetchone()
                if row is None or not self._stored_binding_matches(
                    str(row["binding_json"]), binding_digest,
                ):
                    conn.commit()
                    return False
                conn.execute(
                    "UPDATE pr_review_authorizations SET state='expired' "
                    "WHERE id=? AND state='pending' AND expires_at<=?",
                    (request_id, now),
                )
                changed = conn.execute(
                    "UPDATE pr_review_authorizations SET state=?, decided_at=?, "
                    "approver_principal=? WHERE id=? AND binding_digest=? AND state='pending' "
                    "AND expires_at>?",
                    ("approved" if approved else "denied", now, principal,
                     request_id, binding_digest, now),
                ).rowcount
                conn.commit()
                return changed == 1
            except Exception:
                conn.rollback()
                raise

    def revoke(self, request_id: str, binding_digest: str, *, principal: str) -> bool:
        """A later approver can revoke an unconsumed decision permanently."""
        if principal != "human:approver" or _DIGEST.fullmatch(binding_digest) is None:
            return False
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT binding_json FROM pr_review_authorizations "
                    "WHERE id=? AND binding_digest=?", (request_id, binding_digest),
                ).fetchone()
                if row is None or not self._stored_binding_matches(
                    str(row["binding_json"]), binding_digest,
                ):
                    conn.commit()
                    return False
                changed = conn.execute(
                    "UPDATE pr_review_authorizations SET state='revoked', decided_at=? "
                    "WHERE id=? AND binding_digest=? AND state='approved'",
                    (time.time(), request_id, binding_digest),
                ).rowcount
                conn.commit()
                return changed == 1
            except Exception:
                conn.rollback()
                raise

    def consume(self, request_id: str, binding: PrReviewBinding) -> bool:
        """Atomically spend one matching approved request immediately before push."""
        if not isinstance(binding, PrReviewBinding):
            return False
        now = time.time()
        digest = self._binding_digest(binding)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE pr_review_authorizations SET state='expired' "
                    "WHERE id=? AND state='approved' AND expires_at<=?",
                    (request_id, now),
                )
                changed = conn.execute(
                    "UPDATE pr_review_authorizations SET state='consumed', consumed_at=? "
                    "WHERE id=? AND binding_digest=? AND state='approved' "
                    "AND binding_json=? AND expires_at>? "
                    "AND approver_principal='human:approver'",
                    (now, request_id, digest, binding.canonical_json(), now),
                ).rowcount
                conn.commit()
                return changed == 1
            except Exception:
                conn.rollback()
                raise

    def public_pending(self, *, limit: int = 20) -> list[dict]:
        """Safe operator projection; no candidate body, prompts, or tool data."""
        safe_limit = max(1, min(int(limit), 100))
        now = time.time()
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, binding_json, binding_digest, expires_at FROM pr_review_authorizations "
                "WHERE state='pending' AND typeof(expires_at) IN ('integer', 'real') "
                "AND expires_at>? ORDER BY created_at LIMIT ?",
                (now, safe_limit),
            ).fetchall()
        result = []
        for row in rows:
            try:
                raw = json.loads(str(row["binding_json"]))
                binding = PrReviewBinding(**raw)
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue
            if self._binding_digest(binding) != row["binding_digest"]:
                continue
            result.append({
                "id": str(row["id"]),
                "pr_number": binding.pr_number,
                "repository": f"{binding.owner}/{binding.repo}",
                "head_sha": binding.expected_head,
                "path": binding.path,
                "operation": binding.operation,
                "candidate_digest": binding.candidate_digest,
                "binding_digest": str(row["binding_digest"]),
                "expires_at": float(row["expires_at"]),
            })
        return result
