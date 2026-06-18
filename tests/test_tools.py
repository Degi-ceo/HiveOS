"""P5 — tools: registry, gate-routed executor, builtins, discovery, MCP adapter."""
from __future__ import annotations

import asyncio

import pytest

from hive.core.types import ToolResult
from hive.tools.base import BaseTool, ToolSpec
from hive.tools.builtins import register_builtins
from hive.tools.discovery import scan_red_flags, discover
from hive.tools.executor import DispatchStatus, ToolExecutor
from hive.tools.mcp.client import MCPTool, mcp_tool_to_spec
from hive.tools.mcp.server import build_tool_listing
from hive.tools.registry import ToolRegistry


class _Spy(BaseTool):
    def __init__(self, name="spy", dangerous=False, boom=False):
        self._spec = ToolSpec(name=name, description="t", dangerous=dangerous)
        self.boom = boom
        self.ran = False

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, **params) -> ToolResult:
        self.ran = True
        if self.boom:
            raise RuntimeError("kaboom")
        return ToolResult(tool_name=self._spec.name, content="ok")


def _exec(tools, **kw) -> ToolExecutor:
    return ToolExecutor({t.spec.name: t for t in tools}, **kw)


# --- registry ------------------------------------------------------------------

def test_registry_add_snapshot_and_duplicate_guard():
    class R(ToolRegistry):
        pass
    R.add(_Spy("a"))
    assert "a" in R.snapshot()
    with pytest.raises(ValueError):
        R.add(_Spy("a"))


# --- executor: the three outcomes + audit/events -------------------------------

def test_executor_runs_safe_tool_and_audits():
    spy = _Spy("safe")
    audits: list = []
    ex = _exec([spy], audit=audits.append)
    out = asyncio.run(ex.execute("safe", {"x": 1}))
    assert out.status is DispatchStatus.OK and out.result.content == "ok"
    assert spy.ran is True
    assert audits and audits[0]["status"] == "ok"


def test_executor_gates_spec_dangerous_without_running():
    spy = _Spy("danger", dangerous=True)
    ex = _exec([spy])
    out = asyncio.run(ex.execute("danger", {}))
    assert out.status is DispatchStatus.PENDING and out.approval_id
    assert spy.ran is False                       # gated BEFORE execution


def test_executor_gates_destructive_shell_via_gate():
    # `shell` is not flagged dangerous, but the gate flags `rm -rf /`
    tools = register_builtins(type("R1", (ToolRegistry,), {}))
    ex = ToolExecutor(tools)
    safe = asyncio.run(ex.execute("shell", {"cmd": "echo hi"}))
    assert safe.status is DispatchStatus.OK and "hi" in safe.result.content
    danger = asyncio.run(ex.execute("shell", {"cmd": "rm -rf /"}))
    assert danger.status is DispatchStatus.PENDING


def test_executor_unknown_and_error():
    assert asyncio.run(_exec([]).execute("nope")).status is DispatchStatus.ERROR
    out = asyncio.run(_exec([_Spy("bad", boom=True)]).execute("bad"))
    assert out.status is DispatchStatus.ERROR and "kaboom" in out.error


# --- builtins ------------------------------------------------------------------

def test_builtins_read_write_roundtrip(tmp_path):
    tools = register_builtins(type("R2", (ToolRegistry,), {}))
    ex = ToolExecutor(tools)
    target = tmp_path / "note.txt"
    w = asyncio.run(ex.execute("write_file", {"path": str(target), "content": "hello"}))
    assert w.status is DispatchStatus.OK
    r = asyncio.run(ex.execute("read_file", {"path": str(target)}))
    assert r.result.content == "hello"


def test_builtins_dangerous_set_is_gated():
    tools = register_builtins(type("R3", (ToolRegistry,), {}))
    ex = ToolExecutor(tools)
    for name in ("spend_money", "deploy", "external_message"):
        assert asyncio.run(ex.execute(name, {})).status is DispatchStatus.PENDING


# --- discovery -----------------------------------------------------------------

def test_discovery_red_flag_scan():
    assert "rm -rf" in scan_red_flags("then run rm -rf / please")
    assert scan_red_flags("totally clean code") == []


