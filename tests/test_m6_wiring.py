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

    async def fake_discover(need, *, memory=None, github_token="", security_delegate=None, recorder=None):
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

    async def fake_discover(need, *, memory=None, github_token="", recorder=None):
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

    async def fake_via_envelope(task, name, *, executor=None, bus=None, session_id=None):
        return AgentResult(content="specialist reply")

    monkeypatch.setattr("hive.agents.delegate.delegate_via_envelope", fake_via_envelope)
    res = asyncio.run(h.tools["delegate_to_specialist"].execute(agent="researcher", task="find x"))
    assert "specialist reply" in res.content


def test_delegate_to_specialist_unknown_agent_returns_error(tmp_path, monkeypatch):
    h = _hive(tmp_path, monkeypatch)

    async def fake_via_envelope(task, name, *, executor=None, bus=None, session_id=None):
        raise KeyError(name)

    monkeypatch.setattr("hive.agents.delegate.delegate_via_envelope", fake_via_envelope)
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

    async def fake_discover(need, *, memory=None, github_token="", security_delegate=None, recorder=None):
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


# --- Wave 3O additional tests ---------------------------------------------------

def test_approval_gate_money_kind_is_money():
    """request_money() creates a pending item with kind='money'."""
    from hive.core.approval import ApprovalGate
    gate = ApprovalGate()
    gate.request_money("server hosting", "100 USD", "monthly fee")
    items = gate.pending()
    assert items[0]["kind"] == "money"


def test_approval_gate_danger_kind_is_danger():
    """request() with default kind creates a pending item with kind='danger'."""
    from hive.core.approval import ApprovalGate
    gate = ApprovalGate()
    gate.request("deploy", {"env": "prod"}, "rollout", "danger")
    items = gate.pending()
    assert items[0]["kind"] == "danger"


def test_approval_gate_resolve_approved_false_sets_field():
    """resolve(approved=False) returns item with approved=False."""
    from hive.core.approval import ApprovalGate
    gate = ApprovalGate()
    aid = gate.request("deploy", {}, "test", "danger")
    result = gate.resolve(aid, approved=False)
    assert result is not None
    assert result["approved"] is False


def test_executor_dispatches_unknown_tool_returns_error():
    """ToolExecutor.execute() with an unknown tool returns ERROR status."""
    from hive.tools.executor import ToolExecutor, DispatchStatus
    ex = ToolExecutor({})
    d = asyncio.run(ex.execute("nonexistent_tool", {}))
    assert d.status is DispatchStatus.ERROR


def test_load_mcp_servers_with_empty_env_is_noop(tmp_path, monkeypatch):
    """load_mcp_servers() returns 0 when HIVE_MCP_SERVERS is empty string."""
    monkeypatch.setenv("HIVE_MCP_SERVERS", "")
    h = _hive(tmp_path, monkeypatch)
    assert asyncio.run(h.load_mcp_servers()) == 0


def test_approval_gate_request_returns_string_id():
    """request() returns a non-empty string ID."""
    from hive.core.approval import ApprovalGate
    gate = ApprovalGate()
    aid = gate.request("deploy", {}, "test", "danger")
    assert isinstance(aid, str) and len(aid) > 0


# --- Wave 3R: builtins / curator / executor / skill_store ----------------------

def test_read_file_tool_reads_content(tmp_path):
    """ReadFile.execute() returns the file's UTF-8 text."""
    from hive.tools.builtins import ReadFile
    p = tmp_path / "hello.txt"
    p.write_text("hello world", encoding="utf-8")
    result = asyncio.run(ReadFile().execute(path=str(p)))
    assert "hello world" in result.content
    assert result.success is True


def test_write_file_tool_creates_file(tmp_path):
    """WriteFile.execute() creates the file and returns a success result."""
    from hive.tools.builtins import WriteFile
    p = tmp_path / "out" / "data.txt"
    result = asyncio.run(WriteFile().execute(path=str(p), content="stored"))
    assert result.success is True
    assert p.read_text(encoding="utf-8") == "stored"


def test_web_get_blocks_loopback_url():
    """WebGet.execute() blocks requests to loopback addresses without making a network call."""
    from hive.tools.builtins import WebGet
    result = asyncio.run(WebGet().execute(url="http://127.0.0.1/secret"))
    assert result.success is False
    assert "blocked" in result.content.lower()


