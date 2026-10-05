"""M39 durable binding of host-issued evidence to an exact REVIEW candidate."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from hive.core.candidate_evidence import CandidateEvidenceBinding, CandidateEvidenceIssuer
from hive.core.pr_review_auth import PrReviewAuthorizationStore, PrReviewBinding
from hive.observability.persistence import ObservabilityLedger


HEAD = "b" * 40
NEXT_HEAD = "f" * 40
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


def _binding(
    run_id: str, feedback_key_digest: str, *, expected_head: str = HEAD,
    feedback_round: int = 1,
) -> PrReviewBinding:
    return PrReviewBinding(
        owner="Degi-ceo", repo="HiveOS", pr_number=123, pr_url=URL,
        pr_id=5001, author_id=7001, head_repo_id=9001, base_repo_id=9001,
        branch=BRANCH, base_ref="main", expected_head=expected_head,
        path="src/hive/core/example.py", operation="PATCH_CODE",
        candidate_tree=TREE, candidate_digest=_tree_digest(TREE), run_id=run_id,
        feedback_round=feedback_round, feedback_key_digest=feedback_key_digest,
    )


def _observation(*, head: str = HEAD) -> dict:
    return {
        "number": 123, "url": URL, "state": "open", "head_sha": head,
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
        run_id=binding.run_id, checkout_id="5" * 64, base_commit=binding.expected_head,
        candidate_commit=COMMIT, candidate_tree=TREE,
        candidate_digest=binding.candidate_digest, image_digest=IMAGE,
    )
    issuer = CandidateEvidenceIssuer(db, _Runner(), git_run=_Git(evidence))
    assert asyncio.run(issuer.issue(evidence, str(db.parent))) is not None
    return evidence


def test_host_evidence_binds_exact_pending_candidate_and_survives_restart(tmp_path):
    db = tmp_path / "state.sqlite"
    run_id = str(uuid.uuid4())
    ledger = ObservabilityLedger(db)
    try:
        round_row = _reserve_round(ledger, run_id)
    finally:
        ledger.close()
    binding = _binding(run_id, round_row["feedback_key"])
    store = PrReviewAuthorizationStore(db)
    request_id = store.prepare_candidate(
        binding, candidate_commit=COMMIT, candidate_parent=HEAD,
    )
    assert request_id is not None
    evidence = _issue_evidence(db, binding)

    stores = [PrReviewAuthorizationStore(db) for _ in range(6)]
    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        linked_rows = list(pool.map(
            lambda candidate_store: candidate_store.bind_host_evidence(request_id, evidence),
            stores,
        ))

    assert all(linked_row is not None for linked_row in linked_rows)
    linked = linked_rows[0]
    assert linked is not None
    assert linked is not None
    assert linked.request_id == request_id
    assert linked.binding == binding
    assert linked.candidate_commit == COMMIT
    assert linked.evidence_binding == evidence
    assert PrReviewAuthorizationStore(db).bind_host_evidence(request_id, evidence) == linked
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pr_review_host_evidence").fetchone()[0] == 1
    ledger = ObservabilityLedger(db)
    try:
        assert ledger.get_pr_feedback_rounds(URL)[0]["state"] == "reserved"
    finally:
        ledger.close()


def test_host_evidence_refuses_mismatch_tampering_and_terminal_authorization(tmp_path):
    db = tmp_path / "state.sqlite"
    run_id = str(uuid.uuid4())
    ledger = ObservabilityLedger(db)
    try:
        round_row = _reserve_round(ledger, run_id)
    finally:
        ledger.close()
    binding = _binding(run_id, round_row["feedback_key"])
    store = PrReviewAuthorizationStore(db)
    request_id = store.prepare_candidate(
        binding, candidate_commit=COMMIT, candidate_parent=HEAD,
    )
    assert request_id is not None
    evidence = _issue_evidence(db, binding)
    assert store.bind_host_evidence(
        request_id, replace(evidence, candidate_commit="e" * 40),
    ) is None
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE candidate_evidence_receipts SET check_results_json=?",
            ('[["pytest",0]]',),
        )
    assert store.bind_host_evidence(request_id, evidence) is None

    fresh_db = tmp_path / "terminal.sqlite"
    ledger = ObservabilityLedger(fresh_db)
    try:
        terminal_round = _reserve_round(ledger, run_id)
    finally:
        ledger.close()
    terminal_binding = _binding(run_id, terminal_round["feedback_key"])
    terminal_store = PrReviewAuthorizationStore(fresh_db)
    terminal_id = terminal_store.prepare_candidate(
        terminal_binding, candidate_commit=COMMIT, candidate_parent=HEAD,
    )
    assert terminal_id is not None
    terminal_evidence = _issue_evidence(fresh_db, terminal_binding)
    assert terminal_store.decide(
        terminal_id, terminal_store._binding_digest(terminal_binding),
        approved=False, principal="human:approver",
    )
    assert terminal_store.bind_host_evidence(terminal_id, terminal_evidence) is None


def test_host_evidence_refuses_terminal_feedback_round_or_identity_drift(tmp_path):
    db = tmp_path / "state.sqlite"
    run_id = str(uuid.uuid4())
    ledger = ObservabilityLedger(db)
    try:
        round_row = _reserve_round(ledger, run_id)
    finally:
        ledger.close()
    binding = _binding(run_id, round_row["feedback_key"])
    store = PrReviewAuthorizationStore(db)
    request_id = store.prepare_candidate(
        binding, candidate_commit=COMMIT, candidate_parent=HEAD,
    )
    assert request_id is not None
    evidence = _issue_evidence(db, binding)

    ledger = ObservabilityLedger(db)
    try:
        assert ledger.finish_pr_feedback_round(URL, 1, state="failed")
    finally:
        ledger.close()
    assert store.bind_host_evidence(request_id, evidence) is None

    fresh_db = tmp_path / "identity.sqlite"
    ledger = ObservabilityLedger(fresh_db)
    try:
        fresh_round = _reserve_round(ledger, run_id)
    finally:
        ledger.close()
    fresh_binding = _binding(run_id, fresh_round["feedback_key"])
    fresh_store = PrReviewAuthorizationStore(fresh_db)
    fresh_id = fresh_store.prepare_candidate(
        fresh_binding, candidate_commit=COMMIT, candidate_parent=HEAD,
    )
    assert fresh_id is not None
    fresh_evidence = _issue_evidence(fresh_db, fresh_binding)
    with sqlite3.connect(fresh_db) as conn:
        conn.execute(
            "UPDATE selfmod_pr_identity SET pushed_sha=? WHERE pr_url=?",
            ("e" * 40, URL),
        )
    assert fresh_store.bind_host_evidence(fresh_id, fresh_evidence) is None


def test_host_evidence_accepts_second_round_at_current_pushed_head(tmp_path):
    db = tmp_path / "state.sqlite"
    run_id = str(uuid.uuid4())
    ledger = ObservabilityLedger(db)
    try:
        _reserve_round(ledger, run_id)
        assert ledger.finish_pr_feedback_round(URL, 1, state="pushed", new_sha=NEXT_HEAD)
        second = ledger.reserve_pr_feedback_round(
            run_id, URL, _observation(head=NEXT_HEAD),
            feedback_key="review:second-deterministic-signal",
        )
        assert second is not None and second["round"] == 2
    finally:
        ledger.close()
    binding = _binding(
        run_id, second["feedback_key"], expected_head=NEXT_HEAD, feedback_round=2,
    )
    store = PrReviewAuthorizationStore(db)
    request_id = store.prepare_candidate(
        binding, candidate_commit=COMMIT, candidate_parent=NEXT_HEAD,
    )
    assert request_id is not None
    evidence = _issue_evidence(db, binding)

    assert store.bind_host_evidence(request_id, evidence) is not None