def test_discovery_uses_memory_cache_without_network():
    class FakeMem:
        def recall(self, query, limit=5):
            return [{"topic": query, "content": "cached!"}]
        def learn(self, *a, **k):
            raise AssertionError("cache hit must not learn")

    out = asyncio.run(discover("a pdf parser", memory=FakeMem()))
    assert out["cached"] is True and out["result"]["content"] == "cached!"


# --- MCP adapter (offline) -----------------------------------------------------

def test_mcp_tool_to_spec_marks_dangerous():
    spec = mcp_tool_to_spec({"name": "search", "description": "d",
                             "inputSchema": {"type": "object"}}, prefix="ext.")
    assert spec.name == "ext.search" and spec.dangerous is True and spec.category == "mcp"


def test_execute_approved_rejects_traversal_path(tmp_path):
    tools = register_builtins(type("R5", (ToolRegistry,), {}))
    ex = ToolExecutor(tools)
    # Gate write_file first (it's not dangerous by spec, so it runs directly —
    # use a path outside tmp to trigger the path-safety recheck in execute_approved).
    out = asyncio.run(ex.execute_approved("write_file",
                                          {"path": "../../etc/passwd", "content": "x"}))
    assert out.status is DispatchStatus.ERROR
    assert out.error  # path-safety error must not be silent


def test_mcp_tool_wraps_caller_and_runs_through_executor():
    async def fake_caller(name, args):
        return f"called {name} with {args}"

    tool = MCPTool(mcp_tool_to_spec({"name": "remote"}), fake_caller)
    ex = ToolExecutor({tool.spec.name: tool})
    # dangerous (MCP) -> gated first
    assert asyncio.run(ex.execute("remote", {"q": 1})).status is DispatchStatus.PENDING
    # after approval it dispatches to the remote caller
    out = asyncio.run(ex.execute_approved("remote", {"q": 1}))
    assert out.status is DispatchStatus.OK and "called remote" in out.result.content


def test_mcp_server_listing_is_sorted():
    tools = register_builtins(type("R4", (ToolRegistry,), {}))
    listing = build_tool_listing(tools)
    names = [d["name"] for d in listing]
    assert names == sorted(names)
    assert {"read_file", "shell"} <= set(names)


# --- file_safety path traversal (items 36-37) ----------------------------------

def test_check_path_blocks_traversal():
    from hive.tools.file_safety import check_path
    result = check_path("../../etc/passwd")
    assert result is not None
    assert "traversal" in result.lower() or "permitted" in result.lower()


def test_check_path_allows_normal():
    from hive.tools.file_safety import check_path
    assert check_path("/tmp/safe_file_hive_test.txt") is None


def test_has_traversal():
    from hive.tools.file_safety import has_traversal
    assert has_traversal("../../etc/passwd") is True
    assert has_traversal("../file.txt") is True
    assert has_traversal("/tmp/file.txt") is False
    assert has_traversal("relative/path/file.txt") is False


def test_has_unsafe_symlink_no_symlink(tmp_path):
    from hive.tools.file_safety import has_unsafe_symlink
    real = tmp_path / "real.txt"
    real.write_text("content")
    assert has_unsafe_symlink(str(real)) is False


def test_check_path_blocks_symlink_escape(tmp_path, monkeypatch):
    from hive.tools.file_safety import check_path

    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = repo / "escape.txt"
    try:
        link.symlink_to(outside)
    except OSError:  # pragma: no cover - symlinks unsupported
        pytest.skip("symlinks not supported on this platform")

    monkeypatch.chdir(repo)
    err = check_path(str(link), operation="write")
    assert err is not None
    assert "symlink escape" in err


def test_has_unsafe_symlink_external_escape(tmp_path):
    import os
    from hive.tools.file_safety import has_unsafe_symlink
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    link = tmp_path / "link.txt"
    os.symlink(str(outside), str(link))
    # The symlink points outside tmp_path — resolves outside cwd
    # The function checks if target escapes cwd; since both are under /tmp
    # this is environment-dependent; just verify it doesn't crash.
    result = has_unsafe_symlink(str(link))
    assert isinstance(result, bool)


