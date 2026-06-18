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


# ---------------------------------------------------------------------------
# HiveConfig.validate() edge cases
# ---------------------------------------------------------------------------

def _base_cfg(tmp_path) -> HiveConfig:
    """Return a config with known-valid, non-default values to prevent validate noise."""
    import dataclasses
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return dataclasses.replace(
        cfg,
        secret="a-strong-random-secret",
        minimax_api_key="test-key",
        exec_provider="minimax",
        shell_provider="local",
        max_iterations=30,
    )


def test_config_validate_passes_with_valid_defaults(tmp_path):
    """validate() returns an empty list when all fields are valid."""
    cfg = _base_cfg(tmp_path)
    issues = cfg.validate()
    assert issues == [], f"Expected no issues, got: {issues}"


def test_config_validate_fails_on_invalid_exec_provider(tmp_path):
    """exec_provider outside minimax/anthropic is reported as an issue."""
    import dataclasses
    cfg = dataclasses.replace(_base_cfg(tmp_path), exec_provider="invalid")
    issues = cfg.validate()
    assert any("HIVE_EXEC_PROVIDER" in i for i in issues), \
        f"Expected exec_provider issue, got: {issues}"


def test_config_validate_fails_on_invalid_shell_provider(tmp_path):
    """shell_provider outside local/docker is reported as an issue."""
    import dataclasses
    cfg = dataclasses.replace(_base_cfg(tmp_path), shell_provider="invalid")
    issues = cfg.validate()
    assert any("HIVE_SHELL_PROVIDER" in i for i in issues), \
        f"Expected shell_provider issue, got: {issues}"


def test_config_validate_fails_on_zero_max_iterations(tmp_path):
    """max_iterations=0 is below the minimum of 1, reported as an issue."""
    import dataclasses
    cfg = dataclasses.replace(_base_cfg(tmp_path), max_iterations=0)
    issues = cfg.validate()
    assert any("HIVE_MAX_ITERATIONS" in i for i in issues), \
        f"Expected max_iterations issue, got: {issues}"


def test_config_validate_minimax_key_required_when_provider_is_minimax(tmp_path):
    """exec_provider=minimax with empty minimax_api_key is reported as an issue."""
    import dataclasses
    cfg = dataclasses.replace(_base_cfg(tmp_path), exec_provider="minimax", minimax_api_key="")
    issues = cfg.validate()
    assert any("MINIMAX_API_KEY" in i for i in issues), \
        f"Expected MINIMAX_API_KEY issue, got: {issues}"


# ---------------------------------------------------------------------------
# Doctor migration additional tests
# ---------------------------------------------------------------------------

def test_doctor_m1_schema_creates_tables(tmp_path):
    """Running M1 on a fresh DB with fix=True opens the DB (WAL mode) successfully."""
    cfg = _cfg(tmp_path)
    # Run M1 directly, which should create the DB file
    name, ok, detail = doctor._m1_state_db_schema(cfg, fix=True)
    assert ok is True, f"M1 failed: {detail}"
    assert cfg.state_db.exists(), "state_db file was not created by M1"
    # Verify the DB is usable (WAL mode set means we can open and query it)
    import sqlite3
    conn = sqlite3.connect(str(cfg.state_db))
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode == "wal"


def test_doctor_m1_idempotent(tmp_path):
    """Running M1 twice does not raise and both calls return ok=True."""
    cfg = _cfg(tmp_path)
    _, ok1, detail1 = doctor._m1_state_db_schema(cfg, fix=True)
    _, ok2, detail2 = doctor._m1_state_db_schema(cfg, fix=True)
    assert ok1 is True, f"First M1 run failed: {detail1}"
    assert ok2 is True, f"Second M1 run failed: {detail2}"


def test_doctor_migrations_table_auto_created(tmp_path):
    """After _ensure_migrations_table(), querying schema_migrations succeeds."""
    cfg = _cfg(tmp_path)
    db_path = cfg.state_db
    doctor._ensure_migrations_table(db_path)
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    # If table doesn't exist this would raise; it must not.
    rows = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()
    conn.close()
    assert rows[0] == 0  # freshly created table has no entries


def test_doctor_record_and_get_applied(tmp_path):
    """After _record_migration(db, 'm1'), _get_applied(db) contains 'm1'."""
    cfg = _cfg(tmp_path)
    db_path = cfg.state_db
    doctor._ensure_migrations_table(db_path)
    doctor._record_migration(db_path, "m1")
    applied = doctor._get_applied(db_path)
    assert "m1" in applied, f"Expected 'm1' in applied migrations, got: {applied}"


def test_doctor_get_applied_empty_initially(tmp_path):
    """A fresh DB with schema_migrations table has no applied migrations."""
    cfg = _cfg(tmp_path)
    db_path = cfg.state_db
    doctor._ensure_migrations_table(db_path)
    applied = doctor._get_applied(db_path)
    assert applied == set(), f"Expected empty set, got: {applied}"


