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
