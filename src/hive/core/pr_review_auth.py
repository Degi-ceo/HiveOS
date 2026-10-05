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

from hive.core.candidate_evidence import CandidateEvidenceBinding, CandidateEvidenceReceipt

_BRANCH = re.compile(r"hive/auto-(?:[a-z0-9]{1,8}-)?[0-9a-f]{32}\Z")
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REPO_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_PATH_PART = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_TTL_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class PrReviewContext:
    """Caller-supplied identity data for a future prepared-code binding.

    Constructing this value grants no authority. Runtime must later derive and
    compare it against its authenticated PR identity and feedback reservation.
    """

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
        if _OID.fullmatch(self.expected_head) is None:
            raise ValueError("invalid expected PR head")
        if _DIGEST.fullmatch(self.feedback_key_digest) is None:
            raise ValueError("invalid feedback reservation")
        if type(self.feedback_round) is not int or self.feedback_round not in (1, 2):
            raise ValueError("invalid repair round")
        try:
            if str(uuid.UUID(self.run_id)) != self.run_id:
                raise ValueError
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("invalid run identity") from exc

    def bind_candidate(self, path: str, candidate_tree: str) -> "PrReviewBinding":
        """Build the only source/test push binding permitted by this context."""
        return PrReviewBinding(
            owner=self.owner,
            repo=self.repo,
            pr_number=self.pr_number,
            pr_url=self.pr_url,
            pr_id=self.pr_id,
            author_id=self.author_id,
            head_repo_id=self.head_repo_id,
            base_repo_id=self.base_repo_id,
            branch=self.branch,
            base_ref=self.base_ref,
            expected_head=self.expected_head,
            path=path,
            operation="PATCH_CODE",
            candidate_tree=candidate_tree,
            candidate_digest=hashlib.sha256(
                f"git-tree\0{candidate_tree}".encode("utf-8")
            ).hexdigest(),
            run_id=self.run_id,
            feedback_round=self.feedback_round,
            feedback_key_digest=self.feedback_key_digest,
        )


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


