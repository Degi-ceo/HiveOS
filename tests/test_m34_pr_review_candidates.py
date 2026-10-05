"""Durable prepared-candidate receipts for future authenticated PR repair."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor

from hive.core.pr_review_auth import PrReviewAuthorizationStore, PrReviewBinding


def _binding(**changes) -> PrReviewBinding:
    tree = "c" * 40
    values = {
        "owner": "Degi-ceo", "repo": "HiveOS", "pr_number": 123,
        "pr_url": "https://github.com/Degi-ceo/HiveOS/pull/123",
        "pr_id": 234, "author_id": 345, "head_repo_id": 456, "base_repo_id": 456,
        "branch": "hive/auto-" + "a" * 32, "base_ref": "main",
        "expected_head": "b" * 40, "path": "src/hive/core/example.py",
        "operation": "PATCH_CODE", "candidate_tree": tree,
        "candidate_digest": hashlib.sha256(f"git-tree\0{tree}".encode()).hexdigest(),
        "run_id": str(uuid.uuid4()), "feedback_round": 1,
        "feedback_key_digest": "e" * 64,
    }
    values.update(changes)
    return PrReviewBinding(**values)


def test_prepare_candidate_is_durable_idempotent_and_binds_parent(tmp_path):
    db = tmp_path / "state.sqlite"
    store = PrReviewAuthorizationStore(db)
    binding = _binding()
    commit = "d" * 40

    request_id = store.prepare_candidate(
        binding, candidate_commit=commit, candidate_parent=binding.expected_head,
    )
    assert request_id
    assert store.prepare_candidate(
        binding, candidate_commit=commit, candidate_parent=binding.expected_head,
    ) == request_id

    restarted = PrReviewAuthorizationStore(db)
    assert restarted.prepared_candidate(request_id, binding) == {
        "candidate_commit": commit,
        "candidate_parent": binding.expected_head,
    }


def test_prepare_candidate_rejects_changed_commit_or_parent_and_never_creates_second_receipt(tmp_path):
    store = PrReviewAuthorizationStore(tmp_path / "state.sqlite")
    binding = _binding()
    request_id = store.prepare_candidate(
        binding, candidate_commit="d" * 40, candidate_parent=binding.expected_head,
    )
    assert request_id
    assert store.prepare_candidate(
        binding, candidate_commit="e" * 40, candidate_parent=binding.expected_head,
    ) is None
    assert store.prepare_candidate(
        binding, candidate_commit="d" * 40, candidate_parent="f" * 40,
    ) is None


def test_prepare_candidate_refuses_invalid_commit_and_unmatched_binding(tmp_path):
    store = PrReviewAuthorizationStore(tmp_path / "state.sqlite")
    binding = _binding()
    assert store.prepare_candidate(
        binding, candidate_commit="not-an-oid", candidate_parent=binding.expected_head,
    ) is None
    request_id = store.prepare_candidate(
        binding, candidate_commit="d" * 40, candidate_parent=binding.expected_head,
    )
    changed = _binding(run_id=str(uuid.uuid4()))
    assert store.prepared_candidate(request_id, changed) is None


def test_prepared_candidate_fails_closed_for_corrupt_or_terminal_receipts(tmp_path):
    db = tmp_path / "state.sqlite"
    store = PrReviewAuthorizationStore(db)
    binding = _binding()
    request_id = store.prepare_candidate(
        binding, candidate_commit="d" * 40, candidate_parent=binding.expected_head,
    )
    assert store.decide(
        request_id, store._binding_digest(binding), approved=True,
        principal="human:approver",
    )
    assert store.prepared_candidate(request_id, binding) is not None
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pr_review_candidates SET candidate_commit='invalid' WHERE request_id=?",
            (request_id,),
        )
    assert store.prepared_candidate(request_id, binding) is None


def test_parallel_prepare_candidate_creates_one_immutable_receipt(tmp_path):
    db = tmp_path / "state.sqlite"
    binding = _binding()
    stores = [PrReviewAuthorizationStore(db) for _ in range(8)]

    def prepare(store):
        return store.prepare_candidate(
            binding, candidate_commit="d" * 40, candidate_parent=binding.expected_head,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        request_ids = list(pool.map(prepare, stores))
    assert len(set(request_ids)) == 1
    assert stores[0].prepared_candidate(request_ids[0], binding) == {
        "candidate_commit": "d" * 40,
        "candidate_parent": binding.expected_head,
    }


def test_parallel_prepare_candidates_with_different_commits_choose_only_one(tmp_path):
    db = tmp_path / "state.sqlite"
    binding = _binding()
    stores = [PrReviewAuthorizationStore(db) for _ in range(8)]

    def prepare(index_store):
        index, store = index_store
        commit = "d" * 40 if index % 2 else "e" * 40
        return store.prepare_candidate(
            binding, candidate_commit=commit, candidate_parent=binding.expected_head,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(prepare, enumerate(stores)))
    request_ids = {result for result in results if result is not None}
    assert len(request_ids) == 1
    receipt = stores[0].prepared_candidate(next(iter(request_ids)), binding)
    assert receipt is not None
    assert receipt["candidate_commit"] in {"d" * 40, "e" * 40}
    assert all(result in {None, next(iter(request_ids))} for result in results)


def test_prepared_candidate_is_unavailable_after_denial(tmp_path):
    store = PrReviewAuthorizationStore(tmp_path / "state.sqlite")
    binding = _binding()
    request_id = store.prepare_candidate(
        binding, candidate_commit="d" * 40, candidate_parent=binding.expected_head,
    )
    assert store.decide(
        request_id, store._binding_digest(binding), approved=False,
        principal="human:approver",
    )
    assert store.prepared_candidate(request_id, binding) is None
