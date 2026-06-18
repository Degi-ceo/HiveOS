"""credential_pool.py — gaps not covered by test_llm.py."""
from __future__ import annotations

import pytest

from hive.llm.credential_pool import CredentialPool, PooledCredential


# --- PooledCredential defaults ------------------------------------------------

def test_pooled_credential_default_fields():
    c = PooledCredential(key="abc")
    assert c.key == "abc"
    assert c.label == ""
    assert c.failures == 0
    assert c.cooldown_until == 0.0


# --- Single-key pool -----------------------------------------------------------

def test_single_key_always_returns_same_key():
    pool = CredentialPool(["only-key"])
    results = {pool.acquire().key for _ in range(5)}
    assert results == {"only-key"}


# --- report_success -----------------------------------------------------------

def test_report_success_clears_failures_and_cooldown():
    now = [0.0]
    pool = CredentialPool(["k1"], cooldown_seconds=60.0, clock=lambda: now[0])
    cred = pool.acquire()
    pool.report_failure(cred)
    assert cred.failures == 1
    assert cred.cooldown_until > 0.0

    pool.report_success(cred)
    assert cred.failures == 0
    assert cred.cooldown_until == 0.0


def test_report_success_makes_cooled_key_available_again():
    now = [0.0]
    pool = CredentialPool(["k1"], cooldown_seconds=60.0, clock=lambda: now[0])
    cred = pool.acquire()
    pool.report_failure(cred)
    assert pool.acquire() is None  # cooling

    pool.report_success(cred)
    assert pool.acquire() is not None  # available again


# --- cooldown() (non-failure park) --------------------------------------------

def test_cooldown_parks_key_without_bumping_failures():
    now = [0.0]
    pool = CredentialPool(["k1", "k2"], cooldown_seconds=60.0, clock=lambda: now[0])
    cred = pool.acquire()
    pool.cooldown(cred, 30.0)
    assert cred.failures == 0           # no failure counted
    assert cred.cooldown_until > now[0]


def test_cooldown_key_skipped_while_cooling():
    now = [0.0]
    pool = CredentialPool(["k1", "k2"], cooldown_seconds=60.0, clock=lambda: now[0])
    c1 = pool.acquire()
    pool.cooldown(c1, 10.0)
    # only k2 should be available
    available_keys = {pool.acquire().key for _ in range(3)}
    assert available_keys == {"k2"}


def test_cooldown_key_available_after_window_expires():
    now = [0.0]
    pool = CredentialPool(["k1"], cooldown_seconds=60.0, clock=lambda: now[0])
    cred = pool.acquire()
    pool.cooldown(cred, 10.0)
    assert pool.acquire() is None       # still cooling

    now[0] = 11.0
    assert pool.acquire() is not None   # window expired


def test_cooldown_does_not_shorten_existing_longer_cooldown():
    now = [0.0]
    pool = CredentialPool(["k1"], cooldown_seconds=60.0, clock=lambda: now[0])
    cred = pool.acquire()
    pool.cooldown(cred, 100.0)
    original = cred.cooldown_until
    pool.cooldown(cred, 5.0)            # shorter — must not overwrite
    assert cred.cooldown_until == original


# --- available() list ---------------------------------------------------------

def test_available_returns_only_non_cooled():
    now = [0.0]
    pool = CredentialPool(["a", "b", "c"], cooldown_seconds=30.0, clock=lambda: now[0])
    cred = pool.acquire()
    pool.report_failure(cred)
    avail_keys = {c.key for c in pool.available()}
    assert cred.key not in avail_keys
    assert len(avail_keys) == 2


def test_available_all_cooled_returns_empty():
    now = [0.0]
    pool = CredentialPool(["x", "y"], cooldown_seconds=10.0, clock=lambda: now[0])
    pool.cooldown_all(10.0)
    assert pool.available() == []


# --- custom cooldown via report_failure kwarg ---------------------------------

