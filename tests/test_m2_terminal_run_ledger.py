"""M2 — durable, safe operator-run evidence."""
from __future__ import annotations

import asyncio
import json

import pytest

from hive.core.config import HiveConfig
from hive.core.events import EventBus, EventType
from hive.core.types import ToolCall
from hive.llm.adapters.base import CompletionResult
from hive.observability.runs import RunLedger
from hive.runtime import HiveOS


class _ScriptRouter:
    def __init__(self, script: list[CompletionResult]) -> None:
        self._script = list(script)

    async def complete(self, *_args, **_kwargs) -> CompletionResult:
        return self._script.pop(0)

    async def aclose(self) -> None:
        return None


def _config(tmp_path) -> HiveConfig:
    return HiveConfig.from_env(root=tmp_path, load_dotenv=False)


def test_run_ledger_persists_redacted_events_and_terminal_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_TEST_SECRET", "super-secret-value")
    bus = EventBus()
    ledger = RunLedger(tmp_path / "state.sqlite").attach(bus)
    ledger.begin("run-1", kind="conversation", session_id="terminal-1")
    bus.publish(EventType.TOOL_CALL_END, {
        "run_id": "run-1", "tool": "shell", "token": "super-secret-value",
    })
    ledger.finish("run-1", state="ok")

    assert ledger.get("run-1")["state"] == "ok"
    event = ledger.events("run-1")[0]
    assert event["type"] == "tool_call_end"
    assert event["data"]["token"] == "***REDACTED***"
    with pytest.raises(ValueError, match="already terminal"):
        ledger.finish("run-1", state="error")


def test_run_ledger_recovers_interrupted_runs(tmp_path):
    first = RunLedger(tmp_path / "state.sqlite")
    first.begin("run-interrupted", kind="conversation", session_id="terminal-1")
    first.close()

    restored = RunLedger(tmp_path / "state.sqlite")
    assert restored.recover_interrupted() == 1
    row = restored.get("run-interrupted")
    assert row is not None
    assert row["state"] == "cancelled"
    assert row["error"] == "process ended before run completion"


def test_runtime_build_recovers_interrupted_operator_run(tmp_path):
    config = _config(tmp_path)
    first = RunLedger(config.state_db)
    first.begin("run-before-runtime-restart", kind="conversation", session_id="terminal")
    first.close()

    hive = HiveOS.build(config, router=_ScriptRouter([]))
    recovered = hive.run_ledger.get("run-before-runtime-restart")
    assert recovered is not None
    assert recovered["state"] == "cancelled"
    assert recovered["error"] == "process ended before run completion"
    asyncio.run(hive.aclose())


def test_runtime_turn_has_durable_run_and_tool_lifecycle(tmp_path):
    target = tmp_path / "created.txt"
    router = _ScriptRouter([
        CompletionResult(
            text="", model="fake",
            tool_calls=[ToolCall(
                id="call-1", name="write_file",
                arguments=json.dumps({"path": str(target), "content": "proof"}),
            )],
        ),
        CompletionResult(text="done", model="fake"),
    ])
    hive = HiveOS.build(_config(tmp_path), router=router)

    assert asyncio.run(hive.ask("create proof", session_id="terminal-proof")) == "done"
    run = hive.run_ledger.recent(session_id="terminal-proof", limit=1)[0]
    assert run["kind"] == "conversation"
    assert run["state"] == "ok"
    events = hive.run_ledger.events(run["run_id"])
    assert [event["type"] for event in events] == [
        "agent_turn_start", "tool_call_start", "tool_call_end", "agent_turn_end",
    ]
    assert target.read_text(encoding="utf-8") == "proof"
    asyncio.run(hive.aclose())


def test_failed_runtime_turn_is_durable_and_redacts_known_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_TEST_SECRET", "super-secret-value")

    class _FailingRouter:
        async def complete(self, *_args, **_kwargs):
            raise RuntimeError("provider failed with super-secret-value")

        async def aclose(self) -> None:
            return None

    hive = HiveOS.build(_config(tmp_path), router=_FailingRouter())
    with pytest.raises(RuntimeError):
        asyncio.run(hive.ask("fail safely", session_id="terminal-proof"))

    run = hive.run_ledger.recent(session_id="terminal-proof", limit=1)[0]
    assert run["state"] == "error"
    assert "super-secret-value" not in run["error"]
    assert "***REDACTED***" in run["error"]
    asyncio.run(hive.aclose())


def test_closed_stream_is_recorded_as_cancelled(tmp_path):
    class _StreamRouter:
        async def stream(self, *_args, **_kwargs):
            yield "first"
            yield "second"

        async def aclose(self) -> None:
            return None

    hive = HiveOS.build(_config(tmp_path), router=_StreamRouter())

    async def close_early() -> None:
        stream = hive.ask_stream("stream", session_id="terminal-stream")
        assert await anext(stream) == "first"
        await stream.aclose()

    asyncio.run(close_early())
    run = hive.run_ledger.recent(session_id="terminal-stream", limit=1)[0]
    assert run["state"] == "cancelled"
    assert run["error"] == "stream closed before completion"
    asyncio.run(hive.aclose())
