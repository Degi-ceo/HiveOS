"""M1 resilience tests — rate-limit parsing, proactive cooldown, cost budgeter,
hardened codex planner. All offline (no network)."""
from __future__ import annotations

import asyncio

import pytest

from hive.llm.rate_limit import RateLimitState, parse_rate_limit_headers
from hive.llm.pricing import cost_usd, rate_for
from hive.core.budgeter import Budgeter


# --- rate-limit parsing --------------------------------------------------------

def test_parse_returns_none_without_headers():
    assert parse_rate_limit_headers({"content-type": "application/json"}) is None


def test_parse_reads_buckets_case_insensitive():
    state = parse_rate_limit_headers({
        "X-RateLimit-Limit-Requests": "100",
        "x-ratelimit-remaining-requests": "10",
        "x-ratelimit-reset-requests": "30",
        "x-ratelimit-limit-tokens": "1000000",
        "x-ratelimit-remaining-tokens": "999000",
    }, provider="minimax")
    assert state is not None
    assert state.provider == "minimax"
    assert state.requests_min.limit == 100
    assert state.requests_min.remaining == 10
    assert state.requests_min.usage_pct == 90.0
    assert state.tokens_min.limit == 1_000_000


def test_hottest_picks_highest_usage():
    state = parse_rate_limit_headers({
        "x-ratelimit-limit-requests": "100",
        "x-ratelimit-remaining-requests": "5",       # 95% used
        "x-ratelimit-limit-tokens": "1000",
        "x-ratelimit-remaining-tokens": "900",       # 10% used
    })
    hottest = state.hottest()
    assert hottest is not None and hottest.usage_pct == 95.0


def test_hottest_none_when_no_limits():
    assert RateLimitState().hottest() is None


# --- pricing -------------------------------------------------------------------

def test_cost_usd_known_model():
    # MiniMax-M3 default: 0.30 in / 1.20 out per million
    c = cost_usd("MiniMax-M3", 1_000_000, 1_000_000)
    assert c == pytest.approx(1.50)


def test_cost_usd_unknown_model_uses_fallback():
    assert cost_usd("Totally-New-Model", 1_000_000, 0) == pytest.approx(0.30)


def test_rate_for_env_override(monkeypatch):
    monkeypatch.setenv("HIVE_PRICE_MINIMAX_M3_IN", "9.0")
    rin, _ = rate_for("MiniMax-M3")
    assert rin == 9.0


# --- budgeter cost tracking ----------------------------------------------------

def test_record_usage_accrues_cost_and_tokens():
    # Budgeter is a pure accumulator: cost arrives in the payload (router computes it).
    b = Budgeter()
    b.record_usage({"model": "MiniMax-M3", "input_tokens": 1_000_000,
                    "output_tokens": 1_000_000, "cost_usd": 1.50})
    snap = b.snapshot()
    assert snap["cost_today_usd"] == pytest.approx(1.50)
    assert snap["tokens_today"] == {"input": 1_000_000, "output": 1_000_000}
    assert snap["by_model"]["MiniMax-M3"]["cost_usd"] == pytest.approx(1.50)


def test_record_usage_ignores_empty():
    b = Budgeter()
    b.record_usage({"model": "MiniMax-M3", "input_tokens": 0, "output_tokens": 0})
    assert b.snapshot()["cost_today_usd"] == 0.0


def test_record_usage_accepts_event_object():
    class _Evt:
        data = {"model": "MiniMax-M3", "input_tokens": 1_000_000,
                "output_tokens": 0, "cost_usd": 0.30}
    b = Budgeter()
    b.record_usage(_Evt())
    assert b.snapshot()["cost_today_usd"] == pytest.approx(0.30)


# --- router proactive cooldown -------------------------------------------------

from hive.core.config import HiveConfig
from hive.llm.adapters.base import CompletionResult, LLMAdapter, Usage
from hive.llm.credential_pool import CredentialPool
from hive.llm.failover import RetryPolicy
from hive.llm.router import ModelRouter, PlannerError, make_codex_planner


def _config(tmp_path):
    return HiveConfig.from_env(root=tmp_path, load_dotenv=False)