def test_report_failure_custom_cooldown_overrides_default():
    now = [0.0]
    pool = CredentialPool(["k1"], cooldown_seconds=60.0, clock=lambda: now[0])
    cred = pool.acquire()
    pool.report_failure(cred, cooldown=5.0)
    assert cred.cooldown_until == pytest.approx(5.0)

    now[0] = 6.0
    assert pool.acquire() is not None   # custom shorter window already expired


# --- CredentialPool additional edge cases -----------------------------------------

def test_all_keys_returns_all_credentials():
    from hive.llm.credential_pool import CredentialPool
    pool = CredentialPool(["key-a", "key-b", "key-c"])
    available = pool.available()
    assert len(available) == 3
    keys = {c.key for c in available}
    assert keys == {"key-a", "key-b", "key-c"}


def test_pool_labels_matches_key_count():
    from hive.llm.credential_pool import CredentialPool
    pool = CredentialPool(["k1", "k2"])
    assert len(pool.labels()) == 2


def test_pool_failure_then_success_resets_counter():
    from hive.llm.credential_pool import CredentialPool
    pool = CredentialPool(["key-x"], cooldown_seconds=60.0)
    cred = pool.acquire()
    pool.report_failure(cred)
    assert pool.total_failures() == 1
    pool.reset_cooldowns()
    pool.report_success(pool.acquire())
    # After reset + success, failures back to 0
    assert pool.total_failures() == 0


def test_pool_acquire_after_report_success():
    from hive.llm.credential_pool import CredentialPool
    pool = CredentialPool(["only-key"])
    c = pool.acquire()
    pool.report_success(c)
    c2 = pool.acquire()
    assert c2 is not None
    assert c2.key == "only-key"


# --- New tests (batch 2) -------------------------------------------------------

def test_empty_pool_acquire_returns_none():
    """An empty pool must return None immediately."""
    pool = CredentialPool([])
    assert pool.acquire() is None


def test_empty_pool_len_is_zero():
    """__len__ must report 0 for an empty pool."""
    pool = CredentialPool([])
    assert len(pool) == 0


def test_duplicate_keys_deduplicated():
    """Duplicate keys must be stored only once."""
    pool = CredentialPool(["dup", "dup", "dup"])
    assert len(pool) == 1
    assert pool.available_count() == 1


def test_status_returns_all_expected_fields():
    """status() must return a list of dicts with all expected keys."""
    now = [0.0]
    pool = CredentialPool(["s1", "s2"], clock=lambda: now[0])
    pool.report_failure(pool.acquire())
    result = pool.status()
    assert len(result) == 2
    for entry in result:
        assert "label" in entry
        assert "failures" in entry
        assert "cooling" in entry
        assert "cooldown_remaining" in entry


def test_available_count_reflects_cooling_state():
    """available_count() must decrease when a key is in cooldown and recover after."""
    now = [0.0]
    pool = CredentialPool(["a", "b", "c"], cooldown_seconds=30.0, clock=lambda: now[0])
    cred = pool.acquire()
    pool.cooldown(cred, 30.0)
    assert pool.available_count() == 2  # one is cooling
    now[0] = 31.0
    assert pool.available_count() == 3  # cooldown expired


def test_failure_counts_tracks_per_label():
    """failure_counts() must map each label to its individual failure count."""
    now = [0.0]
    pool = CredentialPool(["x", "y"], cooldown_seconds=60.0, clock=lambda: now[0])
    # acquire and fail the first credential twice
    cred_x = pool._creds[0]
    pool.report_failure(cred_x)
    pool.report_failure(cred_x)
    counts = pool.failure_counts()
    assert counts[cred_x.label] == 2
    other_label = [lbl for lbl in counts if lbl != cred_x.label][0]
    assert counts[other_label] == 0


# --- Wave 3K additional tests -------------------------------------------------

def test_available_count_decreases_after_cooldown():
    now = [0.0]
    pool = CredentialPool(["k1", "k2", "k3"], cooldown_seconds=60.0, clock=lambda: now[0])
    cred = pool.acquire()
    assert pool.available_count() == 3
    pool.report_failure(cred)
    assert pool.available_count() == 2


