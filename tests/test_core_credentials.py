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


# ---------------------------------------------------------------------------
# Batch 5 — six additional tests
# ---------------------------------------------------------------------------

def test_path_is_inside_data_dir(tmp_path):
    """_path() must be located inside the configured data_dir."""
    cfg = _cfg(tmp_path)
    assert credentials._path().parent == cfg.data_dir


def test_save_overwrites_preserves_other_keys(tmp_path):
    """Overwriting one key must not affect other keys stored in the same file."""
    _cfg(tmp_path)
    credentials.save("STABLE", "stays")
    credentials.save("CHANGE", "original")
    credentials.save("CHANGE", "updated")
    assert credentials.get("STABLE") == "stays"
    assert credentials.get("CHANGE") == "updated"


def test_get_env_fallback_not_returned_when_key_is_in_vault(tmp_path, monkeypatch):
    """Vault value wins over the same key set in os.environ."""
    _cfg(tmp_path)
    monkeypatch.setenv("PRIORITY_KEY", "env-val")
    credentials.save("PRIORITY_KEY", "vault-val")
    assert credentials.get("PRIORITY_KEY") == "vault-val"


def test_inject_sets_multiple_env_vars(tmp_path, monkeypatch):
    """inject() must set each vault key as an os.environ entry."""
    _cfg(tmp_path)
    for k in ("INJ_X", "INJ_Y", "INJ_Z"):
        monkeypatch.delenv(k, raising=False)
        credentials.save(k, f"val-{k}")
    n = credentials.inject()
    assert n == 3
    for k in ("INJ_X", "INJ_Y", "INJ_Z"):
        assert os.environ.get(k) == f"val-{k}"


def test_load_after_corrupt_then_resave(tmp_path):
    """After a corrupt vault, saving a new key should create a fresh valid file."""
    _cfg(tmp_path)
    credentials._path().parent.mkdir(parents=True, exist_ok=True)
    credentials._path().write_text("{ corrupted }", encoding="utf-8")
    # _load() returns {} on corruption; save() must start fresh
    credentials.save("FRESH_KEY", "fresh-val")
    assert credentials.get("FRESH_KEY") == "fresh-val"


def test_vault_json_is_valid_on_disk(tmp_path):
    """The on-disk file must always contain parseable JSON after save()."""
    _cfg(tmp_path)
    credentials.save("VALID_JSON_KEY", "somevalue")
    raw = credentials._path().read_text(encoding="utf-8")
    parsed = json.loads(raw)   # must not raise
    assert isinstance(parsed, dict)


# --- Wave 3P additional tests ---------------------------------------------------

def test_save_multiple_keys_coexist(tmp_path):
    """Saving two different keys both appear in the vault simultaneously."""
    _cfg(tmp_path)
    credentials.save("FIRST_KEY", "val1")
    credentials.save("SECOND_KEY", "val2")
    assert credentials.get("FIRST_KEY") == "val1"
    assert credentials.get("SECOND_KEY") == "val2"


def test_get_returns_default_when_key_absent(tmp_path):
    """get() returns the specified default when the key is not in the vault."""
    _cfg(tmp_path)
    result = credentials.get("NONEXISTENT_XYZ", default="fallback")
    assert result == "fallback"


def test_get_returns_none_when_no_default(tmp_path):
    """get() returns None when the key is absent and no default given."""
    _cfg(tmp_path)
    result = credentials.get("TOTALLY_ABSENT_KEY")
    assert result is None


def test_save_overwrites_old_value(tmp_path):
    """save() replaces the previous value for the same key."""
    _cfg(tmp_path)
    credentials.save("OVERWRITE_KEY", "old")
    credentials.save("OVERWRITE_KEY", "new")
    assert credentials.get("OVERWRITE_KEY") == "new"


def test_inject_count_positive_after_save(tmp_path, monkeypatch):
    """inject() returns > 0 after saving a key that isn't in the environment."""
    _cfg(tmp_path)
    credentials.save("MY_NEW_SECRET_PQR", "abc123")
    monkeypatch.delenv("MY_NEW_SECRET_PQR", raising=False)
    count = credentials.inject()
    assert count >= 1


def test_save_unicode_value_round_trips(tmp_path):
    """save() and get() handle Unicode values without corruption."""
    _cfg(tmp_path)
    credentials.save("UNICODE_KEY", "héllo wörld 🌍")
    assert credentials.get("UNICODE_KEY") == "héllo wörld 🌍"


# --- Wave 3Q additional tests ---------------------------------------------------

def test_save_empty_string_value(tmp_path):
    """save() and get() preserve an empty string value."""
    _cfg(tmp_path)
    credentials.save("EMPTY_VAL_KEY", "")
    assert credentials.get("EMPTY_VAL_KEY") == ""


def test_get_with_default_returns_saved_over_default(tmp_path):
    """When a saved value exists, get() returns it even if a default is given."""
    _cfg(tmp_path)
    credentials.save("PRIO_KEY", "real_value")
    result = credentials.get("PRIO_KEY", default="fallback")
    assert result == "real_value"


def test_inject_adds_to_os_environ(tmp_path, monkeypatch):
    """inject() must add saved credentials to os.environ."""
    _cfg(tmp_path)
    credentials.save("MY_INJECT_KEY_7A", "injected_val")
    monkeypatch.delenv("MY_INJECT_KEY_7A", raising=False)
    credentials.inject()
    assert os.environ.get("MY_INJECT_KEY_7A") == "injected_val"


def test_save_numeric_value_as_string(tmp_path):
    """save() must accept a string representation of a number without error."""
    _cfg(tmp_path)
    credentials.save("PORT_NUM", "8080")
    assert credentials.get("PORT_NUM") == "8080"


def test_save_and_get_with_special_chars(tmp_path):
    """Credentials can store values with special characters like =, +, /."""
    _cfg(tmp_path)
    credentials.save("BASE64_KEY", "abc/def+ghi==")
    assert credentials.get("BASE64_KEY") == "abc/def+ghi=="


def test_save_path_is_in_data_dir(tmp_path):
    """The credentials file path must live inside the configured data directory."""
    cfg = _cfg(tmp_path)
    path = credentials._path()
    assert str(path).startswith(str(tmp_path))
