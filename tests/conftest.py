"""Shared pytest fixtures for the HiveOS test suite."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from hive.core.approval import gate as _approval_gate
import hive.core.config as _config_mod

# Tests must never inherit runtime/provider/channel configuration from a user's
# shell or repository .env.  The live-test switch is not read by HiveConfig.
_CONFIG_PREFIXES = (
    "HIVE_",
    "MINIMAX_",
    "ANTHROPIC_",
    "MNEMOSYNE_",
    "OBSIDIAN_",
    "TELEGRAM_",
    "STRIPE_",
)
_TEST_CONTROL_VARS = {"HIVE_LIVE_TEST"}
_PROTECTED_RUNTIME_SNAPSHOTS: dict[Path, tuple[bool, int, int, str]] = {}


def _protected_runtime_paths() -> set[Path]:
    """Return repository-local runtime DB candidates that pytest must not mutate."""
    repo_root = Path(__file__).resolve().parents[1]
    paths = {repo_root / "data" / "hive.db", repo_root / "data" / "hive.sqlite"}
    configured = os.environ.get("HIVE_STATE_DB")
    if configured:
        paths.add(Path(configured).expanduser())
    return {path.resolve() for path in paths}


def _state_snapshot(path: Path) -> tuple[bool, int, int, str]:
    """Hash the DB plus its WAL sidecars without opening SQLite or creating files."""
    files = (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
    digest = hashlib.sha256()
    exists = False
    total_size = 0
    newest_mtime = 0
    for file_path in files:
        if not file_path.is_file():
            digest.update(b"missing")
            continue
        stat = file_path.stat()
        exists = True
        total_size += stat.st_size
        newest_mtime = max(newest_mtime, stat.st_mtime_ns)
        digest.update(file_path.name.encode("utf-8"))
        with file_path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return exists, total_size, newest_mtime, digest.hexdigest()


def pytest_sessionstart(session) -> None:  # noqa: ARG001
    """Snapshot real runtime state before any test fixture can run."""
    global _PROTECTED_RUNTIME_SNAPSHOTS
    _PROTECTED_RUNTIME_SNAPSHOTS = {
        path: _state_snapshot(path) for path in _protected_runtime_paths()
    }


def pytest_sessionfinish(session, exitstatus: int) -> None:
    """Fail the suite if it modified a protected runtime DB or its WAL sidecars."""
    changed = [
        path for path, before in _PROTECTED_RUNTIME_SNAPSHOTS.items()
        if _state_snapshot(path) != before
    ]
    if not changed:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    message = "pytest modified protected runtime state: " + ", ".join(str(path) for path in changed)
    if reporter is not None:
        reporter.write_line(message, red=True)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def _reset_globals(tmp_path, monkeypatch):
    """Give every test a clean singleton state and a private runtime root."""
    saved_config = _config_mod._CONFIG
    test_root = tmp_path / "hive-test-runtime"
    test_data_dir = test_root / "data"

    _approval_gate._pending.clear()
    _config_mod._CONFIG = None

    # Use monkeypatch for every environment mutation so pytest restores the
    # caller's real configuration after the test.  A temporary REPO_ROOT also
    # prevents default HiveConfig.from_env() calls from finding H:\HiveOS\.env.
    for key in tuple(os.environ):
        if key not in _TEST_CONTROL_VARS and key.startswith(_CONFIG_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(_config_mod, "REPO_ROOT", test_root)
    monkeypatch.setenv("HIVE_DATA_DIR", str(test_data_dir))
    monkeypatch.setenv("HIVE_STATE_DB", str(test_data_dir / "hive.sqlite"))
    monkeypatch.setenv("MNEMOSYNE_HOME", str(test_data_dir / "mnemosyne"))
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(test_root / "vault"))
    monkeypatch.setenv("HIVE_SECRET", "change_me")

    yield

    _approval_gate._pending.clear()
    _config_mod._CONFIG = saved_config