def test_shell_tool_returns_stdout():
    """Shell.execute() runs a command and captures stdout."""
    from hive.tools.builtins import Shell
    result = asyncio.run(Shell().execute(cmd="echo hive"))
    assert "hive" in result.content
    assert result.success is True


def test_skill_store_record_use_increments_count(tmp_path):
    """SkillUsageStore.record_use() registers and increments the use_count for a skill."""
    from hive.memory.curator import SkillUsageStore
    store = SkillUsageStore(tmp_path / "skills.db")
    store.record_use("test_skill")
    store.record_use("test_skill")
    skill = store.get("test_skill")
    assert skill is not None
    assert skill.use_count == 2


def test_curator_consolidate_skips_without_summarizer(tmp_path):
    """Curator without a summarizer returns skipped=True immediately."""
    from hive.memory.curator import Curator, SkillUsageStore
    store = SkillUsageStore(tmp_path / "skills.db")
    curator = Curator(store)
    result = asyncio.run(curator.consolidate_umbrellas())
    assert result.get("skipped") is True


# --- Wave 3Y-B additional tests --------------------------------------------------

def test_wave3y_write_file_append_mode(tmp_path):
    """WriteFile in append mode adds to existing content rather than overwriting."""
    from hive.tools.builtins import WriteFile
    p = tmp_path / "log.txt"
    p.write_text("line1\n", encoding="utf-8")
    result = asyncio.run(WriteFile().execute(path=str(p), content="line2\n", mode="a"))
    assert result.success is True
    assert p.read_text(encoding="utf-8") == "line1\nline2\n"


def test_wave3y_delete_file_nonexistent_returns_failure(tmp_path):
    """DeleteFile.execute() returns success=False when the file does not exist."""
    from hive.tools.builtins import DeleteFile
    result = asyncio.run(DeleteFile().execute(path=str(tmp_path / "no_such_file.txt")))
    assert result.success is False
    assert "not found" in result.content


def test_wave3y_delete_file_removes_existing(tmp_path):
    """DeleteFile.execute() removes the file and returns a success result."""
    from hive.tools.builtins import DeleteFile
    p = tmp_path / "to_delete.txt"
    p.write_text("bye", encoding="utf-8")
    result = asyncio.run(DeleteFile().execute(path=str(p)))
    assert result.success is True
    assert not p.exists()


def test_wave3y_spend_money_no_backend_returns_content():
    """SpendMoney.execute() returns a ToolResult describing the missing payment backend."""
    from hive.tools.builtins import SpendMoney
    result = asyncio.run(SpendMoney().execute(what="API credits", amount="20 USD"))
    assert "spend_money" in result.content
    assert "20 USD" in result.content or "API credits" in result.content


def test_wave3y_external_message_no_token_returns_error():
    """ExternalMessage without a token returns a ToolResult flagging the missing token."""
    from hive.tools.builtins import ExternalMessage
    result = asyncio.run(ExternalMessage().execute(to="user1", body="hello"))
    assert "TELEGRAM_BOT_TOKEN" in result.content or "external_message" in result.content


def test_wave3y_discover_tool_spec_not_dangerous():
    """DiscoverTool.spec.dangerous must be False — it is a read-only search tool."""
    from hive.tools.builtins import DiscoverTool
    assert DiscoverTool().spec.dangerous is False


def test_wave3y_executor_execute_approved_runs_tool():
    """execute_approved() bypasses the gate and returns DispatchStatus.OK."""
    from hive.tools.executor import ToolExecutor, DispatchStatus
    from hive.tools.base import BaseTool, ToolSpec
    from hive.core.types import ToolResult

    class _T(BaseTool):
        spec = ToolSpec(name="approved_tool", description="d", parameters={})
        async def execute(self, **kw): return ToolResult(tool_name="approved_tool", content="ran")

    class _Gate:
        def is_dangerous(self, *a, **k): return True
        def request(self, *a, **k): return "approval-id-123"

    ex = ToolExecutor({"approved_tool": _T()}, gate=_Gate())
    d = asyncio.run(ex.execute_approved("approved_tool", {}))
    assert d.status is DispatchStatus.OK
    assert d.result is not None and d.result.content == "ran"


def test_wave3y_agents_registry_contains_coder(tmp_path, monkeypatch):
    """'coder' specialist must be registered in agents_registry after build."""
    h = _hive(tmp_path, monkeypatch)
    assert "coder" in h.agents_registry


# --- Wave 4E additional tests ---------------------------------------------------

