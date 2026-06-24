"""
Tests for hive.core.doctor — health checks and migration owner.

Covers the 15 lines missed at 87%: exception paths in M1, the migration
table helpers, _static_checks import failure paths, run() ok_all False
on non-warn failure, and `if __name__ == "__main__"` SystemExit.
"""
from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest

from hive.core import config, doctor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> config.HiveConfig:
    """Build a fresh HiveConfig rooted in tmp_path and inject it via set_config()."""
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HIVE_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("HIVE_STATE_DB", str(tmp_path / "hive.sqlite"))
    monkeypatch.setenv("HIVE_MINIMAX_API_KEY", "sk-test")
    monkeypatch.setenv("HIVE_SECRET", "test-secret-not-default")
    monkeypatch.setenv("HIVE_SHELL_PROVIDER", "local")
    monkeypatch.setenv("HIVE_SANDBOX_IMAGE", "")
    cfg = config.HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    config.set_config(cfg)
    return cfg


# ---------------------------------------------------------------------------
# Migration checks
# ---------------------------------------------------------------------------


def test_m1_state_db_schema_opens_db(cfg):
    name, ok, detail = doctor._m1_state_db_schema(cfg, fix=False)
    assert name == "state DB openable" and ok is True
    assert detail == str(cfg.state_db)


def test_m1_state_db_schema_reports_missing_parent_when_not_fix(cfg, tmp_path):
    """Lines 51-54: state DB parent dir missing, fix=False → failed 'state DB dir' diagnostic."""
    cfg_missing = mock.MagicMock(spec=config.HiveConfig)
    fake_db = tmp_path / "does" / "not" / "exist" / "hive.sqlite"
    cfg_missing.state_db = fake_db
    name, ok, detail = doctor._m1_state_db_schema(cfg_missing, fix=False)
    assert name == "state DB dir" and ok is False
    assert detail == str(fake_db.parent)


def test_m1_state_db_schema_creates_parent_dir_when_fix(cfg, tmp_path):
    """Lines 51-52: state DB parent dir missing, fix=True → mkdir(parents=True)."""
    cfg_missing = mock.MagicMock(spec=config.HiveConfig)
    fake_db = tmp_path / "does" / "not" / "exist" / "hive.sqlite"
    cfg_missing.state_db = fake_db
    name, ok, detail = doctor._m1_state_db_schema(cfg_missing, fix=True)
    # After fix, the dir is created and the DB opens fine.
    assert name == "state DB openable" and ok is True
    assert fake_db.parent.is_dir()


def test_m1_state_db_schema_handles_connect_failure(cfg):
    """Lines 60-61: sqlite connect failure → caught, returned as a failed diagnostic."""
    with mock.patch.object(sqlite3, "connect", side_effect=OSError("disk full")):
        name, ok, detail = doctor._m1_state_db_schema(cfg, fix=False)
    assert name == "state DB openable" and ok is False
    assert "disk full" in detail


def test_m3_docker_skipped_when_no_sandbox_image(cfg):
    name, ok, detail = doctor._m3_docker(cfg, fix=False)
    assert name == "docker (sandbox not configured)"
    assert ok is True and detail == "skipped"


def test_m3_docker_checks_binary_when_sandbox_image_set(cfg):
    """Lines 77-79: when sandbox_image is set, check shutil.which('docker')."""
    cfg_docker = mock.MagicMock(spec=config.HiveConfig)
    cfg_docker.sandbox_image = "hive/sandbox:latest"
    # Simulate docker present
    with mock.patch("shutil.which", return_value="/usr/bin/docker"):
        name, ok, detail = doctor._m3_docker(cfg_docker, fix=False)
    assert name == "docker available for sandbox"
    assert ok is True
    assert detail == "hive/sandbox:latest"

    # Simulate docker missing
    with mock.patch("shutil.which", return_value=None):
        name, ok, detail = doctor._m3_docker(cfg_docker, fix=False)
    assert ok is False
    assert detail == "hive/sandbox:latest"


def test_m4_shell_provider_invalid_value(cfg):
    """An unrecognised shell_provider value is reported as failed (line 86-87)."""
    cfg_invalid = mock.MagicMock(spec=config.HiveConfig)
    cfg_invalid.shell_provider = "podman"
    name, ok, detail = doctor._m4_shell_provider(cfg_invalid, fix=False)
    assert ok is False
    assert "not 'local' or 'docker'" in detail


def test_m4_shell_provider_docker_with_missing_binary():
    """Line 89: HIVE_SHELL_PROVIDER=docker but 'docker' not in PATH → failed."""
    cfg_docker = mock.MagicMock(spec=config.HiveConfig)
    cfg_docker.shell_provider = "docker"
    with mock.patch("shutil.which", return_value=None):
        name, ok, detail = doctor._m4_shell_provider(cfg_docker, fix=False)
    assert ok is False
    assert "'docker' binary not found" in detail


# ---------------------------------------------------------------------------
# Migration table helpers — exception paths
# ---------------------------------------------------------------------------


