"""M9 transport — SSE MCP client, MNEMOSYNE_MCP_URL consumption, MCP serve-side, CLI."""
from __future__ import annotations

import asyncio

import pytest

from hive.core.config import HiveConfig
from hive.llm.adapters.base import CompletionResult
from hive.runtime import HiveOS


class _Router:
    async def complete(self, *a, **k): return CompletionResult(text="ok", model="m")
    async def aclose(self): pass


def _hive(tmp_path, monkeypatch) -> HiveOS:
    monkeypatch.setattr("hive.runtime.build_mnemosyne_provider", lambda **kw: None)
    return HiveOS.build(HiveConfig.from_env(root=tmp_path, load_dotenv=False), router=_Router())


# --- MCPClient transport selection --------------------------------------------

def test_mcp_client_records_url_vs_command():
    from hive.tools.mcp.client import MCPClient
    assert MCPClient(url="https://x/sse")._url == "https://x/sse"
    c = MCPClient("npx", ["-y", "server"])
    assert c._command == "npx" and c._args == ["-y", "server"] and c._url == ""


class _FakeMCP:
    """Captures how it was constructed; serves one tool."""
    instances: list = []

    def __init__(self, command="", args=None, *, url=""):
        self.command, self.args, self.url = command, args or [], url
        _FakeMCP.instances.append(self)

    async def connect(self): pass
    async def list_tools(self):
        return [{"name": "remember", "description": "d", "inputSchema": {}}]
    async def call(self, name, args): return "ok"
    def as_tools(self, descriptors, *, prefix=""):
        from hive.tools.mcp.client import MCPTool, mcp_tool_to_spec
        return [MCPTool(mcp_tool_to_spec(d, prefix=prefix), self.call,
                        remote_name=d.get("name", "")) for d in descriptors]


def test_load_routes_url_spec_to_sse(tmp_path, monkeypatch):
    _FakeMCP.instances = []
    monkeypatch.setenv("HIVE_MCP_SERVERS", "https://remote.example/sse")
    h = _hive(tmp_path, monkeypatch)
    monkeypatch.setattr("hive.tools.mcp.client.MCPClient", _FakeMCP)
    n = asyncio.run(h.load_mcp_servers())
    assert n == 1
    assert _FakeMCP.instances[0].url == "https://remote.example/sse"  # SSE, not stdio


def test_load_consumes_mnemosyne_mcp_url(tmp_path, monkeypatch):
    _FakeMCP.instances = []
    monkeypatch.setenv("MNEMOSYNE_MCP_URL", "https://mnemo.local/sse")
    h = _hive(tmp_path, monkeypatch)
    monkeypatch.setattr("hive.tools.mcp.client.MCPClient", _FakeMCP)
    n = asyncio.run(h.load_mcp_servers())
    assert n == 1 and _FakeMCP.instances[0].url == "https://mnemo.local/sse"
    assert any(name.endswith(".remember") for name in h.tools)


def test_load_mixed_stdio_and_url(tmp_path, monkeypatch):
    _FakeMCP.instances = []
    monkeypatch.setenv("HIVE_MCP_SERVERS", "localcmd --flag;https://remote/sse")
    h = _hive(tmp_path, monkeypatch)
    monkeypatch.setattr("hive.tools.mcp.client.MCPClient", _FakeMCP)
    n = asyncio.run(h.load_mcp_servers())
    assert n == 2
    transports = {("sse" if i.url else "stdio") for i in _FakeMCP.instances}
    assert transports == {"sse", "stdio"}


# --- MCP serve-side ------------------------------------------------------------

def test_serve_mcp_uses_mcp_server(tmp_path, monkeypatch):
    h = _hive(tmp_path, monkeypatch)
    served = {}

    class _FakeServer:
        def __init__(self, tools, *, name=""):
            served["tools"], served["name"] = tools, name
        async def serve_stdio(self):
            served["ran"] = True

    monkeypatch.setattr("hive.tools.mcp.server.MCPServer", _FakeServer)
    asyncio.run(h.serve_mcp())
    assert served.get("ran") and served["name"] == "hive" and "read_file" in served["tools"]


