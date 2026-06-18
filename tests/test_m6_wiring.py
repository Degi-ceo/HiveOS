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


# --- B2: DelegateToSpecialist tool (G-3) ----------------------------------------

def test_delegate_to_specialist_registered(tmp_path, monkeypatch):
    h = _hive(tmp_path, monkeypatch)
    assert "delegate_to_specialist" in h.tools
    assert h.tools["delegate_to_specialist"].spec.dangerous is False


def test_delegate_to_specialist_executes(tmp_path, monkeypatch):
    from hive.agents.base import AgentResult
    h = _hive(tmp_path, monkeypatch)

    async def fake_delegate_named(tasks, name, *, max_concurrent=3, executor=None):
        return [AgentResult(content="specialist reply")]

    monkeypatch.setattr("hive.agents.delegate.delegate_named", fake_delegate_named)
    res = asyncio.run(h.tools["delegate_to_specialist"].execute(agent="researcher", task="find x"))
    assert "specialist reply" in res.content


def test_delegate_to_specialist_unknown_agent_returns_error(tmp_path, monkeypatch):
    h = _hive(tmp_path, monkeypatch)

    async def fake_delegate_named(tasks, name, *, max_concurrent=3, executor=None):
        raise KeyError(name)

    monkeypatch.setattr("hive.agents.delegate.delegate_named", fake_delegate_named)
    res = asyncio.run(h.tools["delegate_to_specialist"].execute(agent="ghost", task="do x"))
    assert res.content.startswith("[delegate error:")


# --- B4: HiveOS.curate_umbrellas() fail-open wrapper ---------------------------

def test_curate_umbrellas_skip_no_skills(tmp_path, monkeypatch):
    h = _hive(tmp_path, monkeypatch)
    result = asyncio.run(h.curate_umbrellas())
    # fresh build has no agent-created skills -> consolidate_umbrellas returns skipped
    assert result.get("skipped") is True


def test_curate_umbrellas_fail_open(tmp_path, monkeypatch):
    h = _hive(tmp_path, monkeypatch)

    async def boom(*_, **__):
        raise RuntimeError("unexpected internal error")

    monkeypatch.setattr(h.curator, "consolidate_umbrellas", boom)
    result = asyncio.run(h.curate_umbrellas())
    assert result.get("skipped") is True


# --- B5: DiscoverTool security delegate wiring ----------------------------------

def test_discover_tool_passes_security_delegate(tmp_path, monkeypatch):
    captured: dict = {}

    async def fake_discover(need, *, memory=None, github_token="", security_delegate=None):
        captured["security_delegate"] = security_delegate
        return {"need": need, "candidates": []}

    monkeypatch.setattr("hive.tools.builtins._discovery.discover", fake_discover)

    from hive.tools.builtins import DiscoverTool
    tool = DiscoverTool(enable_security_audit=True)
    asyncio.run(tool.execute(need="test tool"))
    assert callable(captured.get("security_delegate")), "security_delegate must be a callable"


# --- ApprovalGate tests (via hive.core.approval bridge, read-only) -------------

def test_approval_gate_dangerous_tool_always_queued():
    """'deploy' is in DANGEROUS_TOOLS → is_dangerous must return True regardless of args."""
    from hive.core.approval import ApprovalGate
    gate = ApprovalGate()
    assert gate.is_dangerous("deploy", {}) is True


def test_approval_gate_resolve_approves_and_removes():
    """queue an item, resolve approved=True → item removed from pending, item returned."""
    from hive.core.approval import ApprovalGate
    gate = ApprovalGate()
    aid = gate.request("deploy", {"env": "staging"}, "release v2", "danger")
    assert any(item["id"] == aid for item in gate.pending())
    result = gate.resolve(aid, approved=True)
    assert result is not None
    assert result["approved"] is True
    assert result["id"] == aid
    # must be removed from the pending queue
    assert not any(item["id"] == aid for item in gate.pending())


def test_approval_gate_resolve_rejects_and_removes():
    """queue an item, resolve approved=False → item also removed, returned with approved=False."""
    from hive.core.approval import ApprovalGate
    gate = ApprovalGate()
    aid = gate.request("deploy", {"env": "prod"}, "rollback", "danger")
    assert any(item["id"] == aid for item in gate.pending())
    result = gate.resolve(aid, approved=False)
    assert result is not None
    assert result["approved"] is False
    assert result["id"] == aid
    assert not any(item["id"] == aid for item in gate.pending())


