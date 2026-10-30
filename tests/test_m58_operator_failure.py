"""Provider failures expose only actionable, bounded operator reason codes."""

import asyncio
from dataclasses import replace

import httpx

from hive.agents.orchestrator import ConversationOrchestrator
from hive.observability.operator_events import public_operator_event
from hive.observability.runs import RunLedger
from hive.surfaces.cli import _terminal_turn


def _stream_failure(cause: Exception) -> dict:
    from hive.llm.router import ProviderError

    orchestrator = ConversationOrchestrator(router=object())

    async def fail(*_args, **_kwargs):
        raise ProviderError("private provider response") from cause

    orchestrator._run_loop = fail

    async def collect():
        return [event async for event in orchestrator.stream_ask("private prompt")]

    events = asyncio.run(collect())
    assert len(events) == 1
    assert "private" not in str(events)
    return events[0]


def test_billing_failure_has_closed_reason_without_provider_body():
    request = httpx.Request("POST", "https://provider.example/messages")
    response = httpx.Response(402, request=request, text="private provider response")
    event = _stream_failure(httpx.HTTPStatusError("private response", request=request, response=response))
    assert event == {"type": "error", "class": "ProviderError", "reason": "billing"}


def test_transport_failure_is_reported_as_network_not_overload():
    event = _stream_failure(httpx.ConnectError("private proxy address"))
    assert event == {"type": "error", "class": "ProviderError", "reason": "network"}


def test_public_reason_is_allowlisted_and_survives_durable_replay(tmp_path):
    base = {"type": "error", "class": "ProviderError", "reason": "billing", "detail": "private token"}
    safe = public_operator_event(base, run_id="run", session_id="private-session", sequence=1)
    assert safe["reason"] == "billing"
    assert "private token" not in str(safe)
    injected = public_operator_event(
        {**base, "reason": "billing private token"}, run_id="run", session_id="private-session", sequence=2,
    )
    assert injected["reason"] == "unknown"

    ledger = RunLedger(tmp_path / "state.sqlite")
    try:
        ledger.begin("run", kind="conversation", session_id="private-session")
        ledger.record_operator_event(safe)
        replay = ledger.public_events("run")
    finally:
        ledger.close()
    assert replay[0]["data"]["reason"] == "billing"
    assert "private" not in str(replay)


def test_terminal_shows_actionable_billing_reason_without_raw_fields(capsys):
    class FakeHive:
        async def stream_ask_iterations(self, *_args, **_kwargs):
            yield {"type": "error", "class": "ProviderError", "reason": "billing", "detail": "private token"}

    result = asyncio.run(_terminal_turn(FakeHive(), "hello", session_id="test"))
    output = capsys.readouterr().out
    assert result == 1
    assert "billing" in output.lower()
    assert "credits" in output.lower()
    assert "private token" not in output


def test_watch_replays_only_the_safe_provider_reason(tmp_path, monkeypatch, capsys):
    from hive.core.config import HiveConfig
    from hive.surfaces.cli import _watch

    cfg = replace(HiveConfig.from_env(root=tmp_path, load_dotenv=False), state_db=tmp_path / "state.sqlite")
    ledger = RunLedger(cfg.state_db)
    try:
        ledger.begin("run", kind="conversation", session_id="private-session")
        ledger.record_operator_event({
            "type": "error", "class": "ProviderError", "reason": "billing", "run_id": "run",
            "sequence": 1, "timestamp": 10.0, "detail": "private provider body",
        })
        ledger.finish("run", state="error")
    finally:
        ledger.close()
    monkeypatch.setattr(HiveConfig, "from_env", classmethod(lambda cls: cfg))
    assert _watch("run") == 0
    output = capsys.readouterr().out
    assert "error billing" in output
    assert "private provider body" not in output
    assert "private-session" not in output
