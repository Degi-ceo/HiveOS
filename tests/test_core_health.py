"""Coverage for core/credentials.py (vault + env injection) and core/doctor.py
(health checks + idempotent migrations). Both were previously untested."""
from __future__ import annotations

import os
import stat

import pytest

from hive.core import config, credentials, doctor
from hive.core.config import HiveConfig


def _cfg(tmp_path) -> HiveConfig:
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    config.set_config(cfg)        # credentials/doctor read the global config
    cfg.ensure_dirs()
    # doctor checks cfg.root/Core/approval_gate.py; stage a stub so a tmp_path
    # root represents a healthy tree (SOUL_PATH is a module constant → real repo).
    gate = tmp_path / "Core" / "approval_gate.py"
    gate.parent.mkdir(parents=True, exist_ok=True)
    gate.write_text("# stub for doctor check\n", encoding="utf-8")
    return cfg


# ---------------------------------------------------------------------------
# credentials.py
# ---------------------------------------------------------------------------

def test_save_and_get_roundtrip(tmp_path):
    _cfg(tmp_path)
    credentials.save("MY_KEY", "secret-value")
    assert credentials.get("MY_KEY") == "secret-value"


def test_get_falls_back_to_env_then_default(tmp_path, monkeypatch):
    _cfg(tmp_path)
    monkeypatch.setenv("ENV_ONLY", "from-env")
    assert credentials.get("ENV_ONLY") == "from-env"
    assert credentials.get("ABSENT", "fallback") == "fallback"
    assert credentials.get("ABSENT") is None


def test_save_sets_owner_only_permissions(tmp_path):
    _cfg(tmp_path)
    credentials.save("K", "v")
    mode = stat.S_IMODE(os.stat(credentials._path()).st_mode)
    # 0o600 — owner read/write only (skip the assert on non-POSIX where chmod no-ops)
    if os.name == "posix":
        assert mode == 0o600


def test_load_returns_empty_on_corrupt_file(tmp_path):
    _cfg(tmp_path)
    credentials._path().write_text("{ this is not json", encoding="utf-8")
    # must not raise — corrupt vault degrades to empty, logged as a warning
    assert credentials._load() == {}
    assert credentials.get("ANYTHING", "d") == "d"


def test_inject_loads_without_overwriting_existing(tmp_path, monkeypatch):
    _cfg(tmp_path)
    credentials.save("NEW_VAR", "from-vault")
    credentials.save("EXISTING_VAR", "vault-version")
    monkeypatch.setenv("EXISTING_VAR", "env-wins")

    n = credentials.inject()
    assert os.environ["NEW_VAR"] == "from-vault"        # injected
    assert os.environ["EXISTING_VAR"] == "env-wins"     # NOT overwritten
    assert n == 1                                        # only the new var counted


def test_inject_noop_when_vault_absent(tmp_path):
    _cfg(tmp_path)
    assert credentials.inject() == 0


# ---------------------------------------------------------------------------
# doctor.py
# ---------------------------------------------------------------------------

def test_check_reports_soul_and_gate_present(tmp_path):
    _cfg(tmp_path)
    results = {name: ok for name, ok, _ in doctor.check()}
    assert results["SOUL.md present"] is True
    assert results["approval_gate present"] is True


def test_check_core_modules_importable(tmp_path):
    _cfg(tmp_path)
    results = {name: ok for name, ok, _ in doctor.check()}
    for mod in ("hive.core.registry", "hive.core.events", "hive.core.config"):
        assert results[f"import {mod}"] is True


def test_migrations_are_idempotent_with_fix(tmp_path):
    _cfg(tmp_path)
    first = doctor.check(fix=True)
    second = doctor.check(fix=True)          # re-run must not change outcomes
    assert [(n, ok) for n, ok, _ in first] == [(n, ok) for n, ok, _ in second]
    # after fix, the dir migrations report ok
    by_name = {n: ok for n, ok, _ in second}
    assert by_name["data dir present"] is True
    assert by_name["mnemosyne home dir"] is True


def test_fix_creates_missing_state_db_dir(tmp_path):
    cfg = _cfg(tmp_path)
    # check without fix reports state DB openable after dirs exist; force a fresh run
    results = {name: ok for name, ok, _ in doctor.check(fix=True)}
    assert results["state DB openable"] is True
    assert cfg.state_db.parent.is_dir()


def test_run_returns_true_when_only_warn_only_checks_fail(tmp_path, monkeypatch):
    # Set HIVE_SECRET before building config so it's included in the frozen dataclass.
    monkeypatch.setenv("HIVE_SECRET", "a-test-secret-not-default")
    _cfg(tmp_path)
    # MINIMAX_API_KEY is in _WARN_ONLY — its absence must NOT fail the doctor run.
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    assert doctor.run(fix=True) is True


def test_main_returns_zero_on_success(tmp_path, monkeypatch):
    # Set HIVE_SECRET before building config so it's included in the frozen dataclass.
    monkeypatch.setenv("HIVE_SECRET", "a-test-secret-not-default")
    _cfg(tmp_path)
    monkeypatch.setattr("sys.argv", ["hive-doctor", "--fix"])
    assert doctor.main() == 0


# ---------------------------------------------------------------------------
# Individual migration functions — direct unit tests
# ---------------------------------------------------------------------------

