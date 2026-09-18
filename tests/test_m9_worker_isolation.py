"""M9.6: real local specialist workers keep authority with the supervisor."""
from __future__ import annotations

import asyncio
import time

import pytest

from hive.agents.worker_protocol import PROTOCOL_VERSION, WorkerProtocolError, WorkerRequest, decode, encode
from hive.agents.worker_supervisor import LocalWorkerSupervisor
from hive.core.child_env import minimal_worker_environment
from hive.core.config import HiveConfig
from hive.core.run_context import bind_run_id
from hive.core.types import ToolCall, ToolResult
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS
from hive.tools.base import BaseTool, ToolSpec


class _ReadOnlyProbe(BaseTool):
    spec = ToolSpec(
        name="read_file", description="test probe",
        parameters={"type": "object", "properties": {}},
    )

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **_params: object) -> ToolResult:
        self.calls += 1
        return ToolResult(tool_name=self.spec.name, content="supervisor tool result")


class _Router:
    def __init__(self, *, tool_call: bool = False, tool_name: str = "read_file") -> None:
        self.calls = 0
        self.tool_call = tool_call
        self.tool_name = tool_name

    async def complete(self, _messages, **_kwargs):
        self.calls += 1
        if self.tool_call and self.calls == 1:
            return CompletionResult(
                text="", model="test",
                tool_calls=[ToolCall(id="call-1", name=self.tool_name, arguments="{}")],
            )
        return CompletionResult(text="worker completed", model="test")


def test_worker_environment_is_an_allowlist_not_a_secret_denylist():
    env = {
        "PATH": "safe", "SystemRoot": "C:\\Windows", "HIVE_APPROVER_KEY": "no",
        "MINIMAX_API_KEY": "no", "GH_TOKEN": "no", "OPAQUE_PASSWORD": "no",
        "HIVE_UNRECOGNIZED_SETTING": "no",
    }
    child = minimal_worker_environment(env)
    assert child == {"PATH": "safe", "SystemRoot": "C:\\Windows"}


def test_protocol_rejects_invalid_frames_and_bounds_payloads():
    request = WorkerRequest(role="researcher", task="review", run_id="run")
    decoded = decode(encode(request.to_dict()))
    assert decoded["version"] == PROTOCOL_VERSION
    with pytest.raises(WorkerProtocolError):
        decode(b'{"version":999,"type":"start"}')
    with pytest.raises(WorkerProtocolError):
        decode(b"not-json")


def test_real_worker_uses_supervisor_for_model_and_tools():
    router = _Router(tool_call=True)
    tool = _ReadOnlyProbe()
    audit = []
    supervisor = LocalWorkerSupervisor(
        router, {"read_file": tool}, timeout=20.0, audit=audit.append,
    )

    result = asyncio.run(supervisor.execute(
        "use the permitted tool", "researcher", run_id="child-run", delegation_id="delegation",
    ))

    assert result.content == "worker completed"
    assert router.calls == 2
    assert tool.calls == 1
    assert audit and audit[0]["tool"] == "read_file"
    assert audit[0]["run_id"] == "child-run"


def test_worker_rejects_tool_outside_closed_profile():
    router = _Router(tool_call=True, tool_name="shell")
    shell = _ReadOnlyProbe()
    supervisor = LocalWorkerSupervisor(router, {"shell": shell}, timeout=20.0)
    result = asyncio.run(supervisor.execute("try shell", "researcher", run_id="child-run"))
    # A compromised/misbehaving model can request it, but the supervisor does
    # not execute a tool absent from the role's closed profile.
    assert result.content == "worker completed"
    assert shell.calls == 0


def test_worker_timeout_stops_the_child_and_fails_closed():
    class _SlowRouter:
        async def complete(self, _messages, **_kwargs):
            await asyncio.sleep(5.0)
            return CompletionResult(text="too late", model="test")

    started = time.monotonic()
    result = asyncio.run(LocalWorkerSupervisor(
        _SlowRouter(), {}, timeout=1.0,
    ).execute("wait", "researcher", run_id="child-run"))
    assert result.content == "[subagent failed: worker unavailable]"
    assert time.monotonic() - started < 3.0


def test_real_hive_worker_failure_creates_redacted_replan_incident(tmp_path, monkeypatch):
    """Exercise worker IPC, containment, delegation and incident projection together."""
    class _FailingRouter:
        async def complete(self, *_args, **_kwargs):
            raise RuntimeError("provider failure token=secret-value")

        async def aclose(self):
            pass

    monkeypatch.setattr("hive.runtime.build_mnemosyne_provider", lambda **_kwargs: None)
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg, router=_FailingRouter())
    try:
        async def run():
            with bind_run_id("parent-run"):
                return await hive.tools["delegate_to_specialist"].execute(
                    agent="researcher", task="private delegated task",
                )

        result = asyncio.run(run())
        assert result.content == "[subagent failed: worker unavailable]"
        assert not result.success
        incident = next(item for item in hive.incident_ledger.recent() if item["source"] == "delegation")
        restored = hive.incident_ledger.get(incident["incident_id"])
        assert restored is not None
        assert "private delegated task" not in str(restored)
        assert "secret-value" not in str(restored)
    finally:
        asyncio.run(hive.aclose())
