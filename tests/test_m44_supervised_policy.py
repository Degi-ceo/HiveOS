"""Manual-tier policy evidence for retained source/test PR candidates."""

from __future__ import annotations

import asyncio
import sqlite3
import uuid

import pytest

from hive.core.pr_review_auth import PrReviewAuthorizationStore
from hive.core.self_mod import SelfModifier
from hive.core.supervised_review_policy import (
    SUPERVISED_POLICY_VERSION,
    qualify_supervised_review_candidate,
)
from tests.test_m35_pr_review_prepare import BRANCH, COMMIT, HEAD, PATH, TREE, FakeGit, _context
from tests.test_self_mod import _apply_ok, _runner


@pytest.mark.parametrize("paths", [
    ["docs/readme.md"],
    ["src/hive/repair.py", "tests/test_repair.py"],
    ["src/hive/core/learning/loop.py"],
    ["tests/conftest.py"],
    ["Config/SOUL.md"],
])
def test_supervised_policy_rejects_non_single_code_candidate(tmp_path, paths):
    result = asyncio.run(qualify_supervised_review_candidate(
        str(tmp_path), HEAD, paths, str(uuid.uuid4()), "a" * 64,
    ))
    assert result["ok"] is False


def test_supervised_policy_accepts_only_manual_review_without_claiming_runtime_eval(tmp_path):
    digest = "a" * 64
    result = asyncio.run(qualify_supervised_review_candidate(
        str(tmp_path), HEAD, [PATH], str(uuid.uuid4()), digest,
    ))
    assert result == {
        "ok": True,
        "candidate_digest": digest,
        "required_tier": "review",
        "policy_version": SUPERVISED_POLICY_VERSION,
    }


def test_supervised_policy_prepares_exact_candidate_without_push_or_approval(tmp_path):
    git = FakeGit()
    run_id = str(uuid.uuid4())
    context = _context(run_id)
    store = PrReviewAuthorizationStore(tmp_path / "state.db")

    async def verify(branch, head):
        return {"ok": True, "branch": branch, "head_sha": head}

    async def repair(_failure):
        async def apply(_worktree):
            return [PATH]
        return apply

    modifier = SelfModifier(repo_root=str(tmp_path), run=git, test_cmd="pytest")
    result = asyncio.run(modifier.prepare_existing_pr_review_candidate(
        BRANCH, HEAD, verify, repair, title="repair", run_id=run_id,
        review_context=context, authorizations=store,
        candidate_gate=qualify_supervised_review_candidate,
    ))

    assert result["ok"] is True and result["stage"] == "prepared"
    assert git.pushes == 0
    assert store.public_pending() == []
    assert store.policy_checked_candidate(context.bind_candidate(PATH, TREE)) == {
        "candidate_commit": result["head_sha"],
        "candidate_parent": HEAD,
    }
    with sqlite3.connect(tmp_path / "state.db") as conn:
        versions = conn.execute(
            "SELECT policy_version FROM pr_review_policy_evidence"
        ).fetchall()
    assert versions == [(SUPERVISED_POLICY_VERSION,)]


def test_supervised_policy_version_survives_restart_and_unknown_version_fails_closed(tmp_path):
    db = tmp_path / "state.db"
    binding = _context(str(uuid.uuid4())).bind_candidate(PATH, TREE)
    store = PrReviewAuthorizationStore(db)
    assert store.record_policy_checked_candidate(
        binding, candidate_commit=COMMIT, candidate_parent=HEAD,
        policy_version=SUPERVISED_POLICY_VERSION,
    )
    assert PrReviewAuthorizationStore(db).policy_checked_candidate(binding) == {
        "candidate_commit": COMMIT, "candidate_parent": HEAD,
    }
    assert not store.record_policy_checked_candidate(
        binding, candidate_commit=COMMIT, candidate_parent=HEAD,
        policy_version="unrecognized-policy",
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE pr_review_policy_evidence SET policy_version='unrecognized-policy'"
        )
    assert PrReviewAuthorizationStore(db).policy_checked_candidate(binding) is None


def test_legacy_attestation_remains_readable_but_cannot_be_relabelled(tmp_path):
    store = PrReviewAuthorizationStore(tmp_path / "state.db")
    binding = _context(str(uuid.uuid4())).bind_candidate(PATH, TREE)
    assert store.record_policy_checked_candidate(
        binding, candidate_commit=COMMIT, candidate_parent=HEAD,
    )
    assert store.policy_checked_candidate(binding) is not None
    assert not store.record_policy_checked_candidate(
        binding, candidate_commit=COMMIT, candidate_parent=HEAD,
        policy_version=SUPERVISED_POLICY_VERSION,
    )


def test_manual_tier_result_never_publishes_through_ordinary_self_mod():
    run = _runner()

    async def manual_gate(_worktree, _base, _paths, _run_id, digest):
        return {
            "ok": True, "candidate_digest": digest,
            "required_tier": "review", "policy_version": SUPERVISED_POLICY_VERSION,
        }

    modifier = SelfModifier(repo_root="/tmp/x", run=run)
    result = asyncio.run(modifier.propose(
        "candidate", "manual review only", _apply_ok, candidate_gate=manual_gate,
    ))
    assert result["ok"] is False and result["stage"] == "review_required"
    assert not any(command.startswith("git commit") for command in run.calls)
    assert not any(command.startswith("git push") for command in run.calls)
