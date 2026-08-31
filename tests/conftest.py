"""Shared pytest fixtures for the HiveOS test suite."""
from __future__ import annotations

import os

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
