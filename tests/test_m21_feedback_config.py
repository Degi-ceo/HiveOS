"""PR feedback write-back is explicitly opt-in and fail-closed at startup."""

from dataclasses import replace

import pytest

from hive.core.config import HiveConfig
from hive.runtime import HiveOS


def test_feedback_writeback_is_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("HIVE_PR_FEEDBACK_ENABLED", raising=False)
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert cfg.pr_feedback_enabled is False
    assert cfg.pr_feedback_timeout_sec == 7200.0
    assert cfg.to_safe_dict()["pr_feedback_enabled"] is False
    assert cfg.to_safe_dict()["pr_feedback_timeout_sec"] == 7200.0


def test_feedback_timeout_is_configurable_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_PR_FEEDBACK_TIMEOUT_SEC", "5400")
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert cfg.pr_feedback_timeout_sec == 5400.0
    for invalid in (0.0, 900.0, 21601.0, float("nan"), float("inf")):
        assert any("HIVE_PR_FEEDBACK_TIMEOUT_SEC" in issue for issue in replace(
            cfg, pr_feedback_timeout_sec=invalid,
        ).validate())


def test_review_author_ids_are_explicit_numeric_and_not_exposed(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_PR_REVIEWER_IDS", "1234, 5678")
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert cfg.pr_reviewer_ids == frozenset({"1234", "5678"})
    safe = cfg.to_safe_dict()
    assert safe["pr_reviewer_count"] == 2
    assert "pr_reviewer_ids" not in safe
    for invalid in (frozenset({"owner"}), frozenset({"0"}), frozenset({"-1"})):
        assert any("HIVE_PR_REVIEWER_IDS" in issue for issue in replace(
            cfg, pr_reviewer_ids=invalid,
        ).validate())
    with pytest.raises(RuntimeError, match="HIVE_PR_REVIEWER_IDS"):
        HiveOS.build(replace(
            cfg, pr_feedback_enabled=True, pr_reviewer_ids=frozenset({"owner"}),
        ), validate_inbound_channels=False)


def test_feedback_writeback_env_flag_requires_autonomous_selfmod(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_PR_FEEDBACK_ENABLED", "true")
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert cfg.pr_feedback_enabled is True
    assert any("HIVE_PR_FEEDBACK_ENABLED" in issue for issue in cfg.validate())
    with pytest.raises(RuntimeError, match="HIVE_PR_FEEDBACK_ENABLED"):
        HiveOS.build(cfg, validate_inbound_channels=False)


def test_feedback_writeback_requires_sandbox_and_github_identity(tmp_path):
    base = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    cfg = replace(base, pr_feedback_enabled=True, autonomous_selfmod_enabled=True)
    with pytest.raises(RuntimeError, match="HIVE_PR_FEEDBACK_ENABLED"):
        HiveOS.build(cfg, validate_inbound_channels=False)
    cfg = replace(cfg, sandbox_image="sandbox@sha256:" + "a" * 64)
    with pytest.raises(RuntimeError, match="HIVE_PR_FEEDBACK_ENABLED"):
        HiveOS.build(cfg, validate_inbound_channels=False)


def test_feedback_writeback_requires_real_candidate_evaluation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "hive.core.worker_isolation.worker_isolation_capability",
        lambda: SimpleNamespace(available=True),
    )
    base = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    cfg = replace(
        base, pr_feedback_enabled=True, autonomy_enabled=True,
        autonomous_selfmod_enabled=True, sandbox_image="sandbox@sha256:" + "a" * 64,
        github_token="test-token", github_owner="owner", github_repo="repo",
        approver_key="test-approver", learning_loop_enabled=False,
        worker_isolation="required",
    )
    assert any("HIVE_PR_FEEDBACK_ENABLED" in issue for issue in cfg.validate())
    with pytest.raises(RuntimeError, match="HIVE_PR_FEEDBACK_ENABLED"):
        HiveOS.build(cfg, validate_inbound_channels=False)