class _RLAdapter(LLMAdapter):
    """Adapter that returns a result carrying a near-exhausted rate-limit state."""
    name = "rl"

    def __init__(self, usage_pct: float):
        state = parse_rate_limit_headers({
            "x-ratelimit-limit-requests": "100",
            "x-ratelimit-remaining-requests": str(int(100 - usage_pct)),
            "x-ratelimit-reset-requests": "60",
        })
        self._state = state

    async def complete(self, request, *, api_key):
        return CompletionResult(text="ok", model=request.model,
                                usage=Usage(input_tokens=5, output_tokens=5),
                                raw={"rate_limit_state": self._state})


def test_router_cools_credential_when_rate_limit_hot(tmp_path):
    pool = CredentialPool(["k0", "k1"], cooldown_seconds=100.0)
    router = ModelRouter(config=_config(tmp_path), adapter=_RLAdapter(usage_pct=95),
                         credential_pool=pool,
                         retry=RetryPolicy(max_attempts=1, base_delay=0, max_delay=0))
    msgs = []
    from hive.core.types import Message, Role
    msgs = [Message(role=Role.USER, content="hi")]
    asyncio.run(router.complete(msgs))
    # one key should now be cooling (only one available)
    assert len(pool.available()) == 1


def test_pool_cooldown_does_not_bump_failures():
    pool = CredentialPool(["a"], clock=lambda: 0.0)
    cred = pool.acquire()
    pool.cooldown(cred, 30.0)
    assert cred.failures == 0           # healthy key — not a failure
    assert cred.cooldown_until == 30.0  # parked for the window


def test_router_does_not_cool_when_rate_limit_cool(tmp_path):
    pool = CredentialPool(["k0", "k1"], cooldown_seconds=100.0)
    router = ModelRouter(config=_config(tmp_path), adapter=_RLAdapter(usage_pct=10),
                         credential_pool=pool,
                         retry=RetryPolicy(max_attempts=1, base_delay=0, max_delay=0))
    from hive.core.types import Message, Role
    asyncio.run(router.complete([Message(role=Role.USER, content="hi")]))
    assert len(pool.available()) == 2  # neither cooled


# --- hardened codex planner ----------------------------------------------------

def test_codex_planner_success(tmp_path):
    # `cat` echoes stdin back -> non-empty output, exit 0
    planner = make_codex_planner("cat", timeout=10)
    from hive.core.types import Message, Role
    out = asyncio.run(planner([Message(role=Role.USER, content="plan this")], None))
    assert "plan this" in out


def test_codex_planner_nonzero_exit_raises():
    planner = make_codex_planner("false", timeout=10)  # exits 1, no output
    from hive.core.types import Message, Role
    with pytest.raises(PlannerError):
        asyncio.run(planner([Message(role=Role.USER, content="x")], None))


def test_codex_planner_missing_binary_raises():
    planner = make_codex_planner("definitely-not-a-real-binary-xyz", timeout=5)
    from hive.core.types import Message, Role
    with pytest.raises(PlannerError):
        asyncio.run(planner([Message(role=Role.USER, content="x")], None))


def test_codex_planner_timeout_raises():
    planner = make_codex_planner("sleep 5", timeout=0.2)
    from hive.core.types import Message, Role
    with pytest.raises(PlannerError):
        asyncio.run(planner([Message(role=Role.USER, content="x")], None))


def test_router_plan_falls_back_to_executor_on_planner_error(tmp_path):
    """A failing planner must not dead-end a PLAN turn; the executor answers."""
    from hive.core.types import Message, Role
    from hive.llm.router import TaskKind

    async def broken_planner(messages, system):
        raise PlannerError("codex down")

    class _ExecAdapter(LLMAdapter):
        name = "exec"
        async def complete(self, request, *, api_key):
            return CompletionResult(text="executor plan", model=request.model,
                                    usage=Usage(input_tokens=1, output_tokens=1))

    router = ModelRouter(config=_config(tmp_path), adapter=_ExecAdapter(),
                         credential_pool=CredentialPool(["k"]),
                         retry=RetryPolicy(max_attempts=1, base_delay=0, max_delay=0),
                         planner=broken_planner)
    out = asyncio.run(router.complete([Message(role=Role.USER, content="plan")],
                                      TaskKind.PLAN))
    assert out.text == "executor plan"
