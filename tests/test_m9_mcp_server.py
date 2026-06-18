"""M9-a — MCP server wiring: hive.mcp_server() + build_tool_listing + CLI command."""
from __future__ import annotations

import asyncio

import pytest

from hive.core.config import HiveConfig
from hive.tools.mcp.server import MCPServer, build_tool_listing


# --- reuse helper from test_runtime ------------------------------------------------

class _ScriptRouter:
    async def complete(self, messages, kind=None, *, system=None, tools=None, **kw):
        from hive.llm.adapters.base import CompletionResult
        return CompletionResult(text="ok", model="fake")

    async def aclose(self):
        pass


def _build(tmp_path):
    from hive.runtime import HiveOS
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return HiveOS.build(cfg, router=_ScriptRouter())


# --- build_tool_listing (pure, no network) ------------------------------------------

def test_build_tool_listing_shape():
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _Dummy(BaseTool):
        spec = ToolSpec(name="dummy", description="a test tool", dangerous=False)
        async def execute(self, **_): return ToolResult(content="ok")

    listing = build_tool_listing({"dummy": _Dummy()})
    assert len(listing) == 1
    entry = listing[0]
    assert entry["name"] == "dummy"
    assert entry["description"] == "a test tool"
    assert "inputSchema" in entry


def test_build_tool_listing_deterministic_order():
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    def _make(name):
        class T(BaseTool):
            spec = ToolSpec(name=name, description=name, dangerous=False)
            async def execute(self, **_): return ToolResult(content="ok")
        return T()

    tools = {"c": _make("c"), "a": _make("a"), "b": _make("b")}
    names = [e["name"] for e in build_tool_listing(tools)]
    assert names == sorted(names)


# --- MCPServer.listing() via runtime ------------------------------------------------

def test_mcp_server_listing_contains_builtins(tmp_path):
    hive = _build(tmp_path)
    server = hive.mcp_server()
    assert isinstance(server, MCPServer)
    names = {e["name"] for e in server.listing()}
    for builtin in ("read_file", "write_file", "shell", "web_get", "discover"):
        assert builtin in names, f"builtin {builtin!r} missing from MCP listing"


def test_mcp_server_listing_schema_shape(tmp_path):
    hive = _build(tmp_path)
    for entry in hive.mcp_server().listing():
        assert "name" in entry
        assert "description" in entry
        assert "inputSchema" in entry


def test_mcp_server_default_name(tmp_path):
    hive = _build(tmp_path)
    assert hive.mcp_server()._name == "hive"
    assert hive.mcp_server(name="custom")._name == "custom"


# --- CLI command registration --------------------------------------------------------

def test_cli_mcp_serve_is_registered():
    """mcp-serve must appear in the unknown-command error message so we know it's wired."""
    import io, sys
    from hive.surfaces.cli import main
    buf = io.StringIO()
    sys.stderr = buf
    try:
        rc = main(["__no_such_command__"])
    finally:
        sys.stderr = sys.__stderr__
    assert rc == 2
    assert "mcp-serve" in buf.getvalue()


# --- Additional MCPServer tests -------------------------------------------------------

def test_mcp_server_listing_multiple_tools():
    """list_tools returns all registered tools when multiple are present."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    def _make(name):
        class T(BaseTool):
            spec = ToolSpec(name=name, description=f"desc:{name}", dangerous=False)
            async def execute(self, **_): return ToolResult(content="ok")
        return T()

    tools = {n: _make(n) for n in ("alpha", "beta", "gamma")}
    server = MCPServer(tools)
    listing = server.listing()
    names = [e["name"] for e in listing]
    assert set(names) == {"alpha", "beta", "gamma"}
    assert len(listing) == 3


def test_mcp_server_listing_with_parameters_schema():
    """A tool whose ToolSpec includes a parameters schema exposes it as inputSchema."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    class _FileTool(BaseTool):
        spec = ToolSpec(name="file_reader", description="reads a file",
                        parameters=schema, dangerous=False)
        async def execute(self, **_): return ToolResult(content="data")

    server = MCPServer({"file_reader": _FileTool()})
    entry = server.listing()[0]
    assert entry["inputSchema"] == schema


