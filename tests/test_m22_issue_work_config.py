"""Issue pickup is a separately gated autonomous capability."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hive.core.config import HiveConfig
from hive.runtime import HiveOS


def test_issue_work_is_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("HIVE_ISSUE_WORK_ENABLED", raising=False)
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert cfg.issue_work_enabled is False
    assert cfg.issue_work_max_inflight == 1
    assert cfg.issue_work_scan_interval_sec == 3600
    safe = cfg.to_safe_dict()
    assert safe["issue_work_enabled"] is False
    assert safe["issue_work_max_inflight"] == 1


def test_issue_work_env_and_bounds(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_ISSUE_WORK_ENABLED", "true")
    monkeypatch.setenv("HIVE_ISSUE_WORK_MAX_INFLIGHT", "3")
    monkeypatch.setenv("HIVE_ISSUE_WORK_SCAN_INTERVAL_SEC", "600")
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert (cfg.issue_work_enabled, cfg.issue_work_max_inflight,
            cfg.issue_work_scan_interval_sec) == (True, 3, 600.0)
    for invalid in (0, 5, True):
        assert any("HIVE_ISSUE_WORK_MAX_INFLIGHT" in problem for problem in replace(
            cfg, issue_work_max_inflight=invalid,
        ).validate())
    for invalid in (0, 59, 86401, float("nan"), float("inf")):
        assert any("HIVE_ISSUE_WORK_SCAN_INTERVAL_SEC" in problem for problem in replace(
            cfg, issue_work_scan_interval_sec=invalid,
        ).validate())


def test_enabled_issue_work_requires_autonomy_sandbox_learning_and_github(tmp_path):
    base = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    cfg = replace(base, issue_work_enabled=True)
    assert any("HIVE_ISSUE_WORK_ENABLED" in problem for problem in cfg.validate())
    with pytest.raises(RuntimeError, match="HIVE_ISSUE_WORK_ENABLED"):
        HiveOS.build(cfg, validate_inbound_channels=False)


def test_enabled_issue_work_rejects_missing_learning_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hive.core.worker_isolation.worker_isolation_capability",
        lambda: SimpleNamespace(available=True),
    )
    base = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    cfg = replace(
        base, issue_work_enabled=True, autonomy_enabled=True,
        autonomous_selfmod_enabled=True, sandbox_image="sandbox@sha256:" + "a" * 64,
        github_token="test-token", github_owner="owner", github_repo="repo",
        approver_key="test-approver", learning_loop_enabled=False,
        worker_isolation="required",
    )
    with pytest.raises(RuntimeError, match="HIVE_ISSUE_WORK_ENABLED"):
        HiveOS.build(cfg, validate_inbound_channels=False)


def test_enabled_issue_work_requires_digest_pinned_image(tmp_path):
    base = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    cfg = replace(
        base, issue_work_enabled=True, autonomy_enabled=True,
        autonomous_selfmod_enabled=True, sandbox_image="sandbox:latest",
        github_token="test-token", github_owner="owner", github_repo="repo",
        approver_key="test-approver", learning_loop_enabled=True,
        worker_isolation="required",
    )
    assert any("HIVE_ISSUE_WORK_ENABLED" in problem for problem in cfg.validate())


def test_issue_work_requires_sufficient_stall_lease(tmp_path):
    base = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    cfg = replace(base, issue_work_enabled=True, task_stall_timeout_sec=0.1)
    assert any("HIVE_TASK_STALL_TIMEOUT_SEC" in problem for problem in cfg.validate())


def test_enabled_issue_work_builds_reader_without_scanning_network(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hive.core.worker_isolation.worker_isolation_capability",
        lambda: SimpleNamespace(available=True),
    )
    base = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    cfg = replace(
        base, issue_work_enabled=True, autonomy_enabled=True,
        autonomous_selfmod_enabled=True, sandbox_image="sandbox@sha256:" + "a" * 64,
        github_token="test-token", github_owner="owner", github_repo="repo",
        approver_key="test-approver", learning_loop_enabled=True,
        worker_isolation="required",
    )
    hive = HiveOS.build(cfg, router=MagicMock(), validate_inbound_channels=False)
    assert hive.issue_work_reader is not None
    assert hive.issue_work_reader.owner == "owner"
    assert hive.issue_work_reader.repo == "repo"