def test_wave4e_read_file_spec_category():
    """ReadFile.spec.category must be 'files'."""
    from hive.tools.builtins import ReadFile
    assert ReadFile().spec.category == "files"


def test_wave4e_shell_spec_category():
    """Shell.spec.category must be 'system'."""
    from hive.tools.builtins import Shell
    assert Shell().spec.category == "system"


def test_wave4e_spend_money_spec_dangerous():
    """SpendMoney.spec.dangerous must be True — it is a gated tool."""
    from hive.tools.builtins import SpendMoney
    assert SpendMoney().spec.dangerous is True


def test_wave4e_deploy_spec_dangerous():
    """Deploy.spec.dangerous must be True — systemctl restart is always gated."""
    from hive.tools.builtins import Deploy
    assert Deploy().spec.dangerous is True


def test_wave4e_skill_store_register_then_get(tmp_path):
    """SkillUsageStore.register() creates a row that get() can retrieve."""
    from hive.memory.curator import SkillUsageStore
    store = SkillUsageStore(tmp_path / "skills.db")
    store.register("my_skill", agent_created=True)
    skill = store.get("my_skill")
    assert skill is not None
    assert skill.name == "my_skill"
    assert skill.agent_created is True
    assert skill.use_count == 0


def test_wave4e_skill_store_by_state_active(tmp_path):
    """by_state('active') returns registered skills in active state."""
    from hive.memory.curator import SkillUsageStore, STATE_ACTIVE
    store = SkillUsageStore(tmp_path / "skills.db")
    store.register("alpha")
    store.register("beta")
    active = store.by_state(STATE_ACTIVE)
    names = [s.name for s in active]
    assert "alpha" in names and "beta" in names


def test_wave4e_curator_run_with_active_skills(tmp_path):
    """Curator.run() returns a report dict with 'skills' and 'transitions' keys."""
    from hive.memory.curator import Curator, SkillUsageStore
    store = SkillUsageStore(tmp_path / "skills.db")
    store.register("s1", agent_created=True)
    store.register("s2", agent_created=True)
    curator = Curator(store)
    report = curator.run()
    assert "skills" in report
    assert "transitions" in report
    assert report["skills"] == 2


def test_wave4e_register_builtins_returns_snapshot():
    """register_builtins() returns a dict mapping name to tool instance."""
    from hive.tools.builtins import register_builtins
    from hive.tools.registry import ToolRegistry
    reg = type("_SnapReg", (ToolRegistry,), {})()
    snapshot = register_builtins(reg)
    assert isinstance(snapshot, dict)
    assert "read_file" in snapshot
    assert "shell" in snapshot
    assert "discover" in snapshot


# --- Wave 4J additional tests (8) ---------------------------------------------

def test_wave4j_discover_spec_category():
    """DiscoverTool.spec.category must be 'discovery'."""
    from hive.tools.builtins import DiscoverTool
    assert DiscoverTool().spec.category == "discovery"


def test_wave4j_delete_file_spec_category():
    """DeleteFile.spec.category must be 'files'."""
    from hive.tools.builtins import DeleteFile
    assert DeleteFile().spec.category == "files"


def test_wave4j_write_file_spec_is_dangerous():
    """Every file mutation must pass through the approval boundary."""
    from hive.tools.builtins import WriteFile
    assert WriteFile().spec.dangerous is True


def test_wave4j_skill_store_top_used_returns_list(tmp_path):
    """SkillUsageStore.top_used() returns a list ordered by use_count descending."""
    from hive.memory.curator import SkillUsageStore
    store = SkillUsageStore(tmp_path / "skills.db")
    store.register("a")
    store.register("b")
    store.record_use("a")
    store.record_use("a")
    store.record_use("b")
    top = store.top_used(2)
    assert len(top) == 2
    assert top[0].name == "a"
    assert top[0].use_count == 2


def test_wave4j_skill_store_unused_skills(tmp_path):
    """SkillUsageStore.unused_skills() returns skills with use_count == 0."""
    from hive.memory.curator import SkillUsageStore
    store = SkillUsageStore(tmp_path / "skills.db")
    store.register("used")
    store.register("idle")
    store.record_use("used")
    unused = store.unused_skills()
    names = [s.name for s in unused]
    assert "idle" in names
    assert "used" not in names


