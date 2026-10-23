"""Approver-only preview of an exact retained PR repair candidate."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from hive.core.config import HiveConfig
from hive.gateway.app import create_app
from hive.observability.supervised_review_preview import (
    SupervisedReviewPreview,
    _scan_displayed_diff,
)
from hive.runtime import HiveOS
from tests.test_m41_supervised_review_prepare import _git, _run
from tests.test_m42_supervised_writer import _approved_candidate


def test_preview_scans_removed_and_context_lines_for_secrets():
    fake_token = "ghp_" + "A" * 25
    header = "diff --git a/tests/example.py b/tests/example.py\n--- a/tests/example.py\n+++ b/tests/example.py\n@@ -1,2 +1,2 @@\n"
    assert _scan_displayed_diff(header + f"-key = '{fake_token}'\n+key = 'safe'\n")
    assert _scan_displayed_diff(header + f" key = '{fake_token}'\n+key = 'safe'\n")
    assert not _scan_displayed_diff(header + "-value = 1\n+value = 2\n")


def test_pending_candidate_preview_uses_exact_retained_git_commit(tmp_path):
    repo, db, store, link = _approved_candidate(tmp_path, approve=False)
    previewer = SupervisedReviewPreview(repo_root=repo, state_db=db, git_run=_run)

    preview = asyncio.run(previewer.render(link.request_id, store))

    assert preview is not None
    assert preview["request_id"] == link.request_id
    assert preview["binding_digest"] == store._binding_digest(link.binding)
    assert preview["candidate_commit"] == link.candidate_commit
    assert preview["head_sha"] == link.binding.expected_head
    assert preview["path"] == link.binding.path
    assert "-value = 1" in preview["diff"]
    assert "+value = 2" in preview["diff"]
    assert store.approved_supervised_candidate(link.request_id) is None
    assert _git(repo, "rev-parse", "HEAD") == link.candidate_commit


@pytest.mark.parametrize("mutation", ["unlink", "unattest", "expire", "finish_round"])
def test_preview_fails_closed_when_durable_proof_changes(tmp_path, mutation):
    repo, db, store, link = _approved_candidate(tmp_path, approve=False)
    previewer = SupervisedReviewPreview(repo_root=repo, state_db=db, git_run=_run)
    assert asyncio.run(previewer.render(link.request_id, store)) is not None
    with sqlite3.connect(db) as conn:
        if mutation == "unlink":
            conn.execute("DELETE FROM pr_review_host_evidence")
        elif mutation == "unattest":
            conn.execute("DELETE FROM pr_review_policy_evidence")
        elif mutation == "expire":
            conn.execute("UPDATE pr_review_authorizations SET expires_at=0")
        else:
            conn.execute("UPDATE selfmod_pr_feedback_rounds SET state='failed'")
    assert asyncio.run(previewer.render(link.request_id, store)) is None


def test_preview_rejects_retained_ref_drift(tmp_path):
    repo, db, store, link = _approved_candidate(tmp_path, approve=False)
    previewer = SupervisedReviewPreview(repo_root=repo, state_db=db, git_run=_run)
    ref = "refs/hive/pr-review-candidates/" + store._binding_digest(link.binding)
    _git(repo, "update-ref", ref, link.binding.expected_head)
    assert asyncio.run(previewer.render(link.request_id, store)) is None


def test_gateway_preview_requires_approver_credential(tmp_path):
    repo, db, _store, link = _approved_candidate(tmp_path, approve=False)
    config = replace(
        HiveConfig.from_env(root=repo, load_dotenv=False),
        state_db=db, approver_key="placeholder", secret="example",
        autonomy_enabled=False, production_mode=False, host="127.0.0.1",
    )
    hive = HiveOS.build(config)
    try:
        with TestClient(create_app(hive)) as client:
            path = f"/pr-reviews/{link.request_id}/preview"
            assert client.get(path, headers={"X-Hive-Token": "example"}).status_code == 401
            response = client.get(path, headers={"X-Hive-Token": "placeholder"})
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["candidate_commit"] == link.candidate_commit
            assert "+value = 2" in response.json()["diff"]
    finally:
        asyncio.run(hive.aclose())
