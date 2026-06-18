"""Tests for src/hive/core/credentials.py."""
from __future__ import annotations

import json
import os
import stat

import pytest

from hive.core import config, credentials


def _cfg(tmp_path):
    cfg = config.HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    config.set_config(cfg)
    cfg.ensure_dirs()
    return cfg


def test_save_and_get_roundtrip(tmp_path):
    _cfg(tmp_path)
    credentials.save("API_KEY", "abc123")
    assert credentials.get("API_KEY") == "abc123"


def test_save_persists_to_json_file(tmp_path):
    _cfg(tmp_path)
    credentials.save("TOKEN", "tok-xyz")
    raw = json.loads(credentials._path().read_text(encoding="utf-8"))
    assert raw["TOKEN"] == "tok-xyz"


def test_save_multiple_keys(tmp_path):
    _cfg(tmp_path)
    credentials.save("K1", "v1")
    credentials.save("K2", "v2")
    assert credentials.get("K1") == "v1"
    assert credentials.get("K2") == "v2"


def test_save_overwrites_existing_key(tmp_path):
    _cfg(tmp_path)
    credentials.save("K", "old")
    credentials.save("K", "new")
    assert credentials.get("K") == "new"


def test_file_permissions_are_0600(tmp_path):
    _cfg(tmp_path)
    credentials.save("X", "y")
    if os.name == "posix":
        mode = stat.S_IMODE(os.stat(credentials._path()).st_mode)
        assert mode == 0o600


def test_get_returns_default_when_key_absent(tmp_path):
    _cfg(tmp_path)
    assert credentials.get("NONEXISTENT") is None
    assert credentials.get("NONEXISTENT", "fallback") == "fallback"


def test_get_falls_back_to_env(tmp_path, monkeypatch):
    _cfg(tmp_path)
    monkeypatch.setenv("ENV_ONLY_KEY", "env-value")
    assert credentials.get("ENV_ONLY_KEY") == "env-value"


def test_vault_key_takes_precedence_over_env(tmp_path, monkeypatch):
    _cfg(tmp_path)
    monkeypatch.setenv("MY_KEY", "env-version")
    credentials.save("MY_KEY", "vault-version")
    assert credentials.get("MY_KEY") == "vault-version"


def test_inject_sets_missing_env_vars(tmp_path, monkeypatch):
    _cfg(tmp_path)
    monkeypatch.delenv("INJECTED_KEY", raising=False)
    credentials.save("INJECTED_KEY", "injected-val")
    n = credentials.inject()
    assert os.environ.get("INJECTED_KEY") == "injected-val"
    assert n >= 1


def test_inject_does_not_overwrite_existing_env(tmp_path, monkeypatch):
    _cfg(tmp_path)
    credentials.save("EXISTING_KEY", "vault-val")
    monkeypatch.setenv("EXISTING_KEY", "env-wins")
    credentials.inject()
    assert os.environ["EXISTING_KEY"] == "env-wins"


def test_inject_returns_count_of_injected(tmp_path, monkeypatch):
    _cfg(tmp_path)
    monkeypatch.delenv("NEW_A", raising=False)
    monkeypatch.delenv("NEW_B", raising=False)
    credentials.save("NEW_A", "a")
    credentials.save("NEW_B", "b")
    n = credentials.inject()
    assert n == 2


def test_inject_noop_on_empty_vault(tmp_path):
    _cfg(tmp_path)
    assert credentials.inject() == 0


def test_load_returns_empty_on_corrupt_file(tmp_path):
    _cfg(tmp_path)
    credentials._path().write_text("{ bad json", encoding="utf-8")
    assert credentials._load() == {}


def test_load_returns_empty_when_no_file(tmp_path):
    _cfg(tmp_path)
    assert not credentials._path().exists()
    assert credentials._load() == {}