def test_mcp_server_listing_unknown_tool_not_present():
    """MCPServer only lists tools that were actually registered."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="only_one", description="d", dangerous=False)
        async def execute(self, **_): return ToolResult(content="ok")

    server = MCPServer({"only_one": _T()})
    names = {e["name"] for e in server.listing()}
    assert "nonexistent_tool" not in names
    assert "only_one" in names


def test_mcp_server_tool_execution_returns_content():
    """Calling a registered tool's execute() returns the expected content string."""
    import asyncio
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _Greeter(BaseTool):
        spec = ToolSpec(name="greet", description="says hello", dangerous=False)
        async def execute(self, **kwargs):
            name = kwargs.get("name", "world")
            return ToolResult(tool_name="greet", content=f"hello {name}")

    server = MCPServer({"greet": _Greeter()})
    # Execute via the internal tool directly (serve_stdio needs the mcp SDK)
    tool = server._tools["greet"]
    result = asyncio.run(tool.execute(name="hive"))
    assert result.content == "hello hive"


def test_mcp_server_close_does_not_raise():
    """MCPServer can be created and garbage-collected without error (no close() needed)."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="t", description="d", dangerous=False)
        async def execute(self, **_): return ToolResult(content="ok")

    server = MCPServer({"t": _T()})
    # Should not raise; MCPServer has no explicit close() — verify del is safe
    del server


def test_mcp_server_empty_tools_returns_empty_listing():
    """MCPServer with no tools produces an empty listing."""
    server = MCPServer({})
    assert server.listing() == []


def test_build_tool_listing_empty_tools():
    """build_tool_listing with an empty map returns an empty list."""
    assert build_tool_listing({}) == []


def test_mcp_server_custom_name_stored():
    """MCPServer stores the provided name for the MCP protocol handshake."""
    server = MCPServer({}, name="my-agent")
    assert server._name == "my-agent"


# --- Six additional MCP server tests -------------------------------------------

def test_build_tool_listing_description_matches_spec():
    """build_tool_listing uses the tool's spec.description verbatim."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="described", description="my unique description", dangerous=False)
        async def execute(self, **_): return ToolResult(content="ok")

    listing = build_tool_listing({"described": _T()})
    assert listing[0]["description"] == "my unique description"


def test_build_tool_listing_default_schema_when_no_parameters():
    """A tool with no explicit parameters gets the default empty-object inputSchema."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="noparams", description="no params", dangerous=False)
        async def execute(self, **_): return ToolResult(content="ok")

    listing = build_tool_listing({"noparams": _T()})
    schema = listing[0]["inputSchema"]
    assert schema.get("type") == "object"
    assert "properties" in schema


def test_mcp_server_listing_is_sorted_alphabetically():
    """MCPServer.listing() returns tools in alphabetical order by name."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    def _make(name):
        class T(BaseTool):
            spec = ToolSpec(name=name, description=name, dangerous=False)
            async def execute(self, **_): return ToolResult(content="ok")
        return T()

    server = MCPServer({"zebra": _make("zebra"), "apple": _make("apple"), "mango": _make("mango")})
    names = [e["name"] for e in server.listing()]
    assert names == ["apple", "mango", "zebra"]


def test_mcp_server_listing_entries_have_all_required_keys():
    """Every entry in listing() has exactly name, description, and inputSchema keys."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="checker", description="check keys", dangerous=False)
        async def execute(self, **_): return ToolResult(content="ok")

    server = MCPServer({"checker": _T()})
    entry = server.listing()[0]
    assert set(entry.keys()) == {"name", "description", "inputSchema"}


def test_build_tool_listing_single_tool_name():
    """build_tool_listing with a single tool returns one entry with the correct name."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="solo", description="only tool", dangerous=False)
        async def execute(self, **_): return ToolResult(content="ok")

    listing = build_tool_listing({"solo": _T()})
    assert len(listing) == 1
    assert listing[0]["name"] == "solo"


def test_mcp_server_tool_execution_error_safe():
    """A tool that raises does not crash the server — the exception propagates from execute()."""
    import asyncio
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _Broken(BaseTool):
        spec = ToolSpec(name="broken", description="always fails", dangerous=False)
        async def execute(self, **_):
            raise ValueError("intentional failure")

    server = MCPServer({"broken": _Broken()})
    tool = server._tools["broken"]
    with pytest.raises(ValueError, match="intentional failure"):
        asyncio.run(tool.execute())


def test_mcp_server_default_name_is_hive():
    """MCPServer defaults to the name 'hive' when no name is provided."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="t", description="d", dangerous=False)
        async def execute(self, **_): return ToolResult(content="ok")

    server = MCPServer({"t": _T()})
    assert server._name == "hive"
