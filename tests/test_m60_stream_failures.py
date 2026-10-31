"""A streamed terminal failure remains a failed run after the client closes."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from hive.agents.orchestrator import ConversationOrchestrator
from hive.core.config import HiveConfig
from hive.llm.router import BudgetError, ProviderError
from hive.runtime import HiveOS
from hive.surfaces.cli import _terminal_turn


class _UnusedRouter:
    async def aclose(self) -> None:
        return None


def _hive(tmp_path) -> HiveOS:
    return HiveOS.build(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False), router=_UnusedRouter(),
    )


def test_terminal_provider_error_is_failed_run_and_durable_incident(tmp_path, monkeypatch, capsys):
    hive = _hive(tmp_path)

    async def failed_stream(*_args, **_kwargs):
        yield {
            "type": "error", "class": "ProviderError", "reason": "billing",
            "detail": "private provider body",
        }

    monkeypatch.setattr(hive.orchestrator, "stream_ask", failed_stream)
    try:
        assert asyncio.run(_terminal_turn(hive, "test", session_id="cli-test")) == 1
        run = hive.run_ledger.recent(session_id="cli-test", limit=1)[0]
        assert run["state"] == "error"
        assert "private provider body" not in str(run)
        assert hive.run_ledger.public_events(run["run_id"])[0]["data"]["reason"] == "billing"
        incidents = hive.incident_ledger.recent()
        assert len(incidents) == 1
        assert incidents[0]["run_id"] == run["run_id"]
        assert "billing" in incidents[0]["summary"]
        assert "private provider body" not in str(incidents)
        assert "billing" in capsys.readouterr().out
    finally:
        asyncio.run(hive.aclose())


def test_drained_error_event_still_finishes_failed(tmp_path, monkeypatch):
    hive = _hive(tmp_path)

    async def failed_stream(*_args, **_kwargs):
        yield {"type": "error", "class": "ProviderError", "reason": "network"}

    monkeypatch.setattr(hive.orchestrator, "stream_ask", failed_stream)

    async def drain():
        return [event async for event in hive.stream_ask_iterations("test", session_id="drained")]

    try:
        assert [event["type"] for event in asyncio.run(drain())] == ["error"]
        assert hive.run_ledger.recent(session_id="drained", limit=1)[0]["state"] == "error"
        assert len(hive.incident_ledger.recent()) == 1
    finally:
        asyncio.run(hive.aclose())


def test_preterminal_close_is_cancellation_without_incident(tmp_path, monkeypatch):
    hive = _hive(tmp_path)

    async def unfinished_stream(*_args, **_kwargs):
        yield {"type": "model_decision", "turn": 1, "tool_calls": []}
        yield {"type": "final", "turn": 1, "text": "never delivered"}

    monkeypatch.setattr(hive.orchestrator, "stream_ask", unfinished_stream)

    async def close_early():
        stream = hive.stream_ask_iterations("test", session_id="early-close")
        assert (await anext(stream))["type"] == "model_decision"
        await stream.aclose()

    try:
        asyncio.run(close_early())
        assert hive.run_ledger.recent(session_id="early-close", limit=1)[0]["state"] == "cancelled"
        assert hive.incident_ledger.recent() == []
    finally:
        asyncio.run(hive.aclose())


def test_expected_budget_refusal_has_no_incident(tmp_path, monkeypatch):
    hive = _hive(tmp_path)

    async def refused_stream(*_args, **_kwargs):
        yield {"type": "error", "class": "BudgetError", "reason": "refused"}

    monkeypatch.setattr(hive.orchestrator, "stream_ask", refused_stream)

    async def drain():
        return [event async for event in hive.stream_ask_iterations("test", session_id="budget")]

    try:
        assert [event["type"] for event in asyncio.run(drain())] == ["error"]
        run = hive.run_ledger.recent(session_id="budget", limit=1)[0]
        assert run["state"] == "error"
        assert run["error"] == "budget_refused"
        hive.reconcile_incidents()
        assert hive.incident_ledger.recent() == []
    finally:
        asyncio.run(hive.aclose())


def test_untrusted_provider_reason_cannot_enter_run_or_incident(tmp_path, monkeypatch):
    hive = _hive(tmp_path)

    async def failed_stream(*_args, **_kwargs):
        yield {"type": "error", "class": "ProviderError", "reason": "billing private token"}

    monkeypatch.setattr(hive.orchestrator, "stream_ask", failed_stream)

    async def drain():
        return [event async for event in hive.stream_ask_iterations("test", session_id="untrusted")]

    try:
        assert asyncio.run(drain())[0]["reason"] == "unknown"
        run = hive.run_ledger.recent(session_id="untrusted", limit=1)[0]
        assert run["state"] == "error"
        assert run["error"] == "provider:unknown"
        assert hive.incident_ledger.recent()[0]["summary"] == "provider:unknown"
        assert "private token" not in str(hive.incident_ledger.recent())
    finally:
        asyncio.run(hive.aclose())


def test_unexpected_stream_exception_is_failed_and_incident_is_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_TEST_SECRET", "private-runtime-secret")
    hive = _hive(tmp_path)

    async def failed_stream(*_args, **_kwargs):
        raise RuntimeError("private-runtime-secret")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(hive.orchestrator, "stream_ask", failed_stream)

    async def drain():
        return [event async for event in hive.stream_ask_iterations("test", session_id="exception")]

    try:
        with pytest.raises(RuntimeError):
            asyncio.run(drain())
        run = hive.run_ledger.recent(session_id="exception", limit=1)[0]
        assert run["state"] == "error"
        assert "private-runtime-secret" not in str(run)
        incident = hive.incident_ledger.recent()[0]
        assert incident["summary"] == "RuntimeError"
        assert "private-runtime-secret" not in str(incident)
    finally:
        asyncio.run(hive.aclose())


def test_reconciliation_does_not_duplicate_stream_incident_after_restart(tmp_path, monkeypatch):
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(config, router=_UnusedRouter())

    async def failed_stream(*_args, **_kwargs):
        yield {"type": "error", "class": "ProviderError", "reason": "billing"}

    monkeypatch.setattr(hive.orchestrator, "stream_ask", failed_stream)

    async def drain():
        return [event async for event in hive.stream_ask_iterations("test", session_id="restart")]

    try:
        asyncio.run(drain())
        incident_id = hive.incident_ledger.recent()[0]["incident_id"]
        assert len(hive.incident_ledger.get(incident_id)["events"]) == 1
    finally:
        asyncio.run(hive.aclose())

    restarted = HiveOS.build(config, router=_UnusedRouter())
    try:
        restarted.reconcile_incidents()
        restarted.reconcile_incidents()
        assert len(restarted.incident_ledger.recent()) == 1
        assert len(restarted.incident_ledger.get(incident_id)["events"]) == 1
    finally:
        asyncio.run(restarted.aclose())


def test_incident_write_failure_does_not_mask_terminal_provider_error(tmp_path, monkeypatch, caplog):
    hive = _hive(tmp_path)

    async def failed_stream(*_args, **_kwargs):
        yield {"type": "error", "class": "ProviderError", "reason": "billing"}

    def failed_record(*_args, **_kwargs):
        raise OSError("private database detail")

    monkeypatch.setattr(hive.orchestrator, "stream_ask", failed_stream)
    monkeypatch.setattr(hive.incident_ledger, "record", failed_record)
    try:
        assert asyncio.run(_terminal_turn(hive, "test", session_id="db-failure")) == 1
        assert hive.run_ledger.recent(session_id="db-failure", limit=1)[0]["state"] == "error"
        assert "private database detail" not in caplog.text
    finally:
        asyncio.run(hive.aclose())


def test_direct_turn_budget_refusal_stays_out_of_reconciliation(tmp_path, monkeypatch):
    hive = _hive(tmp_path)

    async def refused(*_args, **_kwargs):
        raise BudgetError("daily spend cap", reason_code="refused")

    monkeypatch.setattr(hive.orchestrator, "ask", refused)
    try:
        with pytest.raises(BudgetError):
            asyncio.run(hive.ask("test", session_id="direct-budget"))
        run = hive.run_ledger.recent(session_id="direct-budget", limit=1)[0]
        assert run["state"] == "error"
        assert run["error"] == "budget_refused"
        hive.reconcile_incidents()
        assert hive.incident_ledger.recent() == []
    finally:
        asyncio.run(hive.aclose())


def test_direct_turn_retains_original_error_when_incident_write_fails(tmp_path, monkeypatch, caplog):
    hive = _hive(tmp_path)

    async def failed(*_args, **_kwargs):
        raise RuntimeError("original conversation failure")

    def failed_record(*_args, **_kwargs):
        raise OSError("private database detail")

    monkeypatch.setattr(hive.orchestrator, "ask", failed)
    monkeypatch.setattr(hive.incident_ledger, "record", failed_record)
    try:
        with pytest.raises(RuntimeError, match="original conversation failure"):
            asyncio.run(hive.ask("test", session_id="direct-failure"))
        assert hive.run_ledger.recent(session_id="direct-failure", limit=1)[0]["state"] == "error"
        assert "private database detail" not in caplog.text
    finally:
        asyncio.run(hive.aclose())


def test_token_stream_budget_refusal_stays_out_of_reconciliation(tmp_path, monkeypatch):
    hive = _hive(tmp_path)

    async def refused(*_args, **_kwargs):
        raise BudgetError("daily spend cap", reason_code="refused")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(hive.router, "stream", refused, raising=False)

    async def drain():
        return [chunk async for chunk in hive.ask_stream("test", session_id="token-budget")]

    try:
        with pytest.raises(BudgetError):
            asyncio.run(drain())
        run = hive.run_ledger.recent(session_id="token-budget", limit=1)[0]
        assert run["state"] == "error"
        assert run["error"] == "budget_refused"
        hive.reconcile_incidents()
        assert hive.incident_ledger.recent() == []
    finally:
        asyncio.run(hive.aclose())


def _billing_failure() -> ProviderError:
    request = httpx.Request("POST", "https://provider.example/messages")
    response = httpx.Response(402, request=request, text="private provider response")
    cause = httpx.HTTPStatusError("private provider response", request=request, response=response)
    failure = ProviderError("billing: private provider response")
    failure.__cause__ = cause
    return failure


def test_direct_provider_body_stays_private_after_incident_write_failure_and_restart(
    tmp_path, monkeypatch,
):
    config = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(config, router=_UnusedRouter())

    async def failed(*_args, **_kwargs):
        raise _billing_failure()

    def failed_record(*_args, **_kwargs):
        raise OSError("private database detail")

    monkeypatch.setattr(hive.orchestrator, "ask", failed)
    monkeypatch.setattr(hive.incident_ledger, "record", failed_record)
    try:
        with pytest.raises(ProviderError):
            asyncio.run(hive.ask("test", session_id="direct-provider"))
        run = hive.run_ledger.recent(session_id="direct-provider", limit=1)[0]
        assert run["state"] == "error"
        assert run["error"] == "provider:billing"
        assert hive.incident_ledger.recent() == []
    finally:
        asyncio.run(hive.aclose())

    restarted = HiveOS.build(config, router=_UnusedRouter())
    try:
        restarted.reconcile_incidents()
        incidents = restarted.incident_ledger.recent()
        assert len(incidents) == 1
        assert incidents[0]["summary"] == "provider:billing"
        assert incidents[0]["run_id"] == run["run_id"]
        assert "private provider response" not in str(incidents)
        assert "private database detail" not in str(incidents)
    finally:
        asyncio.run(restarted.aclose())


def test_token_stream_provider_body_stays_private_after_reconciliation(tmp_path, monkeypatch):
    hive = _hive(tmp_path)

    async def failed(*_args, **_kwargs):
        raise _billing_failure()
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(hive.router, "stream", failed, raising=False)

    async def drain():
        return [chunk async for chunk in hive.ask_stream("test", session_id="token-provider")]

    try:
        with pytest.raises(ProviderError):
            asyncio.run(drain())
        run = hive.run_ledger.recent(session_id="token-provider", limit=1)[0]
        assert run["state"] == "error"
        assert run["error"] == "provider:billing"
        hive.reconcile_incidents()
        incidents = hive.incident_ledger.recent()
        assert len(incidents) == 1
        assert incidents[0]["summary"] == "provider:billing"
        assert "private provider response" not in str(incidents)
    finally:
        asyncio.run(hive.aclose())


def test_spend_ledger_failure_is_an_incident_not_a_budget_refusal(tmp_path, monkeypatch):
    hive = _hive(tmp_path)

    async def failed_stream(*_args, **_kwargs):
        yield {"type": "error", "class": "BudgetError", "reason": "ledger_unavailable"}

    monkeypatch.setattr(hive.orchestrator, "stream_ask", failed_stream)

    async def drain():
        return [event async for event in hive.stream_ask_iterations("test", session_id="ledger-failure")]

    try:
        events = asyncio.run(drain())
        assert events[0]["reason"] == "ledger_unavailable"
        run = hive.run_ledger.recent(session_id="ledger-failure", limit=1)[0]
        assert run["state"] == "error"
        assert run["error"] == "budget:ledger_unavailable"
        assert hive.run_ledger.public_events(run["run_id"])[0]["data"]["reason"] == "ledger_unavailable"
        assert hive.incident_ledger.recent()[0]["summary"] == "budget:ledger_unavailable"
    finally:
        asyncio.run(hive.aclose())


def test_orchestrator_emits_closed_spend_ledger_failure_reason():
    orchestrator = ConversationOrchestrator(router=object())

    async def failed(*_args, **_kwargs):
        raise BudgetError("daily spend ledger unavailable", reason_code="ledger_unavailable")

    orchestrator._run_loop = failed

    async def drain():
        return [event async for event in orchestrator.stream_ask("test")]

    assert asyncio.run(drain()) == [
        {"type": "error", "class": "BudgetError", "reason": "ledger_unavailable"},
    ]


def test_untrusted_budget_reason_becomes_unknown_incident(tmp_path, monkeypatch):
    hive = _hive(tmp_path)

    async def failed_stream(*_args, **_kwargs):
        yield {"type": "error", "class": "BudgetError", "reason": "ledger_unavailable private"}

    monkeypatch.setattr(hive.orchestrator, "stream_ask", failed_stream)

    async def drain():
        return [event async for event in hive.stream_ask_iterations("test", session_id="bad-budget")]

    try:
        assert asyncio.run(drain())[0]["reason"] == "unknown"
        run = hive.run_ledger.recent(session_id="bad-budget", limit=1)[0]
        assert run["error"] == "budget:unknown"
        assert hive.incident_ledger.recent()[0]["summary"] == "budget:unknown"
        assert "private" not in str(hive.incident_ledger.recent())
    finally:
        asyncio.run(hive.aclose())


def test_direct_spend_ledger_failure_is_an_incident(tmp_path, monkeypatch):
    hive = _hive(tmp_path)

    async def failed(*_args, **_kwargs):
        raise BudgetError("daily spend ledger unavailable", reason_code="ledger_unavailable")

    monkeypatch.setattr(hive.orchestrator, "ask", failed)
    try:
        with pytest.raises(BudgetError):
            asyncio.run(hive.ask("test", session_id="direct-ledger"))
        run = hive.run_ledger.recent(session_id="direct-ledger", limit=1)[0]
        assert run["state"] == "error"
        assert run["error"] == "budget:ledger_unavailable"
        assert hive.incident_ledger.recent()[0]["summary"] == "budget:ledger_unavailable"
    finally:
        asyncio.run(hive.aclose())


def test_token_stream_spend_ledger_failure_records_incident_immediately(tmp_path, monkeypatch):
    hive = _hive(tmp_path)

    async def failed(*_args, **_kwargs):
        raise BudgetError("daily spend ledger unavailable", reason_code="ledger_unavailable")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(hive.router, "stream", failed, raising=False)

    async def drain():
        return [chunk async for chunk in hive.ask_stream("test", session_id="token-ledger")]

    try:
        with pytest.raises(BudgetError):
            asyncio.run(drain())
        run = hive.run_ledger.recent(session_id="token-ledger", limit=1)[0]
        assert run["error"] == "budget:ledger_unavailable"
        incident_id = hive.incident_ledger.recent()[0]["incident_id"]
        assert len(hive.incident_ledger.get(incident_id)["events"]) == 1
        hive.reconcile_incidents()
        assert len(hive.incident_ledger.get(incident_id)["events"]) == 1
    finally:
        asyncio.run(hive.aclose())


def test_unknown_budget_error_creates_incident_instead_of_silent_refusal(tmp_path, monkeypatch):
    hive = _hive(tmp_path)

    async def failed(*_args, **_kwargs):
        raise BudgetError("unrecognized budget infrastructure failure")

    monkeypatch.setattr(hive.orchestrator, "ask", failed)
    try:
        with pytest.raises(BudgetError):
            asyncio.run(hive.ask("test", session_id="unknown-budget"))
        run = hive.run_ledger.recent(session_id="unknown-budget", limit=1)[0]
        assert run["error"] == "budget:unknown"
        assert hive.incident_ledger.recent()[0]["summary"] == "budget:unknown"
    finally:
        asyncio.run(hive.aclose())
