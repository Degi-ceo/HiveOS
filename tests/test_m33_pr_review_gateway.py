"""Authenticated gateway decision boundary for dormant PR REVIEW receipts."""

from __future__ import annotations

import dataclasses
import hashlib
import uuid

import pytest
from starlette.testclient import TestClient

from hive.core.pr_review_auth import PrReviewBinding
from hive.gateway.app import create_app
from hive.runtime import HiveOS
from hive.core.config import HiveConfig


def _binding() -> PrReviewBinding:
    tree = "c" * 40
    return PrReviewBinding(
        owner="Degi-ceo", repo="HiveOS", pr_number=123,
        pr_url="https://github.com/Degi-ceo/HiveOS/pull/123",
        pr_id=234, author_id=345, head_repo_id=456, base_repo_id=456,
        branch="hive/auto-" + "a" * 32, base_ref="main",
        expected_head="b" * 40, path="src/hive/core/example.py",
        operation="PATCH_CODE", candidate_tree=tree,
        candidate_digest=hashlib.sha256(f"git-tree\0{tree}".encode()).hexdigest(),
        run_id=str(uuid.uuid4()), feedback_round=1,
        feedback_key_digest="e" * 64,
    )


def _hive(tmp_path, *, approver_key: str = "") -> HiveOS:
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    config = dataclasses.replace(config, approver_key=approver_key)
    return HiveOS.build(config)


def test_pr_review_decision_requires_strict_out_of_band_approver_key(tmp_path):
    hive = _hive(tmp_path, approver_key="approver-key")
    request_id = hive.pr_review_authorizations.request(_binding())
    pending = hive.pr_review_authorizations.public_pending()[0]

    with TestClient(create_app(hive)) as client:
        listed = client.get("/pr-reviews", headers={"X-Hive-Token": "change_me"})
        assert listed.status_code == 200
        assert listed.json()["pending"] == [pending]

        normal = client.post(
            f"/pr-reviews/{request_id}/decide",
            json={"binding_digest": pending["binding_digest"], "approved": True},
            headers={"X-Hive-Token": "change_me"},
        )
        assert normal.status_code == 401

        approved = client.post(
            f"/pr-reviews/{request_id}/decide",
            json={"binding_digest": pending["binding_digest"], "approved": True},
            headers={"X-Hive-Token": "approver-key"},
        )
        assert approved.status_code == 200
        assert approved.json() == {"request_id": request_id, "state": "approved"}


def test_pr_review_decision_never_falls_back_to_normal_secret(tmp_path):
    hive = _hive(tmp_path)
    request_id = hive.pr_review_authorizations.request(_binding())
    pending = hive.pr_review_authorizations.public_pending()[0]

    with TestClient(create_app(hive)) as client:
        response = client.post(
            f"/pr-reviews/{request_id}/decide",
            json={"binding_digest": pending["binding_digest"], "approved": True},
            headers={"X-Hive-Token": "change_me"},
        )
    assert response.status_code == 401
    assert hive.pr_review_authorizations.public_pending()[0]["id"] == request_id


@pytest.mark.parametrize("approved", [1, 0, "yes", "false"])
def test_pr_review_decision_rejects_coerced_boolean_values(tmp_path, approved):
    hive = _hive(tmp_path, approver_key="approver-key")
    request_id = hive.pr_review_authorizations.request(_binding())
    pending = hive.pr_review_authorizations.public_pending()[0]

    with TestClient(create_app(hive)) as client:
        response = client.post(
            f"/pr-reviews/{request_id}/decide",
            json={"binding_digest": pending["binding_digest"], "approved": approved},
            headers={"X-Hive-Token": "approver-key"},
        )
    assert response.status_code == 422
    assert hive.pr_review_authorizations.public_pending()[0]["id"] == request_id


def test_pr_review_key_equal_to_normal_secret_fails_at_runtime_build(tmp_path):
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    config = dataclasses.replace(config, approver_key="same", secret="same")
    with pytest.raises(RuntimeError, match="HIVE_APPROVER_KEY must differ"):
        HiveOS.build(config)
