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
