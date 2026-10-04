"""Durable, one-use REVIEW binding for future source/test PR repairs."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from hive.core import pr_review_auth as auth


def _tree_digest(tree: str) -> str:
    return hashlib.sha256(f"git-tree\0{tree}".encode()).hexdigest()


def _binding(**changes) -> auth.PrReviewBinding:
    values = dict(
        owner="Degi-ceo", repo="HiveOS", pr_number=123,
        pr_url="https://github.com/Degi-ceo/HiveOS/pull/123",
        pr_id=234, author_id=345, head_repo_id=456, base_repo_id=456,
        branch="hive/auto-" + "a" * 32, base_ref="main",
        expected_head="b" * 40, path="src/hive/core/example.py",
        operation="PATCH_CODE", candidate_tree="c" * 40,
        candidate_digest=_tree_digest("c" * 40),
        run_id=str(uuid.uuid4()), feedback_round=1,
        feedback_key_digest="e" * 64,
    )
    values.update(changes)
    return auth.PrReviewBinding(**values)


@pytest.mark.parametrize("changes", [
    {"path": "../secret.py"}, {"path": "src/hive/../secret.py"},
    {"path": "src\\hive\\core\\example.py"},
    {"path": ".github/workflows/ci.py"}, {"path": "docs/guide.py"},
    {"path": "tests/../../example.py"}, {"path": "tests/example.md"},
    {"operation": "CREATE_FILE"}, {"expected_head": "b" * 39},
    {"pr_url": "https://evil.example/pull/123"},
    {"feedback_key_digest": "e" * 63},
    {"candidate_digest": "d" * 63}, {"candidate_digest": "d" * 64},
    {"candidate_tree": "z" * 40},
    {"base_ref": "other"}, {"branch": "main"},
    {"pr_id": True}, {"head_repo_id": 10}, {"feedback_round": 3},
    {"run_id": "private prompt"},
])
def test_binding_rejects_untrusted_identity_or_path(changes):
    with pytest.raises(ValueError):
        _binding(**changes)


def test_request_is_durable_idempotent_and_public_projection_is_allowlisted(tmp_path):
    db = tmp_path / "state.sqlite"
    first = auth.PrReviewAuthorizationStore(db)
    binding = _binding()
    request_id = first.request(binding)
    assert request_id and first.request(binding) == request_id
    restarted = auth.PrReviewAuthorizationStore(db)
    assert restarted.request(binding) == request_id
    projection = restarted.public_pending()
    assert len(projection) == 1
    assert projection[0]["id"] == request_id
    assert projection[0]["path"] == binding.path
    assert projection[0]["candidate_digest"] == binding.candidate_digest
    assert projection[0]["binding_digest"] == first._binding_digest(binding)
    assert "run_id" not in projection[0]
    assert "author_id" not in projection[0]
    assert "binding_json" not in projection[0]


def test_decision_requires_exact_approver_and_is_one_use(tmp_path):
    store = auth.PrReviewAuthorizationStore(tmp_path / "state.sqlite")
    binding = _binding()
    request_id = store.request(binding)
    digest = store._binding_digest(binding)
    assert not store.decide(request_id, digest, approved=True, principal="agent:normal")
    assert not store.decide(request_id, digest, approved=1, principal="human:approver")
    assert not store.decide(request_id, "f" * 64, approved=True, principal="human:approver")
    assert store.decide(request_id, digest, approved=True, principal="human:approver")
    assert not store.decide(request_id, digest, approved=False, principal="human:approver")
    assert not store.consume(request_id, replace(
        binding, candidate_tree="e" * 40,
        candidate_digest=_tree_digest("e" * 40),
    ))
    assert store.consume(request_id, binding)
    assert not store.consume(request_id, binding)
    assert store.request(binding) is None


def test_denial_cannot_be_reversed_or_consumed(tmp_path):
    store = auth.PrReviewAuthorizationStore(tmp_path / "state.sqlite")
    binding = _binding()
    request_id = store.request(binding)
    digest = store._binding_digest(binding)
    assert store.decide(request_id, digest, approved=False, principal="human:approver")
    assert not store.decide(request_id, digest, approved=True, principal="human:approver")
    assert not store.consume(request_id, binding)
    assert store.request(binding) is None


def test_same_pr_head_round_cannot_request_a_new_candidate(tmp_path):
    store = auth.PrReviewAuthorizationStore(tmp_path / "state.sqlite")
    first = _binding()
    first_id = store.request(first)
    changed_candidate = replace(
        first, candidate_digest=_tree_digest("f" * 40), candidate_tree="f" * 40,
    )
    assert store.request(changed_candidate) is None
    assert store.decide(first_id, store._binding_digest(first), approved=False,
                        principal="human:approver")
    assert store.request(changed_candidate) is None


def test_same_numeric_pr_cannot_reissue_with_repository_case_change(tmp_path):
    store = auth.PrReviewAuthorizationStore(tmp_path / "state.sqlite")
    first = _binding()
    first_id = store.request(first)
    alias = replace(
        first, owner="degi-ceo", repo="hiveos",
        pr_url="https://github.com/degi-ceo/hiveos/pull/123",
        candidate_tree="f" * 40, candidate_digest=_tree_digest("f" * 40),
    )
    assert store.request(alias) is None
    assert store.decide(first_id, store._binding_digest(first), approved=False,
                        principal="human:approver")
    assert store.request(alias) is None


def test_approval_can_be_revoked_before_consumption(tmp_path):
    store = auth.PrReviewAuthorizationStore(tmp_path / "state.sqlite")
    binding = _binding()
    request_id = store.request(binding)
    digest = store._binding_digest(binding)
    assert store.decide(request_id, digest, approved=True, principal="human:approver")
    assert not store.revoke(request_id, "f" * 64, principal="human:approver")
    assert store.revoke(request_id, digest, principal="human:approver")
    assert not store.consume(request_id, binding)
    assert not store.revoke(request_id, digest, principal="human:approver")
    assert store.request(binding) is None


def test_every_binding_field_is_checked_before_consume(tmp_path):
    store = auth.PrReviewAuthorizationStore(tmp_path / "state.sqlite")
    binding = _binding()
    request_id = store.request(binding)
    assert store.decide(request_id, store._binding_digest(binding), approved=True,
                        principal="human:approver")
    alternatives = [
        replace(binding, pr_number=124,
                pr_url="https://github.com/Degi-ceo/HiveOS/pull/124"),
        replace(binding, pr_id=235),
        replace(binding, author_id=346),
        replace(binding, head_repo_id=457, base_repo_id=457),
        replace(binding, expected_head="e" * 40),
        replace(binding, path="tests/example.py"),
        replace(binding, candidate_tree="e" * 40,
                candidate_digest=_tree_digest("e" * 40)),
        replace(binding, run_id=str(uuid.uuid4())),
        replace(binding, feedback_round=2),
        replace(binding, feedback_key_digest="f" * 64),
    ]
    assert all(not store.consume(request_id, other) for other in alternatives)
    assert store.consume(request_id, binding)


def test_expired_pending_and_approved_requests_fail_closed(tmp_path, monkeypatch):
    current = [1000.0]
    monkeypatch.setattr(auth.time, "time", lambda: current[0])
    store = auth.PrReviewAuthorizationStore(tmp_path / "state.sqlite")
    pending = _binding()
    pending_id = store.request(pending)
    approved = replace(pending, run_id=str(uuid.uuid4()), feedback_round=2)
    approved_id = store.request(approved)
    assert store.decide(approved_id, store._binding_digest(approved), approved=True,
                        principal="human:approver")
    current[0] += 3601
    assert not store.decide(pending_id, store._binding_digest(pending), approved=True,
                            principal="human:approver")
    assert not store.consume(approved_id, approved)
    assert store.request(pending) is None
    assert store.request(approved) is None
    assert store.public_pending() == []


def test_parallel_consumers_have_one_winner_after_restart(tmp_path):
    db = tmp_path / "state.sqlite"
    binding = _binding()
    first = auth.PrReviewAuthorizationStore(db)
    request_id = first.request(binding)
    assert first.decide(request_id, first._binding_digest(binding), approved=True,
                        principal="human:approver")
    workers = [auth.PrReviewAuthorizationStore(db) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda worker: worker.consume(request_id, binding), workers))
    assert results.count(True) == 1
    assert results.count(False) == 7
    assert not auth.PrReviewAuthorizationStore(db).consume(request_id, binding)


def test_parallel_requests_share_one_id(tmp_path):
    db = tmp_path / "state.sqlite"
    binding = _binding()
    workers = [auth.PrReviewAuthorizationStore(db) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        request_ids = list(pool.map(lambda worker: worker.request(binding), workers))
    assert len(set(request_ids)) == 1
    assert len(workers[0].public_pending()) == 1


def test_public_projection_omits_tampered_binding(tmp_path):
    db = tmp_path / "state.sqlite"
    store = auth.PrReviewAuthorizationStore(db)
    request_id = store.request(_binding())
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pr_review_authorizations SET binding_json=? WHERE id=?",
            ('{"path":"secret"}', request_id),
        )
    assert store.public_pending() == []


def test_tampered_binding_cannot_be_decided_or_consumed(tmp_path):
    db = tmp_path / "state.sqlite"
    store = auth.PrReviewAuthorizationStore(db)
    binding = _binding()
    request_id = store.request(binding)
    digest = store._binding_digest(binding)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pr_review_authorizations SET binding_json=? WHERE id=?",
            ('{"path":"secret"}', request_id),
        )
    assert not store.decide(request_id, digest, approved=True, principal="human:approver")
    assert store.request(binding) is None
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pr_review_authorizations SET state='approved', "
            "approver_principal='human:approver' WHERE id=?", (request_id,),
        )
    assert not store.consume(request_id, binding)


def test_public_projection_omits_non_numeric_expiry(tmp_path):
    db = tmp_path / "state.sqlite"
    store = auth.PrReviewAuthorizationStore(db)
    request_id = store.request(_binding())
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pr_review_authorizations SET expires_at='not-a-time' WHERE id=?",
            (request_id,),
        )
    assert store.public_pending() == []


def test_in_memory_database_is_not_a_durable_approval_store():
    with pytest.raises(ValueError):
        auth.PrReviewAuthorizationStore(":memory:")
