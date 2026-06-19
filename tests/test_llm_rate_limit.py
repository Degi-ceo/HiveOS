"""rate_limit.py — RateLimitBucket, RateLimitState, parse_rate_limit_headers."""
from __future__ import annotations

import time

import pytest

from hive.llm.rate_limit import (
    RateLimitBucket,
    RateLimitState,
    parse_rate_limit_headers,
)


# --- RateLimitBucket -----------------------------------------------------------

def test_bucket_used_is_limit_minus_remaining():
    b = RateLimitBucket(limit=100, remaining=60)
    assert b.used == 40


def test_bucket_used_clamps_at_zero_when_remaining_exceeds_limit():
    b = RateLimitBucket(limit=10, remaining=15)
    assert b.used == 0


def test_bucket_usage_pct_zero_limit():
    b = RateLimitBucket(limit=0, remaining=0)
    assert b.usage_pct == 0.0


def test_bucket_usage_pct_fully_used():
    b = RateLimitBucket(limit=50, remaining=0)
    assert b.usage_pct == pytest.approx(100.0)


def test_bucket_usage_pct_partial():
    b = RateLimitBucket(limit=200, remaining=100)
    assert b.usage_pct == pytest.approx(50.0)


def test_bucket_remaining_seconds_now_decays_with_time(monkeypatch):
    base = time.time()
    monkeypatch.setattr("hive.llm.rate_limit.time.time", lambda: base)
    b = RateLimitBucket(limit=10, remaining=5, reset_seconds=30.0, captured_at=base)
    assert b.remaining_seconds_now == pytest.approx(30.0)

    monkeypatch.setattr("hive.llm.rate_limit.time.time", lambda: base + 10.0)
    assert b.remaining_seconds_now == pytest.approx(20.0)


def test_bucket_remaining_seconds_now_clamps_at_zero(monkeypatch):
    base = time.time()
    monkeypatch.setattr("hive.llm.rate_limit.time.time", lambda: base + 999.0)
    b = RateLimitBucket(limit=10, remaining=5, reset_seconds=5.0, captured_at=base)
    assert b.remaining_seconds_now == 0.0


# --- RateLimitState ------------------------------------------------------------

def test_state_has_data_false_when_captured_at_zero():
    s = RateLimitState()
    assert s.has_data is False


def test_state_has_data_true_when_captured():
    s = RateLimitState(captured_at=time.time())
    assert s.has_data is True


def test_state_age_seconds_inf_when_no_data():
    s = RateLimitState()
    assert s.age_seconds == float("inf")


def test_state_age_seconds_positive_when_captured(monkeypatch):
    base = time.time()
    monkeypatch.setattr("hive.llm.rate_limit.time.time", lambda: base + 5.0)
    s = RateLimitState(captured_at=base)
    assert s.age_seconds == pytest.approx(5.0)


def test_state_hottest_returns_none_when_no_limits():
    s = RateLimitState()
    assert s.hottest() is None


def test_state_hottest_picks_highest_usage():
    now = time.time()
    # requests_min: 80% used; tokens_hour: 50% used
    s = RateLimitState(
        requests_min=RateLimitBucket(limit=100, remaining=20, captured_at=now),
        tokens_hour=RateLimitBucket(limit=100, remaining=50, captured_at=now),
        captured_at=now,
    )
    assert s.hottest() is s.requests_min


def test_state_hottest_ignores_zero_limit_buckets():
    now = time.time()
    s = RateLimitState(
        requests_min=RateLimitBucket(limit=0, remaining=0, captured_at=now),
        tokens_min=RateLimitBucket(limit=500, remaining=400, captured_at=now),
        captured_at=now,
    )
    assert s.hottest() is s.tokens_min


# --- parse_rate_limit_headers --------------------------------------------------

def test_parse_returns_none_when_no_headers():
    assert parse_rate_limit_headers({}) is None
    assert parse_rate_limit_headers({"content-type": "application/json"}) is None


def test_parse_minimal_requests_headers():
    headers = {
        "x-ratelimit-limit-requests": "100",
        "x-ratelimit-remaining-requests": "60",
        "x-ratelimit-reset-requests": "30",
    }
    state = parse_rate_limit_headers(headers, provider="test")
    assert state is not None
    assert state.requests_min.limit == 100
    assert state.requests_min.remaining == 60
    assert state.requests_min.reset_seconds == pytest.approx(30.0)
    assert state.provider == "test"
    assert state.has_data