# ---------------------------------------------------------------------------
# Additional doctor / HiveConfig tests
# ---------------------------------------------------------------------------

def test_doctor_check_returns_list(tmp_path):
    """check() must return a list (of tuples) — never None or a dict."""
    cfg = _cfg(tmp_path)
    config.set_config(cfg)
    result = doctor.check(fix=False)
    assert isinstance(result, list), f"Expected list, got {type(result)}"
    assert len(result) > 0


def test_doctor_m0_creates_data_dir(tmp_path):
    """After _m0_dirs with fix=True, data_dir exists."""
    cfg = _cfg(tmp_path)
    import dataclasses
    cfg2 = dataclasses.replace(cfg, data_dir=tmp_path / "new_data_dir")
    _, ok, _ = doctor._m0_dirs(cfg2, fix=True)
    assert ok is True
    assert cfg2.data_dir.is_dir()


def test_hiveconfig_exec_model_has_value(tmp_path):
    """exec_model is a non-empty string under default env."""
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert isinstance(cfg.exec_model, str)
    assert len(cfg.exec_model) > 0


def test_hiveconfig_data_dir_is_path(tmp_path):
    """data_dir field is a Path object, not a plain string."""
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert isinstance(cfg.data_dir, type(tmp_path))  # pathlib.Path


def test_hiveconfig_minimax_key_empty_by_default(tmp_path, monkeypatch):
    """Without MINIMAX_API_KEY in env, minimax_api_key is an empty string."""
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert cfg.minimax_api_key == ""


def test_migrations_schema_has_version_column(tmp_path):
    """schema_migrations table has a 'version' column (not 'id' only)."""
    import sqlite3
    cfg = _cfg(tmp_path)
    doctor._ensure_migrations_table(cfg.state_db)
    conn = sqlite3.connect(str(cfg.state_db))
    info = conn.execute("PRAGMA table_info(schema_migrations)").fetchall()
    conn.close()
    col_names = [row[1] for row in info]
    assert "version" in col_names, f"Expected 'version' column, got columns: {col_names}"


def test_hiveconfig_validate_returns_list_type(tmp_path):
    """validate() always returns a list — it must never raise."""
    import dataclasses
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    # Even with a deliberately invalid config, validate() returns a list of issues.
    bad_cfg = dataclasses.replace(cfg, exec_provider="bad", shell_provider="bad",
                                  max_iterations=0)
    result = bad_cfg.validate()
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Six new tests appended for coverage expansion
# ---------------------------------------------------------------------------

def test_doctor_static_checks_include_hive_secret_check(tmp_path):
    """_static_checks must include a check for HIVE_SECRET being customized."""
    cfg = _cfg(tmp_path)
    config.set_config(cfg)
    results = {name: ok for name, ok, _ in doctor._static_checks(cfg)}
    assert "HIVE_SECRET customized" in results


def test_doctor_static_checks_default_secret_fails(tmp_path):
    """When secret is the default 'change_me', HIVE_SECRET customized must be False."""
    import dataclasses
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    # Ensure the secret is the default
    cfg2 = dataclasses.replace(cfg, secret="change_me")
    config.set_config(cfg2)
    results = {name: ok for name, ok, _ in doctor._static_checks(cfg2)}
    assert results["HIVE_SECRET customized"] is False


def test_doctor_static_checks_custom_secret_passes(tmp_path, monkeypatch):
    """When secret is non-default, HIVE_SECRET customized must be True."""
    import dataclasses
    monkeypatch.setenv("HIVE_SECRET", "my-strong-custom-secret")
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    config.set_config(cfg)
    results = {name: ok for name, ok, _ in doctor._static_checks(cfg)}
    assert results["HIVE_SECRET customized"] is True


def test_config_validate_fails_on_out_of_range_port(tmp_path):
    """port=0 (below minimum of 1) is reported as an issue."""
    import dataclasses
    cfg = dataclasses.replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        port=0,
    )
    issues = cfg.validate()
    assert any("HIVE_PORT" in i for i in issues), f"Expected HIVE_PORT issue, got: {issues}"


def test_config_validate_fails_on_zero_daily_call_cap(tmp_path):
    """daily_call_cap=0 (below minimum of 1) is reported as an issue."""
    import dataclasses
    cfg = dataclasses.replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        daily_call_cap=0,
    )
    issues = cfg.validate()
    assert any("HIVE_DAILY_CALL_CAP" in i for i in issues), \
        f"Expected HIVE_DAILY_CALL_CAP issue, got: {issues}"


def test_config_validate_anthropic_key_required_when_provider_is_anthropic(tmp_path):
    """exec_provider=anthropic with empty anthropic_api_key is reported as an issue."""
    import dataclasses
    cfg = dataclasses.replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        exec_provider="anthropic",
        anthropic_api_key="",
    )
    issues = cfg.validate()
    assert any("ANTHROPIC_API_KEY" in i for i in issues), \
        f"Expected ANTHROPIC_API_KEY issue, got: {issues}"
