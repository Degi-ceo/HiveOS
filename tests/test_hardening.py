"""M5 hardening — coverage gaps (delegate/mcp/vault), observability, sandbox, cli."""
from __future__ import annotations

import asyncio
import json

import pytest


# --- #hd-1 agents/delegate concurrency -----------------------------------------

def test_delegate_runs_all_and_preserves_order():
    from hive.agents.base import AgentResult, BaseAgent
    from hive.agents.delegate import delegate

    class _Echo(BaseAgent):
        agent_id = "echo"
        async def run(self, task, context=None, **kw):
            return AgentResult(content=f"done:{task}")

    out = asyncio.run(delegate(["a", "b", "c"], agent_factory=_Echo, max_concurrent=2))
    assert [r.content for r in out] == ["done:a", "done:b", "done:c"]


def test_delegate_bounds_concurrency():
    from hive.agents.base import AgentResult, BaseAgent
    from hive.agents.delegate import delegate

    state = {"live": 0, "peak": 0}

    class _Tracker(BaseAgent):
        agent_id = "track"
        async def run(self, task, context=None, **kw):
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
            await asyncio.sleep(0.01)
            state["live"] -= 1
            return AgentResult(content=task)

    asyncio.run(delegate([str(i) for i in range(8)], agent_factory=_Tracker, max_concurrent=2))
    assert state["peak"] <= 2   # never more than the cap ran at once


# --- #hd-1 tools/mcp client + server -------------------------------------------

def test_mcp_tool_to_spec_marks_dangerous():
    from hive.tools.mcp.client import mcp_tool_to_spec
    spec = mcp_tool_to_spec({"name": "search", "description": "d",
                             "inputSchema": {"type": "object"}}, prefix="ext.")
    assert spec.name == "ext.search" and spec.dangerous is True and spec.category == "mcp"


def test_mcp_tool_executes_via_caller():
    from hive.tools.mcp.client import MCPTool, mcp_tool_to_spec

    async def caller(name, args):
        return f"called {name} with {args.get('q')}"

    tool = MCPTool(mcp_tool_to_spec({"name": "search"}), caller)
    res = asyncio.run(tool.execute(q="hi"))
    assert res.content == "called search with hi"


def test_mcp_server_build_tool_listing_is_sorted():
    from hive.tools.mcp.server import build_tool_listing
    from hive.tools.base import BaseTool, ToolSpec
    from hive.core.types import ToolResult

    class _T(BaseTool):
        def __init__(self, n):
            self._s = ToolSpec(name=n, description=f"{n} desc", parameters={})
        @property
        def spec(self): return self._s
        async def execute(self, **kw): return ToolResult(tool_name=self._s.name, content="")

    listing = build_tool_listing({"b": _T("b"), "a": _T("a")})
    assert [d["name"] for d in listing] == ["a", "b"]   # deterministic order
    assert listing[0]["inputSchema"] == {"type": "object", "properties": {}}


def test_mcp_client_as_tools_wraps_descriptors():
    from hive.tools.mcp.client import MCPClient
    client = MCPClient("echo", [])
    tools = client.as_tools([{"name": "x", "description": "d", "inputSchema": {}}], prefix="m.")
    assert len(tools) == 1 and tools[0].spec.name == "m.x"


# --- #hd-1 memory/vault --------------------------------------------------------

def test_vault_writes_note_with_frontmatter(tmp_path):
    from hive.memory.vault import ObsidianVault
    v = ObsidianVault(tmp_path / "vault")
    note = v.write("skill", "Deploy via SSH", "Use rsync then restart.", source="exp")
    assert note.exists()
    text = note.read_text()
    assert text.startswith("---") and "kind: skill" in text
    assert "# Deploy via SSH" in text and "Use rsync then restart." in text
    assert v.stats()["notes"] == 1


def test_vault_sanitizes_filename(tmp_path):
    from hive.memory.vault import ObsidianVault
    v = ObsidianVault(tmp_path / "vault")
    note = v.write("fix", "weird/name:with*chars", "body")
    assert "/" not in note.name and note.exists()


# --- #hd-2 observability -------------------------------------------------------

def test_telemetry_tracks_cost_and_tokens():
    from hive.core.events import EventBus, EventType
    from hive.observability.telemetry import Telemetry
    bus = EventBus()
    t = Telemetry().attach(bus)
    bus.publish(EventType.INFERENCE_END,
                {"model": "MiniMax-M3", "input_tokens": 100, "output_tokens": 50,
                 "cost_usd": 0.001})
    bus.publish(EventType.TOOL_CALL_END, {"tool": "x", "status": "ok"})
    snap = t.snapshot()
    assert snap["inference_calls"] == 1 and snap["tool_calls"] == 1
    assert snap["input_tokens"] == 100 and snap["output_tokens"] == 50
    assert snap["cost_usd"] == pytest.approx(0.001)
    assert snap["cost_by_model"]["MiniMax-M3"] == pytest.approx(0.001)


def test_trace_export_is_json_serializable():
    from hive.core.events import EventBus, EventType
    from hive.observability.traces import TraceCollector
    bus = EventBus()
    tc = TraceCollector().attach(bus)
    bus.publish(EventType.AGENT_TURN_START, {"session": "s1"})
    bus.publish(EventType.INFERENCE_END, {"session": "s1", "model": "m"})
    exported = tc.export("s1")
    assert len(exported) == 2 and exported[0]["type"] == "agent_turn_start"
    json.dumps(exported)   # must not raise
    assert "s1" in tc.export_all()


# --- #hd-3 sandbox -------------------------------------------------------------

def test_docker_command_wraps_with_mount_and_no_network():
    from hive.core.sandbox import docker_command
    cmd = docker_command("python:3.12", "/opt/hiveos", "pytest -q")
    assert "docker run --rm --network none" in cmd
    assert "/opt/hiveos:/repo" in cmd and "-w /repo" in cmd
    assert "python:3.12" in cmd and "pytest -q" in cmd


def test_sandbox_runner_passthrough_without_image():
    from hive.core.sandbox import make_sandbox_runner, _default_run
    assert make_sandbox_runner(None) is _default_run


def test_sandbox_runner_sandboxes_tests_but_not_git():
    from hive.core.sandbox import make_sandbox_runner
    seen = []

    async def fake(cmd, cwd=None):
        seen.append(cmd)
        return 0, "ok"

    run = make_sandbox_runner("python:3.12", repo_root="/repo", base=fake)
    asyncio.run(run("git status", "/repo"))     # git stays local
    asyncio.run(run("pytest -q", "/repo"))       # tests get containerized
    assert seen[0] == "git status"
    assert seen[1].startswith("docker run") and "pytest -q" in seen[1]


# --- #hd-4 cli commands exist --------------------------------------------------

def test_cli_unknown_command_lists_new_commands(capsys):
    from hive.surfaces import cli
    rc = cli.main(["nonsense"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "heartbeat" in err and "consolidate" in err


def test_cli_help_flag_prints_usage_to_stdout(capsys):
    from hive.surfaces import cli
    for flag in ("-h", "--help", "help"):
        rc = cli.main([flag])
        out = capsys.readouterr()
        assert rc == 0                       # help is success, not an error
        assert "usage: hive" in out.out      # goes to stdout, not stderr
        assert out.err == ""
