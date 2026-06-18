"""M6 — wiring built-but-unwired code into the runtime (discovery, credentials, MCP, executor)."""
from __future__ import annotations

import asyncio

import pytest

from hive.core.config import HiveConfig
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS


class _Router:
    async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
        return CompletionResult(text="ok", model="fake")

    async def aclose(self):
        pass


def _hive(tmp_path, monkeypatch, *, router=_Router()) -> HiveOS:
    # Avoid spinning real Mnemosyne in unit tests -> force the local provider.
    monkeypatch.setattr("hive.runtime.build_mnemosyne_provider", lambda **kw: None)
    return HiveOS.build(HiveConfig.from_env(root=tmp_path, load_dotenv=False), router=router)


# --- A1: discovery-first tool --------------------------------------------------

def test_discover_tool_registered(tmp_path, monkeypatch):
    h = _hive(tmp_path, monkeypatch)
    assert "discover" in h.tools
    assert h.tools["discover"].spec.dangerous is False  # read-only search, not gated


def test_discover_tool_executes(tmp_path, monkeypatch):
    h = _hive(tmp_path, monkeypatch)

    async def fake_discover(need, *, memory=None, github_token="", security_delegate=None):
        return {"need": need, "candidates": [{"name": "x/y"}]}
    monkeypatch.setattr("hive.tools.builtins._discovery.discover", fake_discover)

    res = asyncio.run(h.tools["discover"].execute(need="vector db"))
    assert "vector db" in res.content and "x/y" in res.content


def test_discover_tool_only_caches_with_capable_memory():
    from hive.tools.builtins import DiscoverTool

    class _Capable:
        def recall(self, *a, **k): return []
        def learn(self, *a, **k): pass

    class _Incapable:  # MemoryProvider without recall/learn (e.g. Mnemosyne adapter)
        def prefetch(self, *a, **k): return ""

    assert DiscoverTool(memory=_Capable())._memory is not None
    assert DiscoverTool(memory=_Incapable())._memory is None
    assert DiscoverTool(memory=None)._memory is None


def test_hive_discover_method(tmp_path, monkeypatch):
    h = _hive(tmp_path, monkeypatch)

    async def fake_discover(need, *, memory=None, github_token=""):
        return {"need": need, "cached": False}
    monkeypatch.setattr("hive.tools.discovery.discover", fake_discover)
    out = asyncio.run(h.discover("an mcp server for sqlite"))
    assert out["need"] == "an mcp server for sqlite"


# --- A4: credentials -> multi-key pool ----------------------------------------

def test_multikey_pool_from_comma_separated(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "k1,k2,k3")
    monkeypatch.setattr("hive.runtime.build_mnemosyne_provider", lambda **kw: None)
    h = HiveOS.build(HiveConfig.from_env(root=tmp_path, load_dotenv=False))  # real router
    assert len(h.router._pool) == 3
    asyncio.run(h.aclose())


def test_single_key_pool_default(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "solo")
    monkeypatch.setattr("hive.runtime.build_mnemosyne_provider", lambda **kw: None)
    h = HiveOS.build(HiveConfig.from_env(root=tmp_path, load_dotenv=False))
    assert len(h.router._pool) == 1
    asyncio.run(h.aclose())


def test_credentials_inject_seeds_env(tmp_path, monkeypatch):
    from hive.core import config as cfgmod, credentials
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    cfgmod.set_config(cfg)
    cfg.ensure_dirs()
    credentials.save("SOME_SECRET", "v1")
    monkeypatch.delenv("SOME_SECRET", raising=False)
    assert credentials.inject() >= 1
    import os
    assert os.environ["SOME_SECRET"] == "v1"


# --- A2: MCP load + executor.add_tool -----------------------------------------

def test_executor_add_tool():
    from hive.tools.executor import ToolExecutor
    from hive.tools.base import BaseTool, ToolSpec
    from hive.core.types import ToolResult

    class _T(BaseTool):
        spec = ToolSpec(name="late", description="d", parameters={})
        async def execute(self, **kw): return ToolResult(tool_name="late", content="x")

    ex = ToolExecutor({})
    ex.add_tool(_T())
    assert "late" in ex._tools


def test_load_mcp_servers_registers_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_MCP_SERVERS", "fakecmd --flag")
    h = _hive(tmp_path, monkeypatch)

    class _FakeMCPClient:
        def __init__(self, command, args=None):
            self.command, self.args = command, args or []
        async def connect(self): pass
        async def list_tools(self):
            return [{"name": "search", "description": "d", "inputSchema": {}}]
        async def call(self, name, args): return "fake result"
        def as_tools(self, descriptors, *, prefix=""):
            from hive.tools.mcp.client import MCPTool, mcp_tool_to_spec
            return [MCPTool(mcp_tool_to_spec(d, prefix=prefix), self.call,
                            remote_name=d.get("name", "")) for d in descriptors]

    monkeypatch.setattr("hive.tools.mcp.client.MCPClient", _FakeMCPClient)
    n = asyncio.run(h.load_mcp_servers())
    assert n == 1 and "fakecmd.search" in h.tools
    assert "fakecmd.search" in h.tool_executor._tools


def test_load_mcp_servers_isolates_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_MCP_SERVERS", "bad")
    h = _hive(tmp_path, monkeypatch)

    class _Broken:
        def __init__(self, *a, **k): pass
        async def connect(self): raise RuntimeError("no such server")

    monkeypatch.setattr("hive.tools.mcp.client.MCPClient", _Broken)
    assert asyncio.run(h.load_mcp_servers()) == 0  # failure swallowed, no crash


def test_no_mcp_servers_is_noop(tmp_path, monkeypatch):
    h = _hive(tmp_path, monkeypatch)
    assert asyncio.run(h.load_mcp_servers()) == 0


# --- A5: AgentExecutor wired into delegate -------------------------------------

def test_delegate_uses_executor_retry_and_error_result():
    from hive.agents.base import AgentResult, BaseAgent
    from hive.agents.delegate import delegate
    from hive.agents.executor import AgentExecutor
    from hive.llm.failover import RetryPolicy

    attempts = {"flaky": 0}

    class _Flaky(BaseAgent):
        agent_id = "flaky"
        async def run(self, task, context=None, **kw):
            if task == "flaky":
                attempts["flaky"] += 1
                if attempts["flaky"] < 2:
                    raise RuntimeError("transient")
                return AgentResult(content="recovered")
            if task == "doomed":
                raise RuntimeError("always fails")
            return AgentResult(content=f"ok:{task}")

    ex = AgentExecutor(retry=RetryPolicy(max_attempts=2, base_delay=0, max_delay=0))
    out = asyncio.run(delegate(["good", "flaky", "doomed"], agent_factory=_Flaky,
                               executor=ex))
    assert out[0].content == "ok:good"
    assert out[1].content == "recovered"            # retried then succeeded
    assert "subagent failed" in out[2].content       # permanent failure -> error result