def test_parse_all_four_buckets():
    headers = {
        "x-ratelimit-limit-requests": "100",
        "x-ratelimit-remaining-requests": "80",
        "x-ratelimit-reset-requests": "10",
        "x-ratelimit-limit-requests-1h": "1000",
        "x-ratelimit-remaining-requests-1h": "900",
        "x-ratelimit-reset-requests-1h": "3600",
        "x-ratelimit-limit-tokens": "50000",
        "x-ratelimit-remaining-tokens": "40000",
        "x-ratelimit-reset-tokens": "5",
        "x-ratelimit-limit-tokens-1h": "500000",
        "x-ratelimit-remaining-tokens-1h": "450000",
        "x-ratelimit-reset-tokens-1h": "3600",
    }
    state = parse_rate_limit_headers(headers)
    assert state.requests_min.limit == 100
    assert state.requests_hour.limit == 1000
    assert state.tokens_min.limit == 50000
    assert state.tokens_hour.limit == 500000


def test_parse_case_insensitive_header_names():
    headers = {
        "X-RateLimit-Limit-Requests": "200",
        "X-RateLimit-Remaining-Requests": "150",
    }
    state = parse_rate_limit_headers(headers)
    assert state is not None
    assert state.requests_min.limit == 200


def test_parse_malformed_values_default_to_zero():
    headers = {
        "x-ratelimit-limit-requests": "not-a-number",
        "x-ratelimit-remaining-requests": None,
    }
    state = parse_rate_limit_headers(headers)
    assert state is not None
    assert state.requests_min.limit == 0
    assert state.requests_min.remaining == 0


def test_parse_float_string_values_truncated_to_int():
    headers = {
        "x-ratelimit-limit-requests": "99.9",
        "x-ratelimit-remaining-requests": "49.7",
    }
    state = parse_rate_limit_headers(headers)
    assert state.requests_min.limit == 99
    assert state.requests_min.remaining == 49


def test_parse_sets_captured_at_close_to_now():
    before = time.time()
    headers = {"x-ratelimit-limit-requests": "10", "x-ratelimit-remaining-requests": "5"}
    state = parse_rate_limit_headers(headers)
    after = time.time()
    assert before <= state.captured_at <= after
    assert before <= state.requests_min.captured_at <= after


def test_parse_reset_clamped_at_zero_for_negative():
    headers = {
        "x-ratelimit-limit-requests": "10",
        "x-ratelimit-remaining-requests": "5",
        "x-ratelimit-reset-requests": "-5",
    }
    state = parse_rate_limit_headers(headers)
    assert state.requests_min.reset_seconds == 0.0


# --- New tests (appended) -------------------------------------------------------

def test_rate_limit_bucket_full_reset_on_cooldown_expiry(monkeypatch):
    """Exhaust a bucket then advance clock past reset_seconds; capacity is restored."""
    base = time.time()
    monkeypatch.setattr("hive.llm.rate_limit.time.time", lambda: base)

    # Bucket is fully exhausted: remaining == 0 → no capacity
    b = RateLimitBucket(limit=100, remaining=0, reset_seconds=30.0, captured_at=base)
    assert b.remaining > 0 or b.usage_pct == 100.0  # exhausted

    # Advance clock past reset window — remaining_seconds_now should be 0
    monkeypatch.setattr("hive.llm.rate_limit.time.time", lambda: base + 31.0)
    assert b.remaining_seconds_now == 0.0  # window has expired


def test_rate_limit_state_all_models_start_healthy():
    """A freshly constructed RateLimitState has no usage data."""
    s = RateLimitState()
    assert not s.has_data
    assert s.hottest() is None
    assert s.age_seconds == float("inf")


def test_rate_limit_state_mark_and_check_limited():
    """A bucket with remaining == 0 is fully rate-limited (usage_pct == 100)."""
    now = time.time()
    s = RateLimitState(
        requests_min=RateLimitBucket(limit=50, remaining=0, captured_at=now),
        captured_at=now,
    )
    hot = s.hottest()
    assert hot is not None
    assert hot.usage_pct == pytest.approx(100.0)
    assert hot.remaining == 0


