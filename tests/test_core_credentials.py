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


# ---------------------------------------------------------------------------
# New tests
# ---------------------------------------------------------------------------

def test_credential_store_set_and_get_roundtrip(tmp_path):
    """save() followed by get() returns the same value."""
    _cfg(tmp_path)
    credentials.save("ROUNDTRIP_KEY", "roundtrip_value")
    assert credentials.get("ROUNDTRIP_KEY") == "roundtrip_value"


def test_credential_store_missing_key_returns_none(tmp_path):
    """get() on a key that was never saved returns None (no env fallback)."""
    _cfg(tmp_path)
    # Ensure the key is absent from the environment too
    os.environ.pop("TOTALLY_ABSENT_KEY_XYZ", None)
    result = credentials.get("TOTALLY_ABSENT_KEY_XYZ")
    assert result is None


def test_credential_store_delete_removes_key(tmp_path):
    """Manually removing a key from the JSON file makes get() return None."""
    _cfg(tmp_path)
    credentials.save("DELETE_ME", "gone")
    assert credentials.get("DELETE_ME") == "gone"

    # Simulate deletion by rewriting the JSON without that key
    path = credentials._path()
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["DELETE_ME"]
    path.write_text(json.dumps(data), encoding="utf-8")

    os.environ.pop("DELETE_ME", None)
    assert credentials.get("DELETE_ME") is None


def test_credential_store_list_all_keys(tmp_path):
    """_load() returns a dict containing all saved keys."""
    _cfg(tmp_path)
    keys = ["ALPHA", "BETA", "GAMMA"]
    for k in keys:
        credentials.save(k, f"val_{k}")
    data = credentials._load()
    for k in keys:
        assert k in data, f"key {k!r} missing from _load() result"


def test_credential_store_update_overwrites(tmp_path):
    """Saving the same key twice keeps only the latest value."""
    _cfg(tmp_path)
    credentials.save("UPDATE_KEY", "first")
    credentials.save("UPDATE_KEY", "second")
    assert credentials.get("UPDATE_KEY") == "second"
    # Confirm only one entry in the file
    data = credentials._load()
    assert list(data).count("UPDATE_KEY") == 1


def test_credential_store_empty_initially(tmp_path):
    """Before any save(), _load() returns an empty dict and get() returns None."""
    _cfg(tmp_path)
    assert credentials._load() == {}
    os.environ.pop("EMPTY_INIT_KEY", None)
    assert credentials.get("EMPTY_INIT_KEY") is None


# ---------------------------------------------------------------------------
# Six additional tests
# ---------------------------------------------------------------------------

def test_save_stores_value_as_string_in_json(tmp_path):
    """save() writes the value as a plain JSON string, not nested object."""
    _cfg(tmp_path)
    credentials.save("STRING_VAL_KEY", "plain_string")
    raw = json.loads(credentials._path().read_text(encoding="utf-8"))
    assert isinstance(raw["STRING_VAL_KEY"], str)
    assert raw["STRING_VAL_KEY"] == "plain_string"


def test_get_custom_default_returned_when_absent(tmp_path):
    """get() returns the caller-supplied default when key is absent from vault and env."""
    _cfg(tmp_path)
    os.environ.pop("ABSENT_KEY_CUSTOM_DEFAULT", None)
    result = credentials.get("ABSENT_KEY_CUSTOM_DEFAULT", "my_default")
    assert result == "my_default"


def test_save_creates_parent_directory_if_missing(tmp_path):
    """save() creates missing parent directories automatically."""
    cfg = _cfg(tmp_path)
    # Delete the data dir to force save() to recreate it.
    import shutil
    shutil.rmtree(cfg.data_dir, ignore_errors=True)
    credentials.save("NEW_DIR_KEY", "hello")
    assert credentials._path().exists()
    assert credentials.get("NEW_DIR_KEY") == "hello"


def test_load_is_idempotent(tmp_path):
    """Calling _load() twice returns identical results."""
    _cfg(tmp_path)
    credentials.save("IDEMPOTENT_KEY", "val42")
    first = credentials._load()
    second = credentials._load()
    assert first == second


def test_inject_returns_zero_when_all_keys_already_in_env(tmp_path, monkeypatch):
    """inject() returns 0 when every vault key is already present in os.environ."""
    _cfg(tmp_path)
    credentials.save("ALREADY_SET_KEY", "vault-value")
    monkeypatch.setenv("ALREADY_SET_KEY", "existing-env-value")
    count = credentials.inject()
    assert count == 0


def test_save_and_get_special_characters(tmp_path):
    """save()/get() round-trip works for values containing special characters."""
    _cfg(tmp_path)
    special = "p@$$w0rd!#&=+/ \t\n"
    credentials.save("SPECIAL_CHARS_KEY", special)
    assert credentials.get("SPECIAL_CHARS_KEY") == special
