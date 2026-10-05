"""M40 atomic creation of a REVIEW candidate with host evidence."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor

from hive.core.candidate_evidence import CandidateEvidenceBinding, CandidateEvidenceIssuer
from hive.core.pr_review_auth import PrReviewAuthorizationStore, PrReviewBinding
from hive.observability.persistence import ObservabilityLedger


HEAD = "b" * 40
COMMIT = "d" * 40
TREE = "c" * 40
IMAGE = "sha256:" + "4" * 64
BRANCH = "hive/auto-" + "a" * 32
URL = "https://github.com/Degi-ceo/HiveOS/pull/123"


class _Git:
    def __init__(self, evidence: CandidateEvidenceBinding) -> None:
        self._evidence = evidence

    async def __call__(self, command, _worktree):
        values = {
            ("git", "rev-parse", "HEAD"): self._evidence.candidate_commit,
            ("git", "rev-parse", "HEAD^"): self._evidence.base_commit,
            ("git", "rev-parse", "HEAD^{tree}"): self._evidence.candidate_tree,
            ("git", "status", "--porcelain", "--ignored"): "",
        }
        if tuple(command) == ("git", "symbolic-ref", "--quiet", "HEAD"):
            return 1, ""
        return 0, values[tuple(command)] + "\n"


class _Runner:
    pinned_image_digest = IMAGE

    async def run_pinned_evidence(self, _worktree, _argv):
        return 0, "credential-like output must not be persisted"


def _tree_digest(tree: str) -> str:
    return hashlib.sha256(f"git-tree\0{tree}".encode("utf-8")).hexdigest()


def _binding(run_id: str, feedback_key_digest: str) -> PrReviewBinding:
    return PrReviewBinding(
        owner="Degi-ceo", repo="HiveOS", pr_number=123, pr_url=URL,
        pr_id=5001, author_id=7001, head_repo_id=9001, base_repo_id=9001,
        branch=BRANCH, base_ref="main", expected_head=HEAD,
        path="src/hive/core/example.py", operation="PATCH_CODE",
        candidate_tree=TREE, candidate_digest=_tree_digest(TREE), run_id=run_id,
        feedback_round=1, feedback_key_digest=feedback_key_digest,
    )


def _observation() -> dict:
    return {
        "number": 123, "url": URL, "state": "open", "head_sha": HEAD,
        "pr_id": 5001, "author_id": 7001, "head_repo_id": 9001,
        "base_repo_id": 9001, "head_ref": BRANCH, "base_ref": "main",
    }


def _reserve_round(ledger: ObservabilityLedger, run_id: str) -> dict:
    receipt = {
        "url": URL, "number": 123, "pr_id": 5001, "author_id": 7001,
        "head_repo_id": 9001, "base_repo_id": 9001, "head_ref": BRANCH,
        "head_sha": HEAD, "base_ref": "main",
    }
    ledger.record_selfmod({
        "run_id": run_id, "title": "candidate", "branch": BRANCH,
        "pr_url": URL, "head_sha": HEAD, "stage": "pushed", "ok": True,
        "pr_creation": receipt,
    })
    assert ledger.bind_pr_identity(run_id, URL, _observation())
    reserved = ledger.reserve_pr_feedback_round(
        run_id, URL, _observation(), feedback_key="review:deterministic-signal",
    )
    assert reserved is not None
    return reserved


def _issue_evidence(db, binding: PrReviewBinding) -> CandidateEvidenceBinding:
    evidence = CandidateEvidenceBinding(
        run_id=binding.run_id, checkout_id="5" * 64, base_commit=HEAD,
        candidate_commit=COMMIT, candidate_tree=TREE,
        candidate_digest=binding.candidate_digest, image_digest=IMAGE,
    )
    issuer = CandidateEvidenceIssuer(db, _Runner(), git_run=_Git(evidence))
    assert asyncio.run(issuer.issue(evidence, str(db.parent))) is not None
    return evidence


def _prepared(db):
    run_id = str(uuid.uuid4())
    ledger = ObservabilityLedger(db)
    try:
        round_row = _reserve_round(ledger, run_id)
    finally:
        ledger.close()
    binding = _binding(run_id, round_row["feedback_key"])
    return binding, _issue_evidence(db, binding)


def _table_counts(db) -> tuple[int, int, int]:
    with sqlite3.connect(db) as conn:
        return tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "pr_review_authorizations",
                "pr_review_candidates",
                "pr_review_host_evidence",
            )
        )


def test_atomic_prepare_creates_only_a_fully_evidenced_pending_request(tmp_path):
    db = tmp_path / "state.sqlite"
    binding, evidence = _prepared(db)

    link = PrReviewAuthorizationStore(db).prepare_candidate_with_host_evidence(
        binding, candidate_commit=COMMIT, candidate_parent=HEAD,
        evidence_binding=evidence,
    )

    assert link is not None
    assert link.binding == binding
    assert link.candidate_commit == COMMIT
    assert link.evidence_binding == evidence
    pending = PrReviewAuthorizationStore(db).public_pending()
    assert [row["id"] for row in pending] == [link.request_id]
    assert _table_counts(db) == (1, 1, 1)


def test_atomic_prepare_rolls_back_candidate_when_evidence_is_absent_or_tampered(tmp_path):
    db = tmp_path / "missing.sqlite"
    run_id = str(uuid.uuid4())
    ledger = ObservabilityLedger(db)
    try:
        round_row = _reserve_round(ledger, run_id)
    finally:
        ledger.close()
    binding = _binding(run_id, round_row["feedback_key"])
    unissued = CandidateEvidenceBinding(
        run_id=run_id, checkout_id="5" * 64, base_commit=HEAD,
        candidate_commit=COMMIT, candidate_tree=TREE,
        candidate_digest=binding.candidate_digest, image_digest=IMAGE,
    )

    store = PrReviewAuthorizationStore(db)
    assert store.prepare_candidate_with_host_evidence(
        binding, candidate_commit=COMMIT, candidate_parent=HEAD,
        evidence_binding=unissued,
    ) is None
    assert store.public_pending() == []
    assert _table_counts(db) == (0, 0, 0)

    other_db = tmp_path / "tampered.sqlite"
    tampered_binding, tampered_evidence = _prepared(other_db)
    with sqlite3.connect(other_db) as conn:
        conn.execute(
            "UPDATE candidate_evidence_receipts SET check_results_json=?",
            ('[["pytest",0]]',),
        )
    assert PrReviewAuthorizationStore(other_db).prepare_candidate_with_host_evidence(
        tampered_binding, candidate_commit=COMMIT, candidate_parent=HEAD,
        evidence_binding=tampered_evidence,
    ) is None
    assert _table_counts(other_db) == (0, 0, 0)


def test_atomic_prepare_is_idempotent_under_concurrency_and_refuses_legacy_pending(tmp_path):
    db = tmp_path / "state.sqlite"
    binding, evidence = _prepared(db)
    stores = [PrReviewAuthorizationStore(db) for _ in range(6)]
    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        links = list(pool.map(
            lambda store: store.prepare_candidate_with_host_evidence(
                binding, candidate_commit=COMMIT, candidate_parent=HEAD,
                evidence_binding=evidence,
            ),
            stores,
        ))
    assert all(link is not None for link in links)
    assert len({link.request_id for link in links if link is not None}) == 1
    assert _table_counts(db) == (1, 1, 1)

    legacy_db = tmp_path / "legacy.sqlite"
    legacy_binding, legacy_evidence = _prepared(legacy_db)
    legacy = PrReviewAuthorizationStore(legacy_db)
    assert legacy.prepare_candidate(
        legacy_binding, candidate_commit=COMMIT, candidate_parent=HEAD,
    ) is not None
    assert legacy.prepare_candidate_with_host_evidence(
        legacy_binding, candidate_commit=COMMIT, candidate_parent=HEAD,
        evidence_binding=legacy_evidence,
    ) is None
    assert _table_counts(legacy_db) == (1, 1, 0)