@dataclass(frozen=True, slots=True)
class PrReviewEvidenceLink:
    """Internal-only join of a pending REVIEW candidate and host evidence.

    Constructing this value grants no authority.  A future writer must still
    revalidate the local object and live PR, and consume a separate approver
    decision immediately before its one permitted non-force publication.
    """

    request_id: str
    binding: PrReviewBinding
    candidate_commit: str
    evidence_binding: CandidateEvidenceBinding
    bound_at: float


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
            conn.execute(
                """CREATE TABLE IF NOT EXISTS pr_review_candidates(
                  request_id TEXT PRIMARY KEY,
                  candidate_commit TEXT NOT NULL,
                  candidate_parent TEXT NOT NULL,
                  prepared_at REAL NOT NULL,
                  FOREIGN KEY(request_id) REFERENCES pr_review_authorizations(id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS pr_review_host_evidence(
                  request_id TEXT PRIMARY KEY,
                  binding_digest TEXT NOT NULL UNIQUE,
                  evidence_binding_digest TEXT NOT NULL UNIQUE,
                  bound_at REAL NOT NULL,
                  FOREIGN KEY(request_id) REFERENCES pr_review_authorizations(id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS pr_review_policy_evidence(
                  binding_digest TEXT PRIMARY KEY,
                  binding_json TEXT NOT NULL,
                  candidate_commit TEXT NOT NULL,
                  candidate_parent TEXT NOT NULL,
                  policy_version TEXT NOT NULL,
                  checked_at REAL NOT NULL
                )"""
            )

    @property
    def database_path(self) -> Path:
        """Return the durable store path required to share M36/M40 evidence."""
        return Path(self._path)

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

    def record_policy_checked_candidate(
        self, binding: PrReviewBinding, *, candidate_commit: str,
        candidate_parent: str,
    ) -> bool:
        """Durably attest that SelfModifier completed its pre-review gates.

        This is deliberately not an authorization and cannot create a pending
        approval request.  It binds the exact commit, parent, tree and digest
        already represented by ``binding`` after the self-modification flow
        has completed its protected-path, secret-scan, test, and evaluation
        checks.  A conflicting or malformed historical record fails closed.
        """
        if (
            not isinstance(binding, PrReviewBinding)
            or not isinstance(candidate_commit, str)
            or _OID.fullmatch(candidate_commit) is None
            or candidate_parent != binding.expected_head
        ):
            return False
        digest = self._binding_digest(binding)
        binding_json = binding.canonical_json()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT binding_json,candidate_commit,candidate_parent,policy_version "
                    "FROM pr_review_policy_evidence WHERE binding_digest=?",
                    (digest,),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO pr_review_policy_evidence "
                        "(binding_digest,binding_json,candidate_commit,candidate_parent,policy_version,checked_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (
                            digest, binding_json, candidate_commit,
                            candidate_parent, "m41-self-mod-v1", time.time(),
                        ),
                    )
                elif (
                    existing["binding_json"] != binding_json
                    or existing["candidate_commit"] != candidate_commit
                    or existing["candidate_parent"] != candidate_parent
                    or existing["policy_version"] != "m41-self-mod-v1"
                ):
                    conn.rollback()
                    return False
                conn.commit()
                return True
            except sqlite3.Error:
                conn.rollback()
                return False

    def policy_checked_candidate(
        self, binding: PrReviewBinding,
    ) -> dict[str, str] | None:
        """Return a matching post-gate candidate attestation, never authority."""
        if not isinstance(binding, PrReviewBinding):
            return None
        digest = self._binding_digest(binding)
        try:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT binding_json,candidate_commit,candidate_parent,policy_version "
                    "FROM pr_review_policy_evidence WHERE binding_digest=?",
                    (digest,),
                ).fetchone()
        except sqlite3.Error:
            return None
        if (
            row is None
            or row["binding_json"] != binding.canonical_json()
            or row["policy_version"] != "m41-self-mod-v1"
            or _OID.fullmatch(str(row["candidate_commit"])) is None
            or row["candidate_parent"] != binding.expected_head
        ):
            return None
        return {
            "candidate_commit": str(row["candidate_commit"]),
            "candidate_parent": str(row["candidate_parent"]),
        }

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

    def prepare_candidate(
        self, binding: PrReviewBinding, *, candidate_commit: str,
        candidate_parent: str,
    ) -> str | None:
        """Atomically persist one already-tested local commit for a REVIEW request.

        This is not a write authorization. A later self-modification seam must
        independently prove that the commit still has this parent/tree before it
        can consume an out-of-band decision and attempt a non-force push.
        """
        if (
            not isinstance(binding, PrReviewBinding)
            or _OID.fullmatch(candidate_commit) is None
            or candidate_parent != binding.expected_head
        ):
            return None
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
                    "SELECT id,state,expires_at,binding_json FROM pr_review_authorizations "
                    "WHERE binding_digest=?", (digest,),
                ).fetchone()
                if existing is None:
                    reserved = conn.execute(
                        "SELECT 1 FROM pr_review_authorizations WHERE "
                        "pr_id=? AND expected_head=? AND feedback_round=?",
                        (binding.pr_id, binding.expected_head, binding.feedback_round),
                    ).fetchone()
                    if reserved is not None:
                        conn.commit()
                        return None
                    request_id = str(uuid.uuid4())
                    conn.execute(
                        "INSERT INTO pr_review_authorizations "
                        "(id,pr_id,pr_url,expected_head,feedback_round,binding_json,"
                        "binding_digest,state,created_at,expires_at) "
                        "VALUES (?,?,?,?,?,? ,?,'pending',?,?)",
                        (request_id, binding.pr_id, binding.pr_url, binding.expected_head,
                         binding.feedback_round, binding_json, digest,
                         now, now + _TTL_SECONDS),
                    )
                    state = "pending"
                else:
                    request_id = str(existing["id"])
                    state = str(existing["state"])
                    if (
                        existing["binding_json"] != binding_json
                        or state not in ("pending", "approved")
                        or float(existing["expires_at"]) <= now
                    ):
                        conn.commit()
                        return None
                receipt = conn.execute(
                    "SELECT candidate_commit,candidate_parent FROM pr_review_candidates "
                    "WHERE request_id=?", (request_id,),
                ).fetchone()
                if receipt is None:
                    if state != "pending":
                        conn.commit()
                        return None
                    conn.execute(
                        "INSERT INTO pr_review_candidates "
                        "(request_id,candidate_commit,candidate_parent,prepared_at) "
                        "VALUES (?,?,?,?)",
                        (request_id, candidate_commit, candidate_parent, now),
                    )
                elif (
                    receipt["candidate_commit"] != candidate_commit
                    or receipt["candidate_parent"] != candidate_parent
                ):
                    conn.commit()
                    return None
                conn.commit()
                return request_id
            except Exception:
                conn.rollback()
                raise

    def prepared_candidate(
        self, request_id: str, binding: PrReviewBinding,
    ) -> dict[str, str] | None:
        """Return a matching live candidate receipt without exposing candidate text."""
        if not isinstance(binding, PrReviewBinding):
            return None
        digest = self._binding_digest(binding)
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT a.binding_json,a.binding_digest,a.state,a.expires_at,
                          c.candidate_commit,c.candidate_parent
                   FROM pr_review_authorizations AS a
                   JOIN pr_review_candidates AS c ON c.request_id=a.id
                   WHERE a.id=? AND a.binding_digest=?""",
                (request_id, digest),
            ).fetchone()
        if (
            row is None
            or row["state"] not in ("pending", "approved")
            or not isinstance(row["expires_at"], (int, float))
            or float(row["expires_at"]) <= time.time()
            or row["binding_json"] != binding.canonical_json()
            or not self._stored_binding_matches(str(row["binding_json"]), digest)
            or _OID.fullmatch(str(row["candidate_commit"])) is None
            or row["candidate_parent"] != binding.expected_head
        ):
            return None
        return {
            "candidate_commit": str(row["candidate_commit"]),
            "candidate_parent": str(row["candidate_parent"]),
        }

    @staticmethod
    def _evidence_digest(binding: CandidateEvidenceBinding) -> str:
        return hashlib.sha256(binding.canonical_json().encode("utf-8")).hexdigest()

    @staticmethod
    def _stored_evidence_matches(
        row: sqlite3.Row, expected: CandidateEvidenceBinding,
    ) -> bool:
        """Validate a persisted M36 receipt without trusting caller objects."""
        try:
            stored_json = str(row["binding_json"])
            stored = CandidateEvidenceBinding(**json.loads(stored_json))
            if stored != expected or stored.canonical_json() != stored_json:
                return False
            results = tuple(
                (str(name), int(code))
                for name, code in json.loads(str(row["check_results_json"]))
            )
            CandidateEvidenceReceipt(stored, results, float(row["issued_at"]))
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            return False
        return True

    @staticmethod
    def _identity_matches_binding(row: sqlite3.Row | None, binding: PrReviewBinding) -> bool:
        """Require a bound, immutable Hive-created PR identity for the candidate."""
        if row is None or row["bound_ts"] is None:
            return False
        try:
            return (
                str(row["run_id"]) == binding.run_id
                and int(row["pr_number"]) == binding.pr_number
                and str(row["branch"]) == binding.branch
                and str(row["pushed_sha"]) == binding.expected_head
                and int(row["pr_id"]) == binding.pr_id
                and int(row["author_id"]) == binding.author_id
                and int(row["head_repo_id"]) == binding.head_repo_id
                and int(row["base_repo_id"]) == binding.base_repo_id
                and str(row["head_ref"]) == binding.branch
                and str(row["base_ref"]) == binding.base_ref
                and int(row["created_pr_id"]) == binding.pr_id
                and int(row["created_author_id"]) == binding.author_id
                and int(row["created_head_repo_id"]) == binding.head_repo_id
                and int(row["created_base_repo_id"]) == binding.base_repo_id
                and str(row["created_head_ref"]) == binding.branch
                and _OID.fullmatch(str(row["created_head_sha"])) is not None
                and str(row["created_base_ref"]) == binding.base_ref
            )
        except (TypeError, ValueError, KeyError):
            return False

    def _bind_host_evidence_locked(
        self, conn: sqlite3.Connection, request_id: str,
        evidence_binding: CandidateEvidenceBinding, now: float,
    ) -> PrReviewEvidenceLink | None:
        """Bind persisted evidence while the caller owns a write transaction."""
        candidate = conn.execute(
            """SELECT a.binding_json,a.binding_digest,a.state,a.expires_at,
                      c.candidate_commit,c.candidate_parent
               FROM pr_review_authorizations AS a
               JOIN pr_review_candidates AS c ON c.request_id=a.id
               WHERE a.id=?""",
            (request_id,),
        ).fetchone()
        if candidate is None:
            return None
        binding_json = str(candidate["binding_json"])
        digest = str(candidate["binding_digest"])
        try:
            binding = PrReviewBinding(**json.loads(binding_json))
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            return None
        if (
            candidate["state"] != "pending"
            or not isinstance(candidate["expires_at"], (int, float))
            or float(candidate["expires_at"]) <= now
            or binding.canonical_json() != binding_json
            or self._binding_digest(binding) != digest
            or _OID.fullmatch(str(candidate["candidate_commit"])) is None
            or candidate["candidate_parent"] != binding.expected_head
            or evidence_binding.run_id != binding.run_id
            or evidence_binding.base_commit != binding.expected_head
            or evidence_binding.candidate_commit != candidate["candidate_commit"]
            or evidence_binding.candidate_tree != binding.candidate_tree
            or evidence_binding.candidate_digest != binding.candidate_digest
        ):
            return None
        evidence_digest = self._evidence_digest(evidence_binding)
        evidence = conn.execute(
            "SELECT binding_json,check_results_json,issued_at "
            "FROM candidate_evidence_receipts WHERE binding_digest=?",
            (evidence_digest,),
        ).fetchone()
        feedback = conn.execute(
            "SELECT run_id,expected_sha,feedback_key,state "
            "FROM selfmod_pr_feedback_rounds WHERE pr_url=? AND round=?",
            (binding.pr_url, binding.feedback_round),
        ).fetchone()
        identity = conn.execute(
            "SELECT * FROM selfmod_pr_identity WHERE pr_url=?",
            (binding.pr_url,),
        ).fetchone()
        standdown = conn.execute(
            "SELECT 1 FROM selfmod_pr_standdown WHERE pr_url=?",
            (binding.pr_url,),
        ).fetchone()
        if (
            evidence is None
            or not self._stored_evidence_matches(evidence, evidence_binding)
            or feedback is None
            or str(feedback["run_id"]) != binding.run_id
            or str(feedback["expected_sha"]) != binding.expected_head
            or str(feedback["feedback_key"]) != binding.feedback_key_digest
            or feedback["state"] != "reserved"
            or not self._identity_matches_binding(identity, binding)
            or standdown is not None
        ):
            return None
        existing = conn.execute(
            "SELECT binding_digest,evidence_binding_digest,bound_at "
            "FROM pr_review_host_evidence WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO pr_review_host_evidence "
                "(request_id,binding_digest,evidence_binding_digest,bound_at) "
                "VALUES (?,?,?,?)",
                (request_id, digest, evidence_digest, now),
            )
            bound_at = now
        elif (
            existing["binding_digest"] != digest
            or existing["evidence_binding_digest"] != evidence_digest
            or not isinstance(existing["bound_at"], (int, float))
        ):
            return None
        else:
            bound_at = float(existing["bound_at"])
        return PrReviewEvidenceLink(
            request_id=request_id, binding=binding,
            candidate_commit=str(candidate["candidate_commit"]),
            evidence_binding=evidence_binding, bound_at=bound_at,
        )

    def prepare_candidate_with_host_evidence(
        self, binding: PrReviewBinding, *, candidate_commit: str,
        candidate_parent: str, evidence_binding: CandidateEvidenceBinding,
    ) -> PrReviewEvidenceLink | None:
        """Atomically persist a new REVIEW candidate only with valid host evidence.

        This is an internal preparation primitive, not a write authorization.
        It never invokes Git, approves or consumes a decision, pushes, or changes
        feedback state. Existing pending requests without evidence are refused
        rather than being retroactively linked after they became approvable.
        """
        if (
            not isinstance(binding, PrReviewBinding)
            or not isinstance(evidence_binding, CandidateEvidenceBinding)
            or _OID.fullmatch(candidate_commit) is None
            or candidate_parent != binding.expected_head
        ):
            return None
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
                    "SELECT id,state,expires_at,binding_json FROM pr_review_authorizations "
                    "WHERE binding_digest=?", (digest,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["binding_json"] != binding_json
                        or existing["state"] != "pending"
                        or not isinstance(existing["expires_at"], (int, float))
                        or float(existing["expires_at"]) <= now
                    ):
                        conn.rollback()
                        return None
                    request_id = str(existing["id"])
                    receipt = conn.execute(
                        "SELECT candidate_commit,candidate_parent FROM pr_review_candidates "
                        "WHERE request_id=?", (request_id,),
                    ).fetchone()
                    linked = conn.execute(
                        "SELECT 1 FROM pr_review_host_evidence WHERE request_id=?",
                        (request_id,),
                    ).fetchone()
                    if (
                        receipt is None
                        or receipt["candidate_commit"] != candidate_commit
                        or receipt["candidate_parent"] != candidate_parent
                        or linked is None
                    ):
                        conn.rollback()
                        return None
                else:
                    reserved = conn.execute(
                        "SELECT 1 FROM pr_review_authorizations WHERE "
                        "pr_id=? AND expected_head=? AND feedback_round=?",
                        (binding.pr_id, binding.expected_head, binding.feedback_round),
                    ).fetchone()
                    if reserved is not None:
                        conn.rollback()
                        return None
                    request_id = str(uuid.uuid4())
                    conn.execute(
                        "INSERT INTO pr_review_authorizations "
                        "(id,pr_id,pr_url,expected_head,feedback_round,binding_json,"
                        "binding_digest,state,created_at,expires_at) "
                        "VALUES (?,?,?,?,?,? ,?,'pending',?,?)",
                        (request_id, binding.pr_id, binding.pr_url, binding.expected_head,
                         binding.feedback_round, binding_json, digest,
                         now, now + _TTL_SECONDS),
                    )
                    conn.execute(
                        "INSERT INTO pr_review_candidates "
                        "(request_id,candidate_commit,candidate_parent,prepared_at) "
                        "VALUES (?,?,?,?)",
                        (request_id, candidate_commit, candidate_parent, now),
                    )
                link = self._bind_host_evidence_locked(
                    conn, request_id, evidence_binding, now,
                )
                if link is None:
                    conn.rollback()
                    return None
                conn.commit()
                return link
            except sqlite3.Error:
                conn.rollback()
                return None
            except Exception:
                conn.rollback()
                raise

    def bind_host_evidence(
        self, request_id: str, evidence_binding: CandidateEvidenceBinding,
    ) -> PrReviewEvidenceLink | None:
        """Atomically join one pending REVIEW candidate to a persisted M36 receipt.

        ``evidence_binding`` is a selector, not proof: the matching record is
        re-read from durable storage while the authorization, candidate, PR
        identity, and feedback reservation remain locked.  This method never
        approves, consumes, invokes Git, pushes, or changes feedback state.
        """
        try:
            if str(uuid.UUID(request_id)) != request_id:
                return None
        except (TypeError, ValueError, AttributeError):
            return None
        if not isinstance(evidence_binding, CandidateEvidenceBinding):
            return None
        now = time.time()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE pr_review_authorizations SET state='expired' "
                    "WHERE id=? AND state='pending' AND expires_at<=?",
                    (request_id, now),
                )
                link = self._bind_host_evidence_locked(
                    conn, request_id, evidence_binding, now,
                )
                conn.commit()
                return link
            except sqlite3.Error:
                conn.rollback()
                return None
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