def test_m0_dirs_fix_creates_data_dir(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    config.set_config(cfg)
    # data_dir does NOT exist yet — fix=True must create it
    assert not cfg.data_dir.exists()
    name, ok, detail = doctor._m0_dirs(cfg, fix=True)
    assert name == "data dir present"
    assert ok is True
    assert cfg.data_dir.is_dir()


def test_m0_dirs_no_fix_reports_false_when_absent(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    config.set_config(cfg)
    assert not cfg.data_dir.exists()
    _, ok, _ = doctor._m0_dirs(cfg, fix=False)
    assert ok is False


def test_m0_dirs_idempotent(tmp_path):
    cfg = _cfg(tmp_path)
    name1, ok1, _ = doctor._m0_dirs(cfg, fix=True)
    name2, ok2, _ = doctor._m0_dirs(cfg, fix=True)
    assert ok1 is True and ok2 is True


def test_m1_state_db_schema_creates_db(tmp_path):
    cfg = _cfg(tmp_path)
    name, ok, detail = doctor._m1_state_db_schema(cfg, fix=True)
    assert name == "state DB openable"
    assert ok is True
    assert cfg.state_db.exists()


def test_m1_state_db_schema_fix_creates_missing_parent(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    config.set_config(cfg)
    # Parent dir doesn't exist yet — fix=True should create it
    assert not cfg.state_db.parent.exists()
    _, ok, _ = doctor._m1_state_db_schema(cfg, fix=True)
    assert ok is True
    assert cfg.state_db.parent.is_dir()


def test_m1_state_db_schema_no_fix_false_when_parent_missing(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    config.set_config(cfg)
    _, ok, _ = doctor._m1_state_db_schema(cfg, fix=False)
    assert ok is False


def test_m2_mnemosyne_home_fix_creates_dir(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    config.set_config(cfg)
    mne = tmp_path / "mne_new"
    import dataclasses
    cfg2 = dataclasses.replace(cfg, mnemosyne_home=mne)
    config.set_config(cfg2)
    assert not mne.exists()
    name, ok, _ = doctor._m2_mnemosyne_home(cfg2, fix=True)
    assert name == "mnemosyne home dir"
    assert ok is True
    assert mne.is_dir()


def test_m2_mnemosyne_home_no_fix_false_when_absent(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    config.set_config(cfg)
    import dataclasses
    cfg2 = dataclasses.replace(cfg, mnemosyne_home=tmp_path / "absent_mne")
    config.set_config(cfg2)
    _, ok, _ = doctor._m2_mnemosyne_home(cfg2, fix=False)
    assert ok is False


def test_m3_docker_skipped_when_no_sandbox_image(tmp_path):
    cfg = _cfg(tmp_path)
    # sandbox_image is "" by default — check should be skipped (ok=True)
    name, ok, detail = doctor._m3_docker(cfg, fix=False)
    assert "sandbox not configured" in name
    assert ok is True
    assert detail == "skipped"


def test_m3_docker_false_when_image_configured_but_docker_missing(tmp_path, monkeypatch):
    import dataclasses
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    config.set_config(cfg)
    cfg2 = dataclasses.replace(cfg, sandbox_image="alpine:latest")
    config.set_config(cfg2)
    # Pretend docker is not on PATH
    monkeypatch.setattr("shutil.which", lambda _: None)
    _, ok, _ = doctor._m3_docker(cfg2, fix=False)
    assert ok is False


def test_m4_shell_provider_valid_local(tmp_path):
    import dataclasses
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    config.set_config(cfg)
    cfg2 = dataclasses.replace(cfg, shell_provider="local")
    config.set_config(cfg2)
    _, ok, _ = doctor._m4_shell_provider(cfg2, fix=False)
    assert ok is True


def test_m4_shell_provider_bad_value_returns_false(tmp_path):
    import dataclasses
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    config.set_config(cfg)
    cfg2 = dataclasses.replace(cfg, shell_provider="ssh")
    config.set_config(cfg2)
    _, ok, detail = doctor._m4_shell_provider(cfg2, fix=False)
    assert ok is False
    assert "ssh" in detail


def test_m4_shell_provider_docker_missing_binary(tmp_path, monkeypatch):
    import dataclasses
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    config.set_config(cfg)
    cfg2 = dataclasses.replace(cfg, shell_provider="docker")
    config.set_config(cfg2)
    monkeypatch.setattr("shutil.which", lambda _: None)
    _, ok, detail = doctor._m4_shell_provider(cfg2, fix=False)
    assert ok is False
    assert "docker" in detail


def test_migration_recording(tmp_path):
    cfg = _cfg(tmp_path)
    doctor._ensure_migrations_table(cfg.state_db)
    doctor._record_migration(cfg.state_db, "_test_migration")
    applied = doctor._get_applied(cfg.state_db)
    assert "_test_migration" in applied


def test_record_migration_insert_or_ignore_idempotent(tmp_path):
    cfg = _cfg(tmp_path)
    doctor._ensure_migrations_table(cfg.state_db)
    doctor._record_migration(cfg.state_db, "_dup")
    doctor._record_migration(cfg.state_db, "_dup")  # must not raise
    applied = doctor._get_applied(cfg.state_db)
    assert "_dup" in applied
