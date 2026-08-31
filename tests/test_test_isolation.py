"""Regression tests for process-wide pytest state isolation."""
from __future__ import annotations

from hive.core.config import HiveConfig


def test_default_env_config_uses_pytest_temp_runtime(tmp_path):
    """A default config must never target the repository's durable state."""
    cfg = HiveConfig.from_env()

    assert cfg.root.is_relative_to(tmp_path)
    assert cfg.state_db.is_relative_to(tmp_path)
    assert cfg.mnemosyne_home.is_relative_to(tmp_path)
    assert cfg.obsidian_vault.is_relative_to(tmp_path)