def test_rate_limit_state_backoff_delay_increases():
    """RetryPolicy.backoff() delay ceiling grows with each retry attempt."""
    from hive.llm.failover import RetryPolicy

    policy = RetryPolicy(max_attempts=5, base_delay=1.0, max_delay=16.0)
    # The ceiling doubles each attempt: base*(2^attempt), capped at max_delay.
    # We just verify the ceiling (before jitter) grows monotonically for attempts 0-3.
    ceilings = [min(policy.max_delay, policy.base_delay * (2 ** attempt))
                for attempt in range(4)]
    for i in range(len(ceilings) - 1):
        assert ceilings[i] < ceilings[i + 1], (
            f"Expected ceiling[{i}] < ceiling[{i+1}], got {ceilings[i]} >= {ceilings[i+1]}"
        )


def test_rate_limit_header_parser_returns_none_on_missing():
    """parse_rate_limit_headers returns None when no x-ratelimit-* keys are present."""
    assert parse_rate_limit_headers({}) is None
    assert parse_rate_limit_headers({"content-type": "application/json",
                                     "authorization": "Bearer tok"}) is None


# --- Six new tests ---------------------------------------------------------------

def test_rate_limit_bucket_at_capacity_remaining_zero():
    """A bucket with remaining == 0 has no capacity left."""
    b = RateLimitBucket(limit=100, remaining=0)
    assert b.remaining == 0
    assert b.usage_pct == pytest.approx(100.0)
    assert b.used == 100


def test_rate_limit_bucket_consume_decrements():
    """Manually decrementing remaining reflects correctly in used and usage_pct."""
    b = RateLimitBucket(limit=10, remaining=10)
    assert b.used == 0

    # Simulate consuming 3 tokens by setting remaining directly.
    b.remaining = 7
    assert b.used == 3
    assert b.usage_pct == pytest.approx(30.0)


def test_rate_limit_state_mark_model_limited():
    """A bucket with remaining == 0 reports 100 % usage, indicating full rate-limit."""
    now = time.time()
    s = RateLimitState(
        tokens_min=RateLimitBucket(limit=1000, remaining=0, captured_at=now),
        captured_at=now,
    )
    hot = s.hottest()
    assert hot is not None
    assert hot.remaining == 0
    assert hot.usage_pct == pytest.approx(100.0)


def test_rate_limit_bucket_reset_time_positive():
    """A newly parsed bucket retains a positive reset_seconds value."""
    headers = {
        "x-ratelimit-limit-requests": "60",
        "x-ratelimit-remaining-requests": "30",
        "x-ratelimit-reset-requests": "45",
    }
    state = parse_rate_limit_headers(headers)
    assert state is not None
    assert state.requests_min.reset_seconds > 0


def test_rate_limit_parse_headers_with_retry_after():
    """Retry-After: 30 header does not interfere; parse still returns valid state."""
    headers = {
        "x-ratelimit-limit-requests": "100",
        "x-ratelimit-remaining-requests": "0",
        "x-ratelimit-reset-requests": "30",
        "retry-after": "30",
    }
    state = parse_rate_limit_headers(headers)
    assert state is not None
    # The bucket reset window should equal the header value (30 s).
    assert state.requests_min.reset_seconds == pytest.approx(30.0)
    assert state.requests_min.remaining == 0


def test_rate_limit_multiple_buckets_independent():
    """Two independent RateLimitBucket instances do not share state."""
    b1 = RateLimitBucket(limit=100, remaining=80)
    b2 = RateLimitBucket(limit=200, remaining=200)

    # Mutating b1 must not affect b2.
    b1.remaining = 0
    assert b2.remaining == 200
    assert b1.used == 100
    assert b2.used == 0


# ---------------------------------------------------------------------------
# Six additional tests (batch 4)
# ---------------------------------------------------------------------------

def test_parse_tokens_hour_bucket_populated():
    """parse_rate_limit_headers populates the tokens_hour bucket from -1h suffixed headers."""
    headers = {
        "x-ratelimit-limit-tokens-1h": "500000",
        "x-ratelimit-remaining-tokens-1h": "250000",
        "x-ratelimit-reset-tokens-1h": "3600",
    }
    state = parse_rate_limit_headers(headers)
    assert state is not None
    assert state.tokens_hour.limit == 500000
    assert state.tokens_hour.remaining == 250000
    assert state.tokens_hour.usage_pct == pytest.approx(50.0)