def test_status_includes_all_keys():
    pool = CredentialPool(["alpha", "beta"])
    status = pool.status()
    labels = {entry["label"] for entry in status}
    assert "key0" in labels and "key1" in labels


def test_failure_counts_returns_dict():
    pool = CredentialPool(["k1", "k2"])
    counts = pool.failure_counts()
    assert isinstance(counts, dict)
    assert len(counts) == 2
    assert all(v == 0 for v in counts.values())


def test_available_count_zero_when_all_cooled():
    now = [0.0]
    pool = CredentialPool(["x"], cooldown_seconds=10.0, clock=lambda: now[0])
    pool.cooldown_all(10.0)
    assert pool.available_count() == 0


def test_pooled_credential_failures_increments():
    now = [0.0]
    pool = CredentialPool(["k1"], cooldown_seconds=30.0, clock=lambda: now[0])
    cred = pool.acquire()
    pool.report_failure(cred)
    assert cred.failures == 1
    now[0] = 31.0  # expire cooldown
    cred2 = pool.acquire()
    pool.report_failure(cred2)
    assert cred2.failures == 2


def test_reset_cooldowns_makes_all_available():
    now = [0.0]
    pool = CredentialPool(["k1", "k2"], cooldown_seconds=60.0, clock=lambda: now[0])
    pool.cooldown_all(60.0)
    assert pool.available_count() == 0
    pool.reset_cooldowns()
    assert pool.available_count() == 2


# ---------------------------------------------------------------------------
# Batch 5 — six additional tests
# ---------------------------------------------------------------------------

def test_remove_tool_not_in_pool_is_noop():
    """A pool of one key can never be 'empty' by failure once reset; reset_cooldowns
    also zeroes failures — confirm total_failures returns 0 after reset."""
    now = [0.0]
    pool = CredentialPool(["solo"], cooldown_seconds=5.0, clock=lambda: now[0])
    cred = pool.acquire()
    pool.report_failure(cred)
    pool.report_failure(cred)
    assert pool.total_failures() == 2
    pool.reset_cooldowns()
    assert pool.total_failures() == 0


def test_cooldown_all_parks_every_key():
    """cooldown_all() must make every credential unavailable immediately."""
    now = [0.0]
    pool = CredentialPool(["a", "b", "c"], cooldown_seconds=30.0, clock=lambda: now[0])
    pool.cooldown_all(30.0)
    assert pool.available() == []
    assert pool.acquire() is None


def test_acquire_round_robin_order():
    """acquire() must cycle through credentials in round-robin order."""
    pool = CredentialPool(["first", "second", "third"])
    keys = [pool.acquire().key for _ in range(6)]
    # The sequence must repeat every 3 acquisitions
    assert keys[:3] == keys[3:6]


def test_labels_returns_sequential_names():
    """labels() must return 'key0', 'key1', … in insertion order."""
    pool = CredentialPool(["x", "y", "z"])
    assert pool.labels() == ["key0", "key1", "key2"]


def test_status_cooling_field_is_false_when_fresh():
    """All credentials must show cooling=False in status() before any failure."""
    pool = CredentialPool(["p", "q"])
    for entry in pool.status():
        assert entry["cooling"] is False
        assert entry["cooldown_remaining"] == 0.0


def test_report_failure_increments_total_failures():
    """Each report_failure call must increase total_failures by exactly one."""
    now = [0.0]
    pool = CredentialPool(["k1", "k2"], cooldown_seconds=5.0, clock=lambda: now[0])
    assert pool.total_failures() == 0
    c1 = pool._creds[0]
    pool.report_failure(c1)
    assert pool.total_failures() == 1
    now[0] = 6.0   # expire k1's cooldown
    c2 = pool._creds[1]
    pool.report_failure(c2)
    assert pool.total_failures() == 2