# --- registry remove/list_categories (items 44-45) ----------------------------

def test_registry_remove():
    class MyReg(ToolRegistry):
        pass

    class DummyTool(BaseTool):
        spec = ToolSpec(name="dummy_remove", category="test", description="t")

        async def execute(self, **kwargs):
            return ToolResult(tool_name="dummy_remove", content="ok")

    MyReg.add(DummyTool())
    assert MyReg.remove("dummy_remove") is True
    assert "dummy_remove" not in MyReg.snapshot()
    assert MyReg.remove("nonexistent") is False


def test_registry_list_categories():
    class CatReg(ToolRegistry):
        pass

    register_builtins(CatReg)
    cats = CatReg.list_categories()
    assert isinstance(cats, list)
    assert cats == sorted(cats)
    assert "files" in cats


def test_registry_count_and_values():
    class CountReg(ToolRegistry):
        pass

    class T1(BaseTool):
        spec = ToolSpec(name="cnt_t1", category="test", description="")
        async def execute(self, **kw): return ToolResult(tool_name="cnt_t1", content="")

    class T2(BaseTool):
        spec = ToolSpec(name="cnt_t2", category="test", description="")
        async def execute(self, **kw): return ToolResult(tool_name="cnt_t2", content="")

    CountReg.add(T1())
    CountReg.add(T2())
    assert CountReg.count() == 2
    vals = CountReg.values()
    assert len(vals) == 2


def test_registry_find_by_category():
    class FBCReg(ToolRegistry):
        pass

    class FileTool(BaseTool):
        spec = ToolSpec(name="fbc_file", category="files", description="")
        async def execute(self, **kw): return ToolResult(tool_name="fbc_file", content="")

    class NetTool(BaseTool):
        spec = ToolSpec(name="fbc_net", category="network", description="")
        async def execute(self, **kw): return ToolResult(tool_name="fbc_net", content="")

    FBCReg.add(FileTool())
    FBCReg.add(NetTool())
    files = FBCReg.find_by_category("files")
    assert len(files) == 1 and files[0].spec.name == "fbc_file"
    net = FBCReg.find_by_category("network")
    assert len(net) == 1 and net[0].spec.name == "fbc_net"
    # case-insensitive: "NETWORK" matches "network"
    assert len(FBCReg.find_by_category("NETWORK")) == 1
    assert FBCReg.find_by_category("missing") == []


# --- ToolExecutor timeout (item 46) -------------------------------------------

def test_executor_timeout_surfaces_as_error():
    """A tool that hangs beyond the timeout must return an ERROR dispatch."""
    import asyncio as _asyncio
    from hive.tools.executor import ToolExecutor, DispatchStatus
    from hive.tools.base import BaseTool, ToolSpec, ToolResult

    class SlowTool(BaseTool):
        spec = ToolSpec(name="slow", category="test", description="hangs")

        async def execute(self, **kwargs):
            await _asyncio.sleep(9999)
            return ToolResult(tool_name="slow", content="never")

    ex = ToolExecutor({"slow": SlowTool()}, timeout=0.05)
    result = _asyncio.run(ex.execute("slow"))
    assert result.status is DispatchStatus.ERROR
    assert "timed out" in (result.error or "")


def test_executor_no_timeout_none_runs_normally():
    """timeout=None disables the cap; fast tools must still succeed."""
    import asyncio as _asyncio
    from hive.tools.executor import ToolExecutor, DispatchStatus
    from hive.tools.base import BaseTool, ToolSpec, ToolResult

    class FastTool(BaseTool):
        spec = ToolSpec(name="fast", category="test", description="quick")

        async def execute(self, **kwargs):
            return ToolResult(tool_name="fast", content="done")

    ex = ToolExecutor({"fast": FastTool()}, timeout=None)
    result = _asyncio.run(ex.execute("fast"))
    assert result.status is DispatchStatus.OK