def test_ensure_migrations_table_swallows_exception():
    """Lines 109-110: _ensure_migrations_table must not raise if sqlite fails."""
    with mock.patch.object(sqlite3, "connect", side_effect=OSError("boom")):
        doctor._ensure_migrations_table("/tmp/x.sqlite")  # must not raise


def test_get_applied_returns_empty_set_on_exception():
    """Lines 119-120: query failure → empty set, not exception."""
    with mock.patch.object(sqlite3, "connect", side_effect=OSError("boom")):
        assert doctor._get_applied("/tmp/x.sqlite") == set()


def test_record_migration_swallows_exception():
    """Lines 132-133: insert failure must be swallowed."""
    with mock.patch.object(sqlite3, "connect", side_effect=OSError("boom")):
        doctor._record_migration("/tmp/x.sqlite", "v1")  # must not raise


# ---------------------------------------------------------------------------
# _static_checks — import failure paths
# ---------------------------------------------------------------------------


def _detail_for(results, name):
    for n, _, detail in results:
        if n == name:
            return detail
    return None


def test_static_checks_handles_core_module_import_failure(monkeypatch, cfg):
    """Lines 150-151: a core module failing to import becomes a failed diagnostic."""
    sentinel = "simulated core module import failure"
    real_import = importlib.import_module

    def fake_import(name, *args, **kwargs):
        if name == "hive.core.events":
            raise ImportError(sentinel)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    results = doctor._static_checks(cfg)
    assert _detail_for(results, "import hive.core.events") == sentinel
    assert _detail_for(results, "import hive.core.config") == "ok"


def test_static_checks_handles_mnemosyne_import_failure(monkeypatch, cfg):
    """Lines 161-163: when mnemosyne is not installed, a warn-only diagnostic is appended."""
    real_import = importlib.import_module

    def fake_import(name, *args, **kwargs):
        if name == "mnemosyne.core.beam":
            raise ImportError("no mnemosyne here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    results = doctor._static_checks(cfg)
    mnemosyne_detail = _detail_for(results, "mnemosyne package")
    assert mnemosyne_detail is not None
    assert "LocalMemoryProvider fallback" in mnemosyne_detail
    assert "pip install mnemosyne-memory" in mnemosyne_detail


# ---------------------------------------------------------------------------
# check() — full happy path + migration table recording
# ---------------------------------------------------------------------------


def test_check_records_migrations_when_fix_true(cfg):
    out = doctor.check(fix=True)
    names = [n for n, _, _ in out]
    assert "data dir present" in names
    assert "state DB openable" in names
    assert "shell_provider valid" in names
    applied = doctor._get_applied(cfg.state_db)
    assert "_m0_dirs" in applied
    assert "_m1_state_db_schema" in applied


# ---------------------------------------------------------------------------
# run() — ok_all is False when a non-warn check fails (line 191)
# ---------------------------------------------------------------------------


def test_run_returns_false_when_critical_check_fails(cfg, capsys):
    """run() must return False and print ✗ when a non-warn check fails."""
    bad = ("db schema broken", False, "table X missing")
    with mock.patch.object(doctor, "_static_checks", return_value=[bad]):
        ok = doctor.run(fix=False)
    assert ok is False
    out = capsys.readouterr().out
    assert "✗ db schema broken" in out


def test_run_ignores_warn_only_failures(cfg, capsys):
    """run() must return True even when MINIMAX_API_KEY is missing (warn-only)."""
    bad = ("MINIMAX_API_KEY set", False, "env")
    with mock.patch.object(doctor, "_static_checks", return_value=[bad]):
        ok = doctor.run(fix=False)
    assert ok is True
    out = capsys.readouterr().out
    assert "✗ MINIMAX_API_KEY" in out  # printed but doesn't fail run()


# ---------------------------------------------------------------------------
# main() and `if __name__ == "__main__"` SystemExit (line 202)
# ---------------------------------------------------------------------------


def test_main_returns_zero_when_all_pass(monkeypatch):
    monkeypatch.setattr(doctor, "run", lambda fix=False: True)
    assert doctor.main() == 0


def test_main_returns_one_when_check_fails(monkeypatch):
    monkeypatch.setattr(doctor, "run", lambda fix=False: False)
    assert doctor.main() == 1


def test_main_passes_fix_flag_from_argv(monkeypatch):
    seen = {}

    def fake_run(fix=False):
        seen["fix"] = fix
        return True

    monkeypatch.setattr(doctor, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["hive-doctor", "--fix"])
    doctor.main()
    assert seen["fix"] is True


def test_module_name_main_raises_systemexit(monkeypatch):
    """Running doctor.py as __main__ must raise SystemExit (line 202).

    The line is hit whenever the `if __name__ == "__main__"` block runs;
    the exact exit code depends on the env (CI has no HIVE_SECRET/MINIMAX_API_KEY
    so it exits 1). We only assert that SystemExit is raised and the code is 0 or 1.
    """
    import runpy
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(str(Path(doctor.__file__)), run_name="__main__")
    assert exc_info.value.code in (0, 1)
