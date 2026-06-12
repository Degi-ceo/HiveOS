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
