"""Durable supervised feedback transitions across a runtime restart."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3

import pytest

from hive.core.pr_review_auth import PrReviewAuthorizationStore
from hive.core.supervised_review_policy import SUPERVISED_POLICY_VERSION
from hive.core.supervised_review_prepare import SupervisedReviewEvidencePreparer
from tests.test_m41_supervised_review_prepare import (
    BRANCH,
    PATH,
    URL,
    _Runner,
    _binding,
    _git,
    _run,
)


def _prepared(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "hive@example.invalid"),
        ("config", "user.name", "Hive M45 Test"),
    ):
        _git(repo, *args)
    source = repo / PATH
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", PATH)
    _git(repo, "commit", "-qm", "base")
    source.write_text("value = 2\n", encoding="utf-8")
    _git(repo, "commit", "-am", "candidate", "-q")
    db = tmp_path / "state.sqlite"
    binding, store = _binding(repo, db)
    candidate = _git(repo, "rev-parse", "HEAD")
    retained_ref = "refs/hive/pr-review-candidates/" + hashlib.sha256(
        binding.canonical_json().encode("utf-8")
    ).hexdigest()
    _git(repo, "update-ref", retained_ref, candidate)
    assert store.record_policy_checked_candidate(
        binding, candidate_commit=candidate, candidate_parent=binding.expected_head,
        policy_version=SUPERVISED_POLICY_VERSION,
    )

    async def verify(branch, head):
        return {"ok": branch == BRANCH, "branch": branch, "head_sha": head}

    preparer = SupervisedReviewEvidencePreparer(
        repo_root=repo, state_db=db, runner=_Runner(), git_run=_run,
    )
    link = asyncio.run(preparer.prepare(binding, store, verify))
    assert link is not None
    return db, binding, link


def test_supervised_round_survives_restart_and_out_of_band_decision(tmp_path):
    db, binding, link = _prepared(tmp_path)
    selector = dict(
        pr_url=URL, expected_head=binding.expected_head,
        feedback_round=1, run_id=binding.run_id,
    )
    assert PrReviewAuthorizationStore(db).supervised_round_status(**selector) == {
        "request_id": link.request_id, "state": "pending",
    }
    assert PrReviewAuthorizationStore(db).supervised_round_status(
        **{**selector, "run_id": "00000000-0000-4000-8000-000000000000"}
    ) is None
    store = PrReviewAuthorizationStore(db)
    digest = hashlib.sha256(binding.canonical_json().encode("utf-8")).hexdigest()
    assert store.decide(
        link.request_id, digest, approved=True, principal="human:approver",
    )
    assert PrReviewAuthorizationStore(db).supervised_round_status(**selector) == {
        "request_id": link.request_id, "state": "approved",
    }


def test_expired_or_tampered_round_cannot_be_resumed_as_pending(tmp_path):
    db, binding, link = _prepared(tmp_path)
    selector = dict(
        pr_url=URL, expected_head=binding.expected_head,
        feedback_round=1, run_id=binding.run_id,
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pr_review_authorizations SET expires_at=0 WHERE id=?",
            (link.request_id,),
        )
    assert PrReviewAuthorizationStore(db).supervised_round_status(**selector) == {
        "request_id": link.request_id, "state": "expired",
    }
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pr_review_host_evidence SET binding_digest='tampered' WHERE request_id=?",
            (link.request_id,),
        )
    assert PrReviewAuthorizationStore(db).supervised_round_status(**selector) is None


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("run_id", "00000000-0000-4000-8000-000000000000"),
        ("expected_sha", "0" * 40),
        ("feedback_key", "0" * 64),
        ("state", "failed"),
    ],
)
def test_status_requires_exact_live_feedback_reservation(tmp_path, column, value):
    db, binding, _link = _prepared(tmp_path)
    selector = dict(
        pr_url=URL, expected_head=binding.expected_head,
        feedback_round=1, run_id=binding.run_id,
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            f"UPDATE selfmod_pr_feedback_rounds SET {column}=? WHERE pr_url=? AND round=1",
            (value, URL),
        )
    assert PrReviewAuthorizationStore(db).supervised_round_status(**selector) is None