def test_wave4j_skill_store_pin_and_unpin(tmp_path):
    """pin() sets pinned=True; unpin() sets pinned=False."""
    from hive.memory.curator import SkillUsageStore
    store = SkillUsageStore(tmp_path / "skills.db")
    store.register("p")
    store.pin("p")
    assert store.get("p").pinned is True
    store.unpin("p")
    assert store.get("p").pinned is False


def test_wave4j_executor_has_tool_false_for_unknown():
    """ToolExecutor.has_tool() returns False for an unregistered name."""
    from hive.tools.executor import ToolExecutor
    ex = ToolExecutor({})
    assert ex.has_tool("no_such_tool") is False


def test_wave4j_executor_dangerous_tool_returns_pending():
    """ToolExecutor.execute() on a dangerous tool returns PENDING status."""
    from hive.tools.executor import ToolExecutor, DispatchStatus
    from hive.tools.base import BaseTool, ToolSpec
    from hive.core.types import ToolResult

    class _D(BaseTool):
        spec = ToolSpec(name="d", description="d", parameters={})
        async def execute(self, **kw): return ToolResult(tool_name="d", content="done")

    class _Gate:
        def is_dangerous(self, *a, **k): return True
        def request(self, *a, **k): return "aid-xyz"

    ex = ToolExecutor({"d": _D()}, gate=_Gate())
    dispatch = asyncio.run(ex.execute("d", {}))
    assert dispatch.status is DispatchStatus.PENDING
    assert dispatch.approval_id == "aid-xyz"


# --- Wave 4P additional tests ---------------------------------------------------

def test_wave4p_delete_file_tool_removes_real_file(tmp_path):
    """DeleteFile.execute() removes a real temp file from disk."""
    from hive.tools.builtins import DeleteFile
    p = tmp_path / "real_file.txt"
    p.write_text("content", encoding="utf-8")
    assert p.exists()
    result = asyncio.run(DeleteFile().execute(path=str(p)))
    assert result.success is True
    assert not p.exists()


def test_wave4p_skill_store_record_use_returns_none(tmp_path):
    """SkillUsageStore.record_use() always returns None."""
    from hive.memory.curator import SkillUsageStore
    store = SkillUsageStore(tmp_path / "skills.db")
    ret = store.record_use("some_skill")
    assert ret is None


def test_wave4p_skill_store_by_state_nonexistent_returns_empty(tmp_path):
    """by_state() with a non-existent state returns an empty list."""
    from hive.memory.curator import SkillUsageStore
    store = SkillUsageStore(tmp_path / "skills.db")
    result = store.by_state("nonexistent_state_xyz")
    assert result == []


def test_wave4p_skill_store_set_pinned_then_get_shows_true(tmp_path):
    """set_pinned(name, True) then get() shows pinned=True."""
    from hive.memory.curator import SkillUsageStore
    store = SkillUsageStore(tmp_path / "skills.db")
    store.register("pinnable_skill")
    store.set_pinned("pinnable_skill", True)
    skill = store.get("pinnable_skill")
    assert skill is not None
    assert skill.pinned is True


def test_wave4p_delegate_to_specialist_tool_name():
    """DelegateToSpecialist tool_name from spec equals 'delegate_to_specialist'."""
    from hive.tools.builtins import DelegateToSpecialist
    assert DelegateToSpecialist().spec.name == "delegate_to_specialist"


def test_wave4p_web_get_file_scheme_returns_error_result():
    """WebGet.execute() with 'file://' scheme returns a blocked/error ToolResult."""
    from hive.tools.builtins import WebGet
    result = asyncio.run(WebGet().execute(url="file:///etc/passwd"))
    assert result.success is False
    assert "block" in result.content.lower() or "error" in result.content.lower()


def test_wave4p_discover_tool_in_register_builtins_snapshot():
    """register_builtins() snapshot contains 'discover' key."""
    from hive.tools.builtins import register_builtins
    from hive.tools.registry import ToolRegistry
    R = type("_R4p", (ToolRegistry,), {})
    reg = R()
    snapshot = register_builtins(reg)
    assert "discover" in snapshot


def test_wave4p_skill_store_set_pinned_false_then_get_shows_false(tmp_path):
    """set_pinned(name, False) after pinning shows pinned=False on get()."""
    from hive.memory.curator import SkillUsageStore
    store = SkillUsageStore(tmp_path / "skills.db")
    store.register("toggle_skill")
    store.set_pinned("toggle_skill", True)
    assert store.get("toggle_skill").pinned is True
    store.set_pinned("toggle_skill", False)
    assert store.get("toggle_skill").pinned is False
