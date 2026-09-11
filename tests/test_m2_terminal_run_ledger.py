"""M2 — durable, safe operator-run evidence."""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from hive.core.config import HiveConfig
from hive.core.events import EventBus, EventType
from hive.core.types import ToolCall
from hive.llm.adapters.base import CompletionResult
from hive.observability.runs import RunLedger, _process_is_alive
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


def test_process_liveness_detects_existing_parent_without_side_effects():
    """The platform liveness probe only queries an existing parent process."""
    parent_pid = os.getppid()
    if parent_pid > 0:
        assert _process_is_alive(parent_pid) is True


@pytest.mark.skipif(os.name != "nt", reason="uses the protected Windows System process")
def test_windows_liveness_keeps_protected_system_process_alive():
    """Access denied from OpenProcess means the protected process is still alive."""
    assert _process_is_alive(4) is True


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
    first = RunLedger(tmp_path / "state.sqlite", process_id=0)
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
    first = RunLedger(config.state_db, process_id=0)
    first.begin("run-before-runtime-restart", kind="conversation", session_id="terminal")
    first.close()

    hive = HiveOS.build(config, router=_ScriptRouter([]))
    recovered = hive.run_ledger.get("run-before-runtime-restart")
    assert recovered is not None
    assert recovered["state"] == "cancelled"
    assert recovered["error"] == "process ended before run completion"
    asyncio.run(hive.aclose())


def test_runtime_build_does_not_cancel_live_run_from_another_runtime(tmp_path):
    """A second runtime sharing state must not mistake a live peer for a restart."""
    config = _config(tmp_path)
    first = HiveOS.build(config, router=_ScriptRouter([]))
    first.run_ledger.begin("run-live-peer", kind="conversation", session_id="terminal")
    second = HiveOS.build(config, router=_ScriptRouter([]))

    try:
        live = first.run_ledger.get("run-live-peer")
        assert live is not None
        assert live["state"] == "running"
        first.run_ledger.finish("run-live-peer", state="ok")
        assert first.run_ledger.get("run-live-peer")["state"] == "ok"
    finally:
        asyncio.run(second.aclose())
        asyncio.run(first.aclose())


def test_recovery_leaves_running_remote_host_run_unchanged(tmp_path):
    """A host cannot safely infer whether a remote process is still alive."""
    db_path = tmp_path / "state.sqlite"
    remote = RunLedger(db_path, hostname="remote-host", process_id=0)
    remote.begin("run-remote-peer", kind="conversation", session_id="terminal")

    local = RunLedger(db_path, hostname="local-host", process_is_alive=lambda _pid: False)
    try:
        assert local.recover_interrupted() == 0
        assert local.get("run-remote-peer")["state"] == "running"
    finally:
        remote.close()
        local.close()


def test_recovery_does_not_cancel_live_run_owned_by_another_process(tmp_path):
    """A local peer process stays live even when a new process opens the database."""
    db_path = tmp_path / "state.sqlite"
    owner = RunLedger(
        db_path,
        hostname="same-host",
        process_id=101,
        process_is_alive=lambda pid: pid == 101,
    )
    owner.begin("run-live-process", kind="conversation", session_id="terminal")
    recovering_peer = RunLedger(
        db_path,
        hostname="same-host",
        process_id=202,
        process_is_alive=lambda pid: pid == 101,
    )

    try:
        assert recovering_peer.recover_interrupted() == 0
        owner.finish("run-live-process", state="ok")
        assert owner.get("run-live-process")["state"] == "ok"
    finally:
        owner.close()
        recovering_peer.close()


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
