"""Managed release startup must keep host identity across selected commits."""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import hive.runtime as runtime_module
from hive.core.config import HiveConfig
from hive.core.deployment_identity import (
    claim_runtime_deployment_identity,
    deployment_host_key,
    runtime_deployment_host_key,
)
from hive.core.deployment_ledger import DEGRADED, DeployLedger
from hive.core.deployment_recovery import DeploymentRecoveryLedger
from hive.core.installed_release import InstalledReleaseSelector
from hive.runtime import HiveOS


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _managed(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "core.autocrlf", "false")
    _git(source, "config", "user.name", "Hive test")
    _git(source, "config", "user.email", "hive@example.invalid")
    code = source / "src" / "hive" / "runtime.py"
    code.parent.mkdir(parents=True)
    code.write_text("VERSION = 1\n", encoding="utf-8")
    _git(source, "add", "src")
    _git(source, "commit", "-qm", "first release")
    first = _git(source, "rev-parse", "HEAD")
    code.write_text("VERSION = 2\n", encoding="utf-8")
    _git(source, "commit", "-qam", "second release")
    second = _git(source, "rev-parse", "HEAD")
    catalog = tmp_path / "installed"
    (catalog / "releases").mkdir(parents=True)
    for sha in (first, second):
        _git(source, "worktree", "add", "--detach", "-q", str(catalog / "releases" / sha), sha)
    state_db = tmp_path / "state.sqlite"
    host_key = deployment_host_key(catalog)
    selector = InstalledReleaseSelector(catalog, DeployLedger(state_db),
                                        host_key=host_key, scope="user")
    config = replace(
        HiveConfig.from_env(root=catalog / "releases" / first, load_dotenv=False),
        data_dir=tmp_path / "data", state_db=state_db,
        mnemosyne_home=tmp_path / "mnemosyne", obsidian_vault=tmp_path / "vault",
        autonomy_enabled=True, approver_key="test-approver",
        worker_isolation="required", deploy_verify_enabled=True,
        deploy_systemctl_scope="user", installed_release_root=catalog,
    )
    return catalog, selector, first, second, config


def test_legacy_key_format_is_preserved(tmp_path):
    expected = hashlib.sha256(f"node\0{tmp_path.resolve()}".encode()).hexdigest()[:32]
    assert deployment_host_key(tmp_path, hostname="node") == expected
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert runtime_deployment_host_key(config, loaded_root=tmp_path / "unrelated") == (
        deployment_host_key(tmp_path)
    )


def test_selected_release_keeps_one_host_key_across_versions(tmp_path):
    catalog, selector, first, second, config = _managed(tmp_path)
    first_root = catalog / "releases" / first
    second_root = catalog / "releases" / second
    selector.select_candidate(first, expected_selected=None)
    key_one = runtime_deployment_host_key(config, loaded_root=first_root)
    claim_runtime_deployment_identity(config, loaded_root=first_root, expected_key=key_one)
    assert key_one == deployment_host_key(catalog)
    selector.select_candidate(second, expected_selected=first)
    key_two = runtime_deployment_host_key(replace(config, root=second_root), loaded_root=second_root)
    assert key_two == key_one
    restarted = InstalledReleaseSelector(catalog, DeployLedger(config.state_db),
                                         host_key=key_two, scope="user")
    assert restarted.selected_sha() == second


def test_wrong_loaded_or_selected_source_refuses_start(tmp_path, monkeypatch):
    catalog, selector, first, second, config = _managed(tmp_path)
    selector.select_candidate(first, expected_selected=None)
    with pytest.raises(RuntimeError, match="running source"):
        runtime_deployment_host_key(config, loaded_root=catalog / "releases" / second)
    selector.select_candidate(second, expected_selected=first)
    with pytest.raises(RuntimeError, match="selected release"):
        runtime_deployment_host_key(config, loaded_root=config.root)
    monkeypatch.setattr(runtime_module, "__file__",
                        str(config.root / "src" / "hive" / "runtime.py"))
    with pytest.raises(RuntimeError, match="selected release"):
        HiveOS.build(config)