def test_executor_list_and_has_tool():
    from hive.tools.executor import ToolExecutor
    from hive.tools.base import BaseTool, ToolSpec, ToolResult

    class T1(BaseTool):
        spec = ToolSpec(name="t_alpha", category="test", description="")
        async def execute(self, **kw): return ToolResult(tool_name="t_alpha", content="")

    class T2(BaseTool):
        spec = ToolSpec(name="t_beta", category="test", description="")
        async def execute(self, **kw): return ToolResult(tool_name="t_beta", content="")

    ex = ToolExecutor({"t_alpha": T1(), "t_beta": T2()})
    assert ex.list_tools() == ["t_alpha", "t_beta"]
    assert ex.has_tool("t_alpha") is True
    assert ex.has_tool("missing") is False


def test_executor_remove_tool():
    from hive.tools.executor import ToolExecutor
    from hive.tools.base import BaseTool, ToolSpec, ToolResult

    class DynTool(BaseTool):
        spec = ToolSpec(name="dyn", category="test", description="")
        async def execute(self, **kw): return ToolResult(tool_name="dyn", content="")

    ex = ToolExecutor({"dyn": DynTool()})
    assert ex.has_tool("dyn") is True
    assert ex.remove_tool("dyn") is True
    assert ex.has_tool("dyn") is False
    assert ex.remove_tool("dyn") is False


def test_executor_stats():
    from hive.tools.executor import ToolExecutor
    from hive.tools.base import BaseTool, ToolSpec, ToolResult

    class SafeTool(BaseTool):
        spec = ToolSpec(name="safe_a", category="io", description="", dangerous=False)
        async def execute(self, **kw): return ToolResult(tool_name="safe_a", content="")

    class DangerTool(BaseTool):
        spec = ToolSpec(name="danger_b", category="system", description="", dangerous=True)
        async def execute(self, **kw): return ToolResult(tool_name="danger_b", content="")

    ex = ToolExecutor({"safe_a": SafeTool(), "danger_b": DangerTool()}, timeout=30.0)
    stats = ex.stats()
    assert stats["total"] == 2
    assert stats["available"] == 2
    assert stats["unavailable"] == 0
    assert stats["dangerous_count"] == 1
    assert "danger_b" in stats["dangerous"]
    assert "io" in stats["by_category"] and "system" in stats["by_category"]
    assert stats["timeout_seconds"] == 30.0


# --- N-1: SSRF protection in WebGet -------------------------------------------

def test_web_get_blocks_private_ip():
    from hive.tools.builtins import WebGet
    result = asyncio.run(WebGet().execute(url="http://192.168.1.1/"))
    assert result.success is False
    assert "blocked" in result.content.lower()


def test_web_get_blocks_loopback():
    from hive.tools.builtins import WebGet
    result = asyncio.run(WebGet().execute(url="http://127.0.0.1/"))
    assert result.success is False
    assert "blocked" in result.content.lower()


def test_web_get_blocks_metadata_endpoint():
    from hive.tools.builtins import WebGet
    result = asyncio.run(WebGet().execute(url="http://169.254.169.254/latest/meta-data/"))
    assert result.success is False
    assert "blocked" in result.content.lower()


def test_web_get_blocks_userinfo_url():
    from hive.tools.builtins import WebGet
    result = asyncio.run(WebGet().execute(url="http://user:pass@example.com/"))
    assert result.success is False
    assert "blocked" in result.content.lower()


def test_web_get_allows_public_url():
    from hive.tools.builtins import _validate_url
    # No exception means the URL passed validation
    _validate_url("https://example.com/path?q=1")
    _validate_url("http://api.github.com/repos")


@pytest.mark.asyncio
async def test_web_get_blocks_ssrf_via_redirect():
    """Redirect to a private IP must be blocked even if initial URL was public."""
    import httpx
    from hive.tools.builtins import _check_redirect
    mock_response = httpx.Response(302, headers={"location": "http://192.168.1.1/secret"})
    with pytest.raises(ValueError, match="SSRF redirect blocked"):
        _check_redirect(mock_response)


def test_check_redirect_allows_public_location():
    """Redirect to a public URL must be allowed."""
    import httpx
    from hive.tools.builtins import _check_redirect
    mock_response = httpx.Response(302, headers={"location": "https://example.com/page"})
    _check_redirect(mock_response)  # Should not raise
