"""Supervisor-issued receipts for a future source-candidate review boundary.

This is deliberately not a candidate gate and is not wired into push, PR, or
approval code.  It records only that a credential-owning host independently
verified one detached Git checkout and ran a fixed diagnostic set in a pinned,
network-isolated container.  It does not attest candidate-owned runtime output,
tool traces, model claims, or behaviour.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CHECK_SET_VERSION = "m36-v1"
_CHECKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ruff", ("ruff", "check", "src/hive")),
    ("compileall", ("python", "-m", "compileall", "src/hive")),
    ("pytest", ("python", "-m", "pytest", "-q", "tests")),
)

GitRunner = Callable[[str | list[str], str | None], Awaitable[tuple[int, str]]]


class PinnedEvidenceRunner(Protocol):
    """Minimal container boundary required by ``CandidateEvidenceIssuer``."""

    @property
    def pinned_image_digest(self) -> str: ...

    async def run_pinned_evidence(
        self, candidate_worktree: str, argv: tuple[str, ...],
    ) -> tuple[int, str]: ...


@dataclass(frozen=True, slots=True)
class CandidateEvidenceBinding:
    """Identity that a receipt must bind before any diagnostics run."""

    run_id: str
    checkout_id: str
    base_commit: str
    candidate_commit: str
    candidate_tree: str
    candidate_digest: str
    image_digest: str
    check_set_version: str = _CHECK_SET_VERSION

    def __post_init__(self) -> None:
        try:
            if str(uuid.UUID(self.run_id)) != self.run_id:
                raise ValueError
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("invalid evidence run identity") from exc
        if _DIGEST.fullmatch(self.checkout_id) is None:
            raise ValueError("invalid opaque checkout identity")
        if any(_OID.fullmatch(value) is None for value in (
            self.base_commit, self.candidate_commit, self.candidate_tree,
        )):
            raise ValueError("invalid Git evidence identity")
        expected_digest = hashlib.sha256(
            f"git-tree\0{self.candidate_tree}".encode("utf-8")
        ).hexdigest()
        if self.candidate_digest != expected_digest or _DIGEST.fullmatch(self.candidate_digest) is None:
            raise ValueError("candidate digest does not match Git tree")
        if _IMAGE_DIGEST.fullmatch(self.image_digest) is None:
            raise ValueError("candidate evidence image must be pinned")
        if self.check_set_version != _CHECK_SET_VERSION:
            raise ValueError("unsupported evidence check set")

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@dataclass(frozen=True, slots=True)
class CandidateEvidenceReceipt:
    """Immutable, redacted record of fixed diagnostics for one Git object."""

    binding: CandidateEvidenceBinding
    check_results: tuple[tuple[str, int], ...]
    issued_at: float

    def __post_init__(self) -> None:
        if self.check_results != tuple((name, 0) for name, _argv in _CHECKS):
            raise ValueError("evidence receipt has invalid diagnostic outcomes")
        if not isinstance(self.issued_at, (int, float)) or isinstance(self.issued_at, bool):
            raise ValueError("evidence receipt has invalid timestamp")


class CandidateEvidenceIssuer:
    """Issue durable receipts only after independent Git/container verification.

    A receipt cannot be reconstructed from candidate output.  Any Git mismatch,
    cancelled diagnostic, non-zero exit, pinned-image mismatch, or persistence
    uncertainty returns ``None`` and leaves no accepted result.
    """

    def __init__(self, db_path: str | Path, runner: PinnedEvidenceRunner, *,
                 git_run: GitRunner, clock: Callable[[], float] = time.time) -> None:
        self._path = str(db_path)
        if self._path == ":memory:":
            raise ValueError("candidate evidence requires durable storage")
        self._runner = runner
        self._git_run = git_run
        self._clock = clock
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS candidate_evidence_receipts(
                  binding_digest TEXT PRIMARY KEY,
                  binding_json TEXT NOT NULL,
                  check_results_json TEXT NOT NULL,
                  issued_at REAL NOT NULL
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _binding_digest(binding: CandidateEvidenceBinding) -> str:
        return hashlib.sha256(binding.canonical_json().encode("utf-8")).hexdigest()

    @staticmethod
    def _decode_receipt(row: sqlite3.Row, binding: CandidateEvidenceBinding) -> CandidateEvidenceReceipt | None:
        try:
            if str(row["binding_json"]) != binding.canonical_json():
                return None
            stored = CandidateEvidenceBinding(**json.loads(str(row["binding_json"])))
            if stored != binding:
                return None
            results = tuple((str(name), int(code)) for name, code in json.loads(str(row["check_results_json"])))
            receipt = CandidateEvidenceReceipt(binding, results, float(row["issued_at"]))
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            return None
        return receipt

    def receipt(self, binding: CandidateEvidenceBinding) -> CandidateEvidenceReceipt | None:
        """Return a matching complete receipt, never a partially stored record."""
        if not isinstance(binding, CandidateEvidenceBinding):
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT binding_json, check_results_json, issued_at FROM candidate_evidence_receipts "
                "WHERE binding_digest=?", (self._binding_digest(binding),),
            ).fetchone()
        return self._decode_receipt(row, binding) if row is not None else None

    async def issue(
        self, binding: CandidateEvidenceBinding, candidate_worktree: str,
    ) -> CandidateEvidenceReceipt | None:
        """Verify a detached candidate before/after fixed container diagnostics."""
        if not isinstance(binding, CandidateEvidenceBinding):
            return None
        try:
            if self._runner.pinned_image_digest != binding.image_digest:
                return None
            if not await self._matches_detached_checkout(binding, candidate_worktree):
                return None
            # A historical receipt is not a substitute for the caller proving
            # that the checkout it supplied still denotes the bound object.
            # Re-check before returning an idempotent result so a missing,
            # attached, dirty, or changed worktree never looks accepted.
            existing = self.receipt(binding)
            if existing is not None:
                return existing
            results: list[tuple[str, int]] = []
            for name, argv in _CHECKS:
                rc, _safe_output = await self._runner.run_pinned_evidence(candidate_worktree, argv)
                if rc != 0:
                    return None
                results.append((name, rc))
            if not await self._matches_detached_checkout(binding, candidate_worktree):
                return None
            receipt = CandidateEvidenceReceipt(binding, tuple(results), float(self._clock()))
            return self._persist(receipt)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    async def _matches_detached_checkout(
        self, binding: CandidateEvidenceBinding, worktree: str,
    ) -> bool:
        checks = (
            (["git", "rev-parse", "HEAD"], binding.candidate_commit),
            (["git", "rev-parse", "HEAD^"], binding.base_commit),
            (["git", "rev-parse", "HEAD^{tree}"], binding.candidate_tree),
            # Ignored files can still influence an import, test discovery, or
            # project configuration. A receipt for a Git tree must not be
            # issued while any untracked *or ignored* local byte is present.
            (["git", "status", "--porcelain", "--ignored"], ""),
        )
        for command, expected in checks:
            rc, output = await self._git_run(command, worktree)
            if rc != 0 or output.strip().lower() != expected:
                return False
        detached_rc, _ = await self._git_run(["git", "symbolic-ref", "--quiet", "HEAD"], worktree)
        return detached_rc == 1

    def _persist(self, receipt: CandidateEvidenceReceipt) -> CandidateEvidenceReceipt | None:
        binding = receipt.binding
        digest = self._binding_digest(binding)
        binding_json = binding.canonical_json()
        results_json = json.dumps(receipt.check_results, separators=(",", ":"))
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT binding_json, check_results_json, issued_at FROM candidate_evidence_receipts "
                    "WHERE binding_digest=?", (digest,),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO candidate_evidence_receipts "
                        "(binding_digest, binding_json, check_results_json, issued_at) VALUES (?, ?, ?, ?)",
                        (digest, binding_json, results_json, receipt.issued_at),
                    )
                    row = conn.execute(
                        "SELECT binding_json, check_results_json, issued_at FROM candidate_evidence_receipts "
                        "WHERE binding_digest=?", (digest,),
                    ).fetchone()
                else:
                    row = existing
                decoded = self._decode_receipt(row, binding) if row is not None else None
                conn.commit()
                return decoded
            except Exception:
                conn.rollback()
                raise