def test_missing_or_dirty_selection_refuses_start(tmp_path):
    catalog, selector, first, _second, config = _managed(tmp_path)
    with pytest.raises(RuntimeError, match="selection unavailable"):
        runtime_deployment_host_key(config, loaded_root=config.root)
    selector.select_candidate(first, expected_selected=None)
    (catalog / "releases" / first / "src" / "hive" / "runtime.py").write_text(
        "VERSION = 3\n", encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="selection unavailable"):
        runtime_deployment_host_key(config, loaded_root=config.root)


def test_fence_blocks_late_legacy_deploy_and_recovery_writes(tmp_path):
    catalog, selector, first, _second, config = _managed(tmp_path)
    selector.select_candidate(first, expected_selected=None)
    legacy_key = deployment_host_key(config.root)
    ledger = DeployLedger(config.state_db)
    recovery_ledger = DeploymentRecoveryLedger(config.state_db)
    previous = ledger.schedule("legacy-failed", "gateway", "systemctl", legacy_key,
                               first, now=100)
    claim = ledger.claim_due(legacy_key, "test-verifier", now=200)
    assert claim is not None
    ledger.finish(previous.id, "test-verifier", DEGRADED,
                  failed_signals=("gateway",), now=201, claim_count=claim.claim_count)
    new_key = runtime_deployment_host_key(config, loaded_root=config.root)
    claim_runtime_deployment_identity(config, loaded_root=config.root, expected_key=new_key)
    assert new_key == deployment_host_key(catalog)
    with pytest.raises(sqlite3.DatabaseError, match="identity fenced"):
        ledger.schedule("late-legacy", "gateway", "systemctl",
                        legacy_key, first, now=300)
    with pytest.raises(sqlite3.DatabaseError, match="identity fenced"):
        recovery_ledger.begin(ledger.get(previous.id))
    with pytest.raises(RuntimeError, match="legacy deployment identity is fenced"):
        runtime_deployment_host_key(replace(config, installed_release_root=None),
                                    loaded_root=config.root)
    current = DeployLedger(config.state_db).schedule(
        "new-identity", "gateway", "systemctl", new_key, first, now=300,
    )
    assert current.host_key == new_key


def test_foreign_inflight_deployment_or_recovery_refuses_identity_switch(tmp_path):
    _catalog, selector, first, _second, config = _managed(tmp_path)
    selector.select_candidate(first, expected_selected=None)
    legacy_key = deployment_host_key(config.root)
    ledger = DeployLedger(config.state_db)
    receipt = ledger.schedule("legacy-run", "gateway", "systemctl", legacy_key,
                              first, now=100)
    with pytest.raises(RuntimeError, match="identity handoff unavailable"):
        claim_runtime_deployment_identity(
            config, loaded_root=config.root, expected_key=deployment_host_key(_catalog),
        )
    claim = ledger.claim_due(legacy_key, "test-verifier", now=200)
    assert claim is not None
    ledger.finish(receipt.id, "test-verifier", DEGRADED,
                  failed_signals=("gateway",), now=201, claim_count=claim.claim_count)
    DeploymentRecoveryLedger(config.state_db).begin(ledger.get(receipt.id))
    with pytest.raises(RuntimeError, match="identity handoff unavailable"):
        claim_runtime_deployment_identity(
            config, loaded_root=config.root, expected_key=deployment_host_key(_catalog),
        )


def test_rejected_runtime_build_does_not_fence_legacy_identity(tmp_path, monkeypatch):
    catalog, selector, first, _second, config = _managed(tmp_path)
    selector.select_candidate(first, expected_selected=None)
    monkeypatch.setattr(runtime_module, "__file__",
                        str(catalog / "releases" / first / "src" / "hive" / "runtime.py"))
    with pytest.raises(RuntimeError, match="HIVE_DEPLOY_VERIFY_ENABLED requires"):
        HiveOS.build(replace(config, autonomy_enabled=False), router=_Router())
    legacy = replace(config, installed_release_root=None)
    assert runtime_deployment_host_key(legacy, loaded_root=config.root) == (
        deployment_host_key(config.root)
    )


