"""M1 #152 — enforced, restart-safe daily USD spend cap regressions."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest

from hive.autonomy.budget_alert import BudgetAlert
from hive.core.budgeter import Budgeter
from hive.core.config import HiveConfig
from hive.core.events import EventBus, EventType
from hive.core.types import Message, Role
from hive.llm.adapters.base import CompletionRequest, CompletionResult
from hive.llm.credential_pool import CredentialPool
from hive.llm.failover import RetryPolicy
from hive.llm.pricing import rate_for
from hive.llm.router import BudgetError, ModelRouter, ProviderError
from hive.observability.persistence import ObservabilityLedger
from hive.observability.telemetry import Telemetry
from hive.runtime import HiveOS


class _Router:
    async def aclose(self) -> None:
        pass


class _Telegram:
    def __init__(self) -> None:
        self.messages = []

    async def send(self, message) -> object:
        self.messages.append(message)
        return type("Result", (), {"ok": True})()


class _FlakyTelegram(_Telegram):
    async def send(self, message) -> object:
        self.messages.append(message)
        return type("Result", (), {"ok": len(self.messages) > 1})()


class _CountingAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: CompletionRequest, *, api_key: str) -> CompletionResult:
        self.calls += 1
        return CompletionResult(text="ok", model=request.model)


class _TimeoutAdapter(_CountingAdapter):
    async def complete(self, request: CompletionRequest, *, api_key: str) -> CompletionResult:
        self.calls += 1
        raise httpx.ReadTimeout("provider response was lost")


class _AuthFailureAdapter(_CountingAdapter):
    async def complete(self, request: CompletionRequest, *, api_key: str) -> CompletionResult:
        self.calls += 1
        response = httpx.Response(401, request=httpx.Request("POST", "https://provider.test"))
        raise httpx.HTTPStatusError("invalid credential", request=response.request, response=response)


class _CancelledAdapter(_CountingAdapter):
    async def complete(self, request: CompletionRequest, *, api_key: str) -> CompletionResult:
        self.calls += 1
        raise asyncio.CancelledError()


class _StreamingAdapter(_CountingAdapter):
    async def astream(self, request: CompletionRequest, *, api_key: str):
        self.calls += 1
        yield "streamed response"


class _StreamFailureThenCompleteAdapter(_CountingAdapter):
    async def astream(self, request: CompletionRequest, *, api_key: str):
        self.calls += 1
        raise httpx.ReadTimeout("stream response was lost")
        yield ""  # pragma: no cover - keeps this an async generator


class _Hive:
    def __init__(self, budgeter: Budgeter) -> None:
        self.budgeter = budgeter
        self.config = type(
            "Config",
            (),
            {
                "budget_forecast_alert_days": 1,
                "telegram_token": "",
                "telegram_admin_chat_id": "admin",
            },
        )()


def test_daily_usd_cap_blocks_a_persisted_total_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DAILY_SPEND_CAP_USD", "1.00")
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    first = HiveOS.build(config, router=_Router())
    first.events.publish(EventType.INFERENCE_END, {
        "model": "MiniMax-M3", "input_tokens": 10, "output_tokens": 10, "cost_usd": 1.00,
    })
    asyncio.run(first.aclose())

    restored = HiveOS.build(config, router=_Router())
    allowed, reason = restored.budgeter.gate()
    assert allowed is False
    assert "USD spend cap" in reason
    assert restored.budgeter.daily_spend_status()["hard_cap_reached"] is True
    asyncio.run(restored.aclose())


def test_soft_spend_threshold_sends_one_alert_before_the_hard_stop():
    budgeter = Budgeter(daily_spend_cap_usd=10.00)
    budgeter.record_usage({"model": "MiniMax-M3", "input_tokens": 1,
                           "output_tokens": 1, "cost_usd": 8.00})
    telegram = _Telegram()
    alert = BudgetAlert(_Hive(budgeter), telegram=telegram, chat_id="admin")

    assert asyncio.run(alert.check()) is True
    assert len(telegram.messages) == 1
    assert "80.00%" in telegram.messages[0].text
    assert asyncio.run(alert.check()) is False


def test_soft_spend_alert_retries_after_a_temporary_delivery_failure():
    budgeter = Budgeter(daily_spend_cap_usd=10.00)
    budgeter.record_usage({"model": "MiniMax-M3", "input_tokens": 1,
                           "output_tokens": 1, "cost_usd": 8.00})
    telegram = _FlakyTelegram()
    alert = BudgetAlert(_Hive(budgeter), telegram=telegram, chat_id="admin")

    assert asyncio.run(alert.check()) is False
    assert asyncio.run(alert.check()) is True
    assert len(telegram.messages) == 2


def test_soft_spend_alert_can_fire_again_after_usage_returns_to_ok():
    budgeter = Budgeter(daily_spend_cap_usd=10.00)
    budgeter.record_usage({"model": "MiniMax-M3", "input_tokens": 1,
                           "output_tokens": 1, "cost_usd": 8.00})
    telegram = _Telegram()
    alert = BudgetAlert(_Hive(budgeter), telegram=telegram, chat_id="admin")

    assert asyncio.run(alert.check()) is True
    budgeter._cost_today_usd = 0.0
    assert asyncio.run(alert.check()) is False
    budgeter._cost_today_usd = 8.0
    assert asyncio.run(alert.check()) is True
    assert len(telegram.messages) == 2


def test_router_emits_budget_block_without_attempting_an_over_cap_request(tmp_path):
    bus = EventBus(record_history=True)
    budgeter = Budgeter(daily_spend_cap_usd=1.00, initial_usage={"cost_usd": 1.00})
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    router = ModelRouter(config=config, budget=budgeter.gate, events=bus)

    with pytest.raises(BudgetError, match="USD spend cap"):
        asyncio.run(router.complete([Message(role=Role.USER, content="hello")]))

    assert [event.event_type for event in bus.history()] == [EventType.BUDGET_BLOCK]


def test_durable_reservations_atomically_block_an_over_cap_request(tmp_path):
    ledger = ObservabilityLedger(tmp_path / "state.sqlite")
    ledger.record_inference({"model": "m", "input_tokens": 1, "output_tokens": 1,
                             "cost_usd": 0.75})
    first = ledger.reserve_spend(amount_usd=0.20, cap_usd=1.00)
    assert first
    assert ledger.reserve_spend(amount_usd=0.10, cap_usd=1.00) is None

    ledger.record_inference({"model": "m", "input_tokens": 1, "output_tokens": 1,
                             "cost_usd": 0.10, "spend_reservation_id": first})
    assert ledger.reserve_spend(amount_usd=0.14, cap_usd=1.00)
    ledger.close()


def test_pending_reservation_survives_a_restart_and_blocks_new_spend(tmp_path):
    state_db = tmp_path / "state.sqlite"
    ledger = ObservabilityLedger(state_db)
    assert ledger.reserve_spend(amount_usd=1.00, cap_usd=1.00)
    ledger.close()

    restored = ObservabilityLedger(state_db)
    assert restored.reserve_spend(amount_usd=0.01, cap_usd=1.00) is None
    restored.close()


def test_router_preflight_blocks_before_calling_the_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_EXEC_MODEL", "M-exec")
    monkeypatch.setenv("HIVE_EXEC_FALLBACK_MODEL", "M-exec")
    monkeypatch.setenv("HIVE_PRICE_M_EXEC_IN", "0")
    monkeypatch.setenv("HIVE_PRICE_M_EXEC_OUT", "1000000")
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    ledger = ObservabilityLedger(tmp_path / "state.sqlite")
    ledger.record_inference({"model": "M-exec", "input_tokens": 1, "output_tokens": 1,
                             "cost_usd": 0.50})
    adapter = _CountingAdapter()
    router = ModelRouter(
        config=config,
        adapter=adapter,
        credential_pool=CredentialPool(["test-key"]),
        budget=lambda: (True, ""),
        spend_reserve=lambda amount: ledger.reserve_spend(amount_usd=amount, cap_usd=1.00),
        spend_release=ledger.release_spend_reservation,
    )

    with pytest.raises(BudgetError, match="would exceed"):
        asyncio.run(router.complete([Message(role=Role.USER, content="hello")], max_tokens=1))

    assert adapter.calls == 0
    ledger.close()


def test_ledger_write_failure_keeps_the_reservation_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_EXEC_MODEL", "M-exec")
    monkeypatch.setenv("HIVE_EXEC_FALLBACK_MODEL", "M-exec")
    monkeypatch.setenv("HIVE_PRICE_M_EXEC_IN", "0")
    monkeypatch.setenv("HIVE_PRICE_M_EXEC_OUT", "1000000")
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    state_db = tmp_path / "state.sqlite"
    ledger = ObservabilityLedger(state_db)
    events = EventBus()
    Telemetry(ledger=ledger).attach(events)
    adapter = _CountingAdapter()
    router = ModelRouter(
        config=config,
        adapter=adapter,
        credential_pool=CredentialPool(["test-key"]),
        budget=lambda: (True, ""),
        events=events,
        spend_reserve=lambda amount: ledger.reserve_spend(amount_usd=amount, cap_usd=1.00),
        spend_release=ledger.release_spend_reservation,
    )

    def fail_record(_data):
        raise OSError("disk full")

    monkeypatch.setattr(ledger, "record_inference", fail_record)
    asyncio.run(router.complete([Message(role=Role.USER, content="hello")], max_tokens=1))
    assert adapter.calls == 1
    ledger.close()

    restored = ObservabilityLedger(state_db)
    assert restored.reserve_spend(amount_usd=0.01, cap_usd=1.00) is None
    restored.close()


def test_router_keeps_a_reservation_after_an_ambiguous_provider_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_EXEC_MODEL", "M-exec")
    monkeypatch.setenv("HIVE_EXEC_FALLBACK_MODEL", "M-exec")
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    adapter = _TimeoutAdapter()
    releases: list[str] = []
    router = ModelRouter(
        config=config,
        adapter=adapter,
        credential_pool=CredentialPool(["test-key"]),
        retry=RetryPolicy(max_attempts=1, base_delay=0.0, max_delay=0.0),
        budget=lambda: (True, ""),
        spend_reserve=lambda _amount: "reservation-1",
        spend_release=releases.append,
    )

    with pytest.raises(ProviderError):
        asyncio.run(router.complete([Message(role=Role.USER, content="hello")]))

    assert adapter.calls == 1
    assert releases == []


def test_router_releases_a_reservation_after_a_confirmed_auth_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_EXEC_MODEL", "M-exec")
    monkeypatch.setenv("HIVE_EXEC_FALLBACK_MODEL", "M-exec")
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    releases: list[str] = []
    router = ModelRouter(
        config=config,
        adapter=_AuthFailureAdapter(),
        credential_pool=CredentialPool(["test-key"]),
        retry=RetryPolicy(max_attempts=1, base_delay=0.0, max_delay=0.0),
        budget=lambda: (True, ""),
        spend_reserve=lambda _amount: "reservation-1",
        spend_release=releases.append,
    )

    with pytest.raises(ProviderError):
        asyncio.run(router.complete([Message(role=Role.USER, content="hello")]))

    assert releases == ["reservation-1"]


def test_router_keeps_a_reservation_after_provider_cancellation(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_EXEC_MODEL", "M-exec")
    monkeypatch.setenv("HIVE_EXEC_FALLBACK_MODEL", "M-exec")
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    adapter = _CancelledAdapter()
    releases: list[str] = []
    router = ModelRouter(
        config=config,
        adapter=adapter,
        credential_pool=CredentialPool(["test-key"]),
        budget=lambda: (True, ""),
        spend_reserve=lambda _amount: "reservation-1",
        spend_release=releases.append,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(router.complete([Message(role=Role.USER, content="hello")]))

    assert adapter.calls == 1
    assert releases == []


def test_streaming_records_the_conservative_input_ceiling(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_EXEC_MODEL", "M-exec")
    monkeypatch.setenv("HIVE_EXEC_FALLBACK_MODEL", "M-exec")
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    events = EventBus(record_history=True)
    router = ModelRouter(
        config=config,
        adapter=_StreamingAdapter(),
        credential_pool=CredentialPool(["test-key"]),
        budget=lambda: (True, ""),
        events=events,
    )

    async def consume() -> list[str]:
        return [chunk async for chunk in router.stream(
            [Message(role=Role.USER, content="hello")]
        )]

    assert asyncio.run(consume()) == ["streamed response"]
    event = [event for event in events.history() if event.event_type is EventType.INFERENCE_END][0]
    assert event.data["input_tokens"] >= 512
    assert event.data["output_tokens"] == 4_096
    assert event.data["cost_usd"] > 0


def test_stream_fallback_receives_its_own_reservation(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_EXEC_MODEL", "M-exec")
    monkeypatch.setenv("HIVE_EXEC_FALLBACK_MODEL", "M-exec")
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    adapter = _StreamFailureThenCompleteAdapter()
    reservations: list[str] = []

    def reserve(_amount: float) -> str:
        reservation = f"reservation-{len(reservations) + 1}"
        reservations.append(reservation)
        return reservation

    router = ModelRouter(
        config=config,
        adapter=adapter,
        credential_pool=CredentialPool(["test-key", "next-key"]),
        budget=lambda: (True, ""),
        spend_reserve=reserve,
    )

    async def consume() -> list[str]:
        return [chunk async for chunk in router.stream(
            [Message(role=Role.USER, content="hello")]
        )]

    assert asyncio.run(consume()) == ["ok"]
    assert adapter.calls == 2
    assert reservations == ["reservation-1", "reservation-2"]


def test_cap_enabled_build_disables_the_global_mnemosyne_host_llm(tmp_path, monkeypatch):
    import hive.runtime as runtime
    from hive.memory.mnemosyne_provider import HiveMnemosyneProvider

    monkeypatch.setenv("HIVE_DAILY_SPEND_CAP_USD", "1.00")
    inner = MagicMock()
    provider = HiveMnemosyneProvider(inner)
    monkeypatch.setattr(runtime, "build_mnemosyne_provider", lambda **_kwargs: provider)
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)

    hive = HiveOS.build(config, router=_Router())
    sync_backend = inner.set_host_llm_backend.call_args.args[0]
    assert sync_backend("consolidate this") is None
    asyncio.run(hive.aclose())


@pytest.mark.parametrize("invalid_price", ["nan", "inf", "-1"])
def test_invalid_price_override_keeps_a_finite_nonnegative_reservation_rate(monkeypatch,
                                                                              invalid_price):
    monkeypatch.setenv("HIVE_PRICE_M_EXEC_OUT", invalid_price)
    assert rate_for("M-exec")[1] == pytest.approx(1.20)


def test_spend_cap_resets_when_the_local_day_changes():
    now = [1_700_000_000.0]
    budgeter = Budgeter(daily_spend_cap_usd=1.00, clock=lambda: now[0],
                        initial_usage={"cost_usd": 1.00})
    assert budgeter.gate()[0] is False

    now[0] += 86_400.0
    assert budgeter.gate() == (True, "")
    assert budgeter.daily_spend_status()["hard_cap_reached"] is False


@pytest.mark.parametrize("raw_cap", ["inf", "nan"])
def test_config_rejects_a_nonfinite_daily_spend_cap(tmp_path, monkeypatch, raw_cap):
    monkeypatch.setenv("HIVE_DAILY_SPEND_CAP_USD", raw_cap)
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    assert any("finite value" in issue for issue in config.validate())
    with pytest.raises(RuntimeError, match="HIVE_DAILY_SPEND_CAP_USD"):
        HiveOS.build(config, router=_Router())