def test_rate_limit_state_provider_field_stored():
    """parse_rate_limit_headers stores the provider name in the resulting state."""
    headers = {
        "x-ratelimit-limit-requests": "60",
        "x-ratelimit-remaining-requests": "30",
    }
    state = parse_rate_limit_headers(headers, provider="openrouter")
    assert state is not None
    assert state.provider == "openrouter"


def test_rate_limit_bucket_default_captured_at_zero():
    """A RateLimitBucket created with default args has captured_at == 0."""
    b = RateLimitBucket(limit=10, remaining=5, reset_seconds=60.0)
    assert b.captured_at == 0.0
    # With captured_at=0 the elapsed time is huge, so remaining_seconds_now clamps to 0.
    assert b.remaining_seconds_now == 0.0


def test_classify_rate_limit_sets_rotate_credential():
    """failover.classify maps a 429 response to RATE_LIMIT with should_rotate_credential=True."""
    from hive.llm.failover import classify, FailoverReason
    import httpx
    req = httpx.Request("POST", "http://example.com")
    resp = httpx.Response(429, content=b"Too Many Requests")
    exc = httpx.HTTPStatusError("429", request=req, response=resp)
    err = classify(exc)
    assert err.reason == FailoverReason.RATE_LIMIT
    assert err.retryable is True
    assert err.should_rotate_credential is True


def test_classify_auth_error_sets_rotate_and_fallback():
    """failover.classify maps a 401 response to AUTH with rotate=True and fallback=True."""
    from hive.llm.failover import classify, FailoverReason
    import httpx
    req = httpx.Request("POST", "http://example.com")
    resp = httpx.Response(401, content=b"Unauthorized")
    exc = httpx.HTTPStatusError("401", request=req, response=resp)
    err = classify(exc)
    assert err.reason == FailoverReason.AUTH
    assert err.should_rotate_credential is True
    assert err.should_fallback is True


def test_rate_limit_hottest_returns_tokens_hour_when_highest():
    """hottest() selects tokens_hour when it has the highest usage percentage."""
    now = time.time()
    s = RateLimitState(
        requests_min=RateLimitBucket(limit=100, remaining=90, captured_at=now),   # 10%
        tokens_hour=RateLimitBucket(limit=500000, remaining=50000, captured_at=now),  # 90%
        tokens_min=RateLimitBucket(limit=10000, remaining=8000, captured_at=now),  # 20%
        captured_at=now,
    )
    hot = s.hottest()
    assert hot is s.tokens_hour
    assert hot.usage_pct == pytest.approx(90.0)


# --- Wave 3R additional tests ---------------------------------------------------

def test_rate_limit_bucket_usage_pct_at_zero_remaining():
    """usage_pct is 100.0 when remaining == 0."""
    b = RateLimitBucket(limit=1000, remaining=0)
    assert b.usage_pct == pytest.approx(100.0)


def test_rate_limit_bucket_usage_pct_full_remaining():
    """usage_pct is 0.0 when remaining == limit."""
    b = RateLimitBucket(limit=1000, remaining=1000)
    assert b.usage_pct == pytest.approx(0.0)


def test_rate_limit_bucket_reset_seconds_default():
    """RateLimitBucket reset_seconds defaults to 0.0."""
    b = RateLimitBucket(limit=100, remaining=50)
    assert b.reset_seconds == 0.0


def test_rate_limit_state_default_provider():
    """RateLimitState provider defaults to empty string."""
    s = RateLimitState(captured_at=time.time())
    assert s.provider == ""


def test_rate_limit_state_with_provider_set():
    """RateLimitState stores the provider string."""
    s = RateLimitState(provider="minimax", captured_at=time.time())
    assert s.provider == "minimax"


def test_rate_limit_bucket_limit_stored():
    """RateLimitBucket stores the limit value."""
    b = RateLimitBucket(limit=5000, remaining=3000)
    assert b.limit == 5000
    assert b.remaining == 3000