@pytest.mark.parametrize("field", ["data_dir", "state_db", "mnemosyne_home", "obsidian_vault"])
def test_mutable_data_inside_catalog_refuses_start(tmp_path, field):
    catalog, selector, first, _second, config = _managed(tmp_path)
    selector.select_candidate(first, expected_selected=None)
    config = replace(config, **{field: catalog / "mutable"})
    with pytest.raises(RuntimeError, match="external mutable data"):
        runtime_deployment_host_key(config, loaded_root=config.root)


def test_catalog_nested_under_mutable_data_refuses_start(tmp_path):
    catalog, selector, first, _second, config = _managed(tmp_path)
    selector.select_candidate(first, expected_selected=None)
    with pytest.raises(RuntimeError, match="external mutable data"):
        runtime_deployment_host_key(
            replace(config, data_dir=catalog.parent), loaded_root=config.root,
        )


def test_symlink_in_external_vault_cannot_write_back_into_release(tmp_path):
    catalog, selector, first, _second, config = _managed(tmp_path)
    selector.select_candidate(first, expected_selected=None)
    config.obsidian_vault.mkdir()
    link = config.obsidian_vault / "facts"
    try:
        link.symlink_to(catalog / "releases" / first, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable to this test account")
    with pytest.raises(RuntimeError, match="mutable tree is unsafe"):
        runtime_deployment_host_key(config, loaded_root=config.root)


def test_managed_mode_requires_verification_and_disables_selfmod(tmp_path):
    _catalog, _selector, _first, _second, config = _managed(tmp_path)
    with pytest.raises(RuntimeError, match="HIVE_DEPLOY_VERIFY_ENABLED"):
        runtime_deployment_host_key(replace(config, deploy_verify_enabled=False),
                                    loaded_root=config.root)
    with pytest.raises(RuntimeError, match="self-modification"):
        runtime_deployment_host_key(replace(config, autonomous_selfmod_enabled=True),
                                    loaded_root=config.root)


def test_managed_mode_env_loads_without_exposing_catalog_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_INSTALLED_RELEASE_ROOT", str(tmp_path / "installed"))
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert config.installed_release_root == tmp_path / "installed"
    assert config.to_safe_dict()["installed_release_mode"] is True
    assert str(tmp_path / "installed") not in str(config.to_safe_dict())


class _Router:
    async def aclose(self):
        return None


def test_runtime_wires_stable_host_key_for_selected_release(tmp_path, monkeypatch):
    catalog, selector, first, _second, config = _managed(tmp_path)
    selector.select_candidate(first, expected_selected=None)
    monkeypatch.setattr(runtime_module, "__file__",
                        str(catalog / "releases" / first / "src" / "hive" / "runtime.py"))
    hive = HiveOS.build(config, router=_Router())
    assert hive.tools["deploy"]._deploy_host_key == deployment_host_key(catalog)


def test_runtime_rechecks_host_credential_on_build_and_claim(tmp_path, monkeypatch):
    catalog, selector, first, second, config = _managed(tmp_path)
    selector.select_candidate(first, expected_selected=None)
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    selected = credentials / "selected_release"
    selected.write_bytes(first.encode("ascii") + b"\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    config = replace(config, installed_release_credential="selected_release")
    monkeypatch.setattr(runtime_module, "__file__",
                        str(catalog / "releases" / first / "src" / "hive" / "runtime.py"))
    hive = HiveOS.build(config, router=_Router())
    assert hive.tools["deploy"]._deploy_host_key == deployment_host_key(catalog)
    selected.write_bytes(second.encode("ascii") + b"\n")
    with pytest.raises(RuntimeError, match="host-selected release"):
        runtime_deployment_host_key(config, loaded_root=config.root)


def test_host_credential_requires_managed_release(tmp_path):
    config = replace(HiveConfig.from_env(root=tmp_path, load_dotenv=False),
                     installed_release_credential="selected_release")
    with pytest.raises(RuntimeError, match="requires HIVE_INSTALLED_RELEASE_ROOT"):
        runtime_deployment_host_key(config, loaded_root=tmp_path)