def test_mcp_server_listing_is_deterministic(tmp_path, monkeypatch):
    from hive.tools.mcp.server import MCPServer
    h = _hive(tmp_path, monkeypatch)
    listing = MCPServer(h.tools).listing()
    names = [d["name"] for d in listing]
    assert names == sorted(names) and "discover" in names


# --- CLI mcp-serve -------------------------------------------------------------

def test_cli_lists_mcp_serve(capsys):
    from hive.surfaces import cli
    assert cli.main(["nonsense"]) == 2
    assert "mcp-serve" in capsys.readouterr().err


def test_cli_mcp_serve_dispatches(tmp_path, monkeypatch):
    from hive.surfaces import cli

    async def _fake_serve(self):
        _fake_serve.ran = True
    _fake_serve.ran = False
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setattr("hive.runtime.build_mnemosyne_provider", lambda **kw: None)
    monkeypatch.setattr(HiveOS, "serve_mcp", _fake_serve)
    assert cli.main(["mcp-serve"]) == 0
    assert _fake_serve.ran


# --- Additional transport / MCPClient tests -------------------------------------------

def test_mcp_client_default_command_is_empty():
    """MCPClient with no args has empty command, args list, and url."""
    from hive.tools.mcp.client import MCPClient
    c = MCPClient()
    assert c._command == ""
    assert c._args == []
    assert c._url == ""


def test_mcp_client_url_transport_stores_url():
    """MCPClient constructed with url= uses SSE transport (url stored, command empty)."""
    from hive.tools.mcp.client import MCPClient
    c = MCPClient(url="https://example.com/sse")
    assert c._url == "https://example.com/sse"
    assert c._command == ""


def test_mcp_client_stdio_transport_stores_command_and_args():
    """MCPClient constructed with a command stores both command and args."""
    from hive.tools.mcp.client import MCPClient
    c = MCPClient("uvx", ["my-server", "--port", "9000"])
    assert c._command == "uvx"
    assert c._args == ["my-server", "--port", "9000"]
    assert c._url == ""


def test_mcp_client_args_default_empty_list():
    """MCPClient with command but no args stores an empty list, not None."""
    from hive.tools.mcp.client import MCPClient
    c = MCPClient("npx")
    assert c._args == []


def test_mcp_client_as_tools_wraps_descriptors():
    """as_tools() converts MCP tool descriptors into MCPTool instances."""
    from hive.tools.mcp.client import MCPClient, MCPTool

    async def _fake_caller(name, args): return "result"

    c = MCPClient(url="https://x/sse")
    c.call = _fake_caller  # inject a fake caller

    descriptors = [
        {"name": "do_thing", "description": "does a thing", "inputSchema": {}},
        {"name": "do_other", "description": "does another", "inputSchema": {}},
    ]
    tools = c.as_tools(descriptors)
    assert len(tools) == 2
    assert all(isinstance(t, MCPTool) for t in tools)
    names = {t.spec.name for t in tools}
    assert names == {"do_thing", "do_other"}


def test_mcp_client_as_tools_applies_prefix():
    """as_tools() with prefix= prepends the prefix to each tool name."""
    from hive.tools.mcp.client import MCPClient

    c = MCPClient(url="https://x/sse")
    descriptors = [{"name": "search", "description": "search", "inputSchema": {}}]
    tools = c.as_tools(descriptors, prefix="mnemo.")
    assert tools[0].spec.name == "mnemo.search"


def test_mcp_client_as_tools_marks_dangerous():
    """All tools created via as_tools() are dangerous (external untrusted capability)."""
    from hive.tools.mcp.client import MCPClient

    c = MCPClient(url="https://x/sse")
    descriptors = [{"name": "rm_rf", "description": "deletes things", "inputSchema": {}}]
    tools = c.as_tools(descriptors)
    assert tools[0].spec.dangerous is True


