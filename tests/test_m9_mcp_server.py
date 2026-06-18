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


# --- Six additional MCP server tests (appended) ------------------------------------

def test_mcp_server_listing_dangerous_tool_still_listed():
    """A tool marked dangerous=True is still included in the MCP listing."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _Danger(BaseTool):
        spec = ToolSpec(name="bomb", description="risky op", dangerous=True)
        async def execute(self, **_): return ToolResult(tool_name="bomb", content="boom")

    server = MCPServer({"bomb": _Danger()})
    names = [e["name"] for e in server.listing()]
    assert "bomb" in names


def test_build_tool_listing_uses_spec_parameters_when_provided():
    """build_tool_listing uses the spec.parameters dict as inputSchema when present."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    custom_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }

    class _T(BaseTool):
        spec = ToolSpec(name="searcher", description="search", parameters=custom_schema)
        async def execute(self, **_): return ToolResult(tool_name="searcher", content="result")

    listing = build_tool_listing({"searcher": _T()})
    assert listing[0]["inputSchema"] == custom_schema


def test_mcp_server_tool_execution_returns_false_on_failure():
    """A ToolResult with success=False is falsy but still carries content."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _FailTool(BaseTool):
        spec = ToolSpec(name="fail", description="always fails gracefully", dangerous=False)
        async def execute(self, **_):
            return ToolResult(tool_name="fail", content="error details", success=False)

    server = MCPServer({"fail": _FailTool()})
    tool = server._tools["fail"]
    result = asyncio.run(tool.execute())
    assert result.success is False
    assert bool(result) is False
    assert result.content == "error details"


def test_build_tool_listing_name_matches_key():
    """The name in the listing entry equals the key used to register the tool, not a different spec name."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="inner_name", description="desc", dangerous=False)
        async def execute(self, **_): return ToolResult(tool_name="inner_name", content="ok")

    # Register under a different key — listing must use the key
    listing = build_tool_listing({"registered_key": _T()})
    assert listing[0]["name"] == "registered_key"


def test_mcp_server_stores_all_registered_tools():
    """MCPServer._tools dict contains exactly the tools passed at construction."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    def _make(name):
        class T(BaseTool):
            spec = ToolSpec(name=name, description=name, dangerous=False)
            async def execute(self, **_): return ToolResult(tool_name=name, content="ok")
        return T()

    tool_map = {"x": _make("x"), "y": _make("y"), "z": _make("z")}
    server = MCPServer(tool_map)
    assert set(server._tools.keys()) == {"x", "y", "z"}


def test_build_tool_listing_four_tools_correct_count():
    """build_tool_listing with four tools returns exactly four entries."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    def _make(n):
        class T(BaseTool):
            spec = ToolSpec(name=n, description=n, dangerous=False)
            async def execute(self, **_): return ToolResult(tool_name=n, content="ok")
        return T()

    tools = {n: _make(n) for n in ("p", "q", "r", "s")}
    listing = build_tool_listing(tools)
    assert len(listing) == 4


# --- Six more MCP server tests (appended) -------------------------------------------

def test_build_tool_listing_empty_returns_empty_list():
    """build_tool_listing with an empty dict returns an empty list."""
    assert build_tool_listing({}) == []


def test_build_tool_listing_default_input_schema_is_empty_object():
    """When spec.parameters is not provided, inputSchema defaults to empty object schema."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="noparams", description="no params tool")
        async def execute(self, **_): return ToolResult(tool_name="noparams", content="ok")

    listing = build_tool_listing({"noparams": _T()})
    assert listing[0]["inputSchema"] == {"type": "object", "properties": {}}


def test_build_tool_listing_sorted_alphabetically():
    """build_tool_listing returns entries sorted by key name (alphabetical)."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    def _make(n):
        class T(BaseTool):
            spec = ToolSpec(name=n, description=n)
            async def execute(self, **_): return ToolResult(tool_name=n, content="ok")
        return T()

    listing = build_tool_listing({"zebra": _make("zebra"), "apple": _make("apple"), "mango": _make("mango")})
    names = [e["name"] for e in listing]
    assert names == sorted(names)


def test_mcp_server_listing_returns_description_from_spec():
    """listing() returns the spec description for each registered tool."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="described", description="unique description text")
        async def execute(self, **_): return ToolResult(tool_name="described", content="ok")

    server = MCPServer({"described": _T()})
    entry = server.listing()[0]
    assert entry["description"] == "unique description text"


def test_mcp_server_custom_name_stored():
    """MCPServer stores the custom name supplied at construction."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="t", description="d")
        async def execute(self, **_): return ToolResult(tool_name="t", content="ok")

    server = MCPServer({"t": _T()}, name="my_custom_server")
    assert server._name == "my_custom_server"


def test_mcp_server_listing_single_tool_correct_structure():
    """A single-tool MCPServer listing has exactly one entry with name, description, inputSchema."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="single", description="single tool")
        async def execute(self, **_): return ToolResult(tool_name="single", content="ok")

    server = MCPServer({"single": _T()})
    listing = server.listing()
    assert len(listing) == 1
    assert set(listing[0].keys()) >= {"name", "description", "inputSchema"}