# --- Additional M6 wiring tests -----------------------------------------------

def test_approval_gate_list_pending_empty_initially():
    """A freshly created ApprovalGate has no pending items."""
    from hive.core.approval import ApprovalGate
    gate = ApprovalGate()
    assert gate.pending() == []


def test_approval_gate_dangerous_tool_goes_through_gate():
    """'deploy' is in DANGEROUS_TOOLS → is_dangerous returns True and request queues an item."""
    from hive.core.approval import ApprovalGate
    gate = ApprovalGate()
    assert gate.is_dangerous("deploy", {}) is True
    aid = gate.request("deploy", {"env": "staging"}, "triggered by test", "danger")
    # Item should now be pending
    ids = [item["id"] for item in gate.pending()]
    assert aid in ids


def test_curate_umbrellas_returns_dict_with_key(tmp_path, monkeypatch):
    """curate_umbrellas always returns a dict with 'skipped' or 'umbrellas_created'."""
    h = _hive(tmp_path, monkeypatch)
    result = asyncio.run(h.curate_umbrellas())
    assert isinstance(result, dict)
    assert "skipped" in result or "umbrellas_created" in result


def test_discover_tool_registered_in_hive(tmp_path, monkeypatch):
    """'discover' key exists in h.tools after building HiveOS."""
    h = _hive(tmp_path, monkeypatch)
    assert "discover" in h.tools


def test_delegate_to_specialist_is_not_dangerous(tmp_path, monkeypatch):
    """delegate_to_specialist should not be marked dangerous — it is not gated."""
    h = _hive(tmp_path, monkeypatch)
    spec = h.tools["delegate_to_specialist"].spec
    assert spec.dangerous is False


def test_tool_executor_runs_available_tool():
    """ToolExecutor dispatches a registered available tool and returns OK status."""
    from hive.tools.executor import ToolExecutor, DispatchStatus
    from hive.tools.base import BaseTool, ToolSpec
    from hive.core.types import ToolResult

    class _Echo(BaseTool):
        spec = ToolSpec(name="echo", description="echo", parameters={})
        async def execute(self, **kw): return ToolResult(tool_name="echo", content="pong")

    class _Gate:
        def is_dangerous(self, *a, **k): return False
        def request(self, *a, **k): return "id-unused"

    ex = ToolExecutor({"echo": _Echo()}, gate=_Gate())
    d = asyncio.run(ex.execute("echo", {}))
    assert d.status is DispatchStatus.OK
    assert d.result is not None and d.result.content == "pong"


# --- Wave 3L additional tests -----------------------------------------------

def test_approval_gate_multiple_pending_items():
    """Multiple request() calls → all items appear in pending()."""
    from hive.core.approval import ApprovalGate
    gate = ApprovalGate()
    id1 = gate.request("deploy", {}, "first", "danger")
    id2 = gate.request("deploy", {}, "second", "danger")
    ids = {item["id"] for item in gate.pending()}
    assert id1 in ids and id2 in ids


def test_approval_gate_request_money():
    """request_money() adds a 'money' kind item to pending."""
    from hive.core.approval import ApprovalGate
    gate = ApprovalGate()
    aid = gate.request_money("invoice #42", "50 USD", "pay contractor")
    ids = [item["id"] for item in gate.pending()]
    assert aid in ids


def test_approval_gate_resolve_unknown_id_returns_none():
    """resolve() with an unknown ID returns None gracefully."""
    from hive.core.approval import ApprovalGate
    gate = ApprovalGate()
    result = gate.resolve("unknown-id-xyz", approved=True)
    assert result is None


def test_hive_tool_executor_registered(tmp_path, monkeypatch):
    """HiveOS.tool_executor is accessible after build."""
    h = _hive(tmp_path, monkeypatch)
    assert h.tool_executor is not None


def test_hive_agents_registry_contains_reviewer(tmp_path, monkeypatch):
    """'reviewer' specialist is registered in agents_registry."""
    h = _hive(tmp_path, monkeypatch)
    assert "reviewer" in h.agents_registry


def test_hive_approval_gate_not_dangerous_read_file(tmp_path, monkeypatch):
    """read_file is NOT in DANGEROUS_TOOLS — is_dangerous must return False."""
    from hive.core.approval import ApprovalGate
    gate = ApprovalGate()
    assert gate.is_dangerous("read_file", {}) is False