def test_mcp_client_as_tools_sets_category_mcp():
    """Tools from as_tools() have category='mcp' so they can be filtered."""
    from hive.tools.mcp.client import MCPClient

    c = MCPClient("npx", ["-y", "server"])
    descriptors = [{"name": "ping", "description": "ping", "inputSchema": {}}]
    tools = c.as_tools(descriptors)
    assert tools[0].spec.category == "mcp"


def test_mcp_tool_execute_calls_remote_with_correct_args():
    """MCPTool.execute() passes the remote name and arguments to the caller."""
    import asyncio
    from hive.tools.mcp.client import MCPTool, mcp_tool_to_spec

    calls = []

    async def _caller(name, args):
        calls.append((name, args))
        return "pong"

    spec = mcp_tool_to_spec({"name": "ping", "description": "ping", "inputSchema": {}})
    tool = MCPTool(spec, _caller, remote_name="ping")
    result = asyncio.run(tool.execute(value="x"))
    assert calls == [("ping", {"value": "x"})]
    assert result.content == "pong"


def test_load_no_mcp_servers_returns_zero(tmp_path, monkeypatch):
    """load_mcp_servers returns 0 when no env vars configure MCP servers."""
    monkeypatch.delenv("HIVE_MCP_SERVERS", raising=False)
    monkeypatch.delenv("MNEMOSYNE_MCP_URL", raising=False)
    h = _hive(tmp_path, monkeypatch)
    n = asyncio.run(h.load_mcp_servers())
    assert n == 0


# --- New tests (batch 2) -------------------------------------------------------

def test_mcp_tool_to_spec_sets_name_from_descriptor():
    """mcp_tool_to_spec must use the 'name' key from the descriptor."""
    from hive.tools.mcp.client import mcp_tool_to_spec
    spec = mcp_tool_to_spec({"name": "my_tool", "description": "does stuff", "inputSchema": {}})
    assert spec.name == "my_tool"


def test_mcp_tool_to_spec_prefix_prepended():
    """mcp_tool_to_spec with prefix= prepends it to the tool name."""
    from hive.tools.mcp.client import mcp_tool_to_spec
    spec = mcp_tool_to_spec({"name": "search", "description": "search", "inputSchema": {}},
                             prefix="mnemo.")
    assert spec.name == "mnemo.search"


def test_mcp_tool_execute_returns_tool_result():
    """MCPTool.execute() must return a ToolResult instance."""
    import asyncio
    from hive.core.types import ToolResult
    from hive.tools.mcp.client import MCPTool, mcp_tool_to_spec

    async def _caller(name, args):
        return "response"

    spec = mcp_tool_to_spec({"name": "ping", "description": "ping", "inputSchema": {}})
    tool = MCPTool(spec, _caller, remote_name="ping")
    result = asyncio.run(tool.execute())
    assert isinstance(result, ToolResult)
    assert result.content == "response"


def test_mcp_server_listing_contains_inputSchema_key():
    """Each entry in MCPServer.listing() must have an 'inputSchema' key."""
    from hive.tools.mcp.server import MCPServer
    from hive.tools.builtins import ReadFile
    server = MCPServer({"read_file": ReadFile()})
    for entry in server.listing():
        assert "inputSchema" in entry


def test_mcp_server_listing_contains_description_key():
    """Each entry in MCPServer.listing() must have a non-empty 'description' key."""
    from hive.tools.mcp.server import MCPServer
    from hive.tools.builtins import ReadFile
    server = MCPServer({"read_file": ReadFile()})
    for entry in server.listing():
        assert "description" in entry
        assert isinstance(entry["description"], str)


def test_mcp_server_name_defaults_to_hive():
    """MCPServer must default its name to 'hive' when no name is given."""
    from hive.tools.mcp.server import MCPServer
    import inspect
    sig = inspect.signature(MCPServer.__init__)
    default_name = sig.parameters["name"].default
    assert default_name == "hive"
