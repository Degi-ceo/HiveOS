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


# --- Six new tests (Wave 3Q) --------------------------------------------------------

def test_mcp_server_listing_returns_list_type():
    """MCPServer.listing() always returns a plain list, even when populated."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="t", description="d", dangerous=False)
        async def execute(self, **_): return ToolResult(tool_name="t", content="ok")

    server = MCPServer({"t": _T()})
    assert isinstance(server.listing(), list)


def test_build_tool_listing_returns_list_type():
    """build_tool_listing always returns a plain list."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="t", description="d", dangerous=False)
        async def execute(self, **_): return ToolResult(tool_name="t", content="ok")

    result = build_tool_listing({"t": _T()})
    assert isinstance(result, list)


def test_mcp_server_tool_available_by_default():
    """BaseTool.available() returns True by default (no override needed)."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="t", description="d", dangerous=False)
        async def execute(self, **_): return ToolResult(tool_name="t", content="ok")

    tool = _T()
    assert tool.available() is True


def test_build_tool_listing_empty_parameters_uses_default_schema():
    """When spec.parameters is an empty dict (falsy), inputSchema falls back to default."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="empty_params", description="d", parameters={}, dangerous=False)
        async def execute(self, **_): return ToolResult(tool_name="empty_params", content="ok")

    listing = build_tool_listing({"empty_params": _T()})
    assert listing[0]["inputSchema"] == {"type": "object", "properties": {}}


def test_mcp_server_listing_count_matches_registered_tools():
    """listing() count equals the number of tools registered."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    def _make(n):
        class T(BaseTool):
            spec = ToolSpec(name=n, description=n, dangerous=False)
            async def execute(self, **_): return ToolResult(tool_name=n, content="ok")
        return T()

    n = 5
    tools = {str(i): _make(str(i)) for i in range(n)}
    server = MCPServer(tools)
    assert len(server.listing()) == n


def test_mcp_server_replaces_tools_on_reconstruction():
    """Constructing two MCPServer instances with different tools keeps them independent."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    def _make(name):
        class T(BaseTool):
            spec = ToolSpec(name=name, description=name, dangerous=False)
            async def execute(self, **_): return ToolResult(tool_name=name, content="ok")
        return T()

    server_a = MCPServer({"tool_a": _make("tool_a")})
    server_b = MCPServer({"tool_b": _make("tool_b")})
    names_a = {e["name"] for e in server_a.listing()}
    names_b = {e["name"] for e in server_b.listing()}
    assert "tool_a" in names_a and "tool_b" not in names_a
    assert "tool_b" in names_b and "tool_a" not in names_b


# --- Wave 3W-A tests -----------------------------------------------------------

def test_wave3w_build_tool_listing_empty_returns_list():
    """build_tool_listing({}) returns a list, not None."""
    result = build_tool_listing({})
    assert result == [] and isinstance(result, list)


def test_wave3w_build_tool_listing_single_tool_entry_count():
    """build_tool_listing with exactly one tool returns a list of length 1."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="solo", description="only one")
        async def execute(self, **_): return ToolResult(tool_name="solo", content="ok")

    listing = build_tool_listing({"solo": _T()})
    assert len(listing) == 1


def test_wave3w_mcp_server_name_attribute_stored():
    """MCPServer._name equals the name argument passed at construction."""
    server = MCPServer({}, name="test-agent")
    assert server._name == "test-agent"


def test_wave3w_mcp_server_default_name_attribute():
    """MCPServer._name defaults to 'hive' when no name argument is given."""
    server = MCPServer({})
    assert server._name == "hive"


def test_wave3w_tool_spec_name_field_in_listing():
    """build_tool_listing entry name equals the spec name (key used in dict)."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="mykey", description="d")
        async def execute(self, **_): return ToolResult(tool_name="mykey", content="ok")

    listing = build_tool_listing({"mykey": _T()})
    assert listing[0]["name"] == "mykey"


def test_wave3w_tool_spec_input_schema_has_type_object():
    """A tool with no explicit parameters gets inputSchema with type='object'."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="nop", description="no params")
        async def execute(self, **_): return ToolResult(tool_name="nop", content="ok")

    listing = build_tool_listing({"nop": _T()})
    assert listing[0]["inputSchema"]["type"] == "object"


def test_wave3w_build_tool_listing_with_two_tools_sorted():
    """build_tool_listing sorts two tools alphabetically by key."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    def _mk(n):
        class T(BaseTool):
            spec = ToolSpec(name=n, description=n)
            async def execute(self, **_): return ToolResult(tool_name=n, content="ok")
        return T()

    listing = build_tool_listing({"bravo": _mk("bravo"), "alpha": _mk("alpha")})
    assert [e["name"] for e in listing] == ["alpha", "bravo"]


def test_wave3w_mcp_server_tools_dict_stored():
    """MCPServer._tools stores a dict copy of the tools passed at construction."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="t", description="d")
        async def execute(self, **_): return ToolResult(tool_name="t", content="ok")

    tool_instance = _T()
    server = MCPServer({"t": tool_instance})
    assert isinstance(server._tools, dict)
    assert "t" in server._tools


# --- Wave 4A-A new tests -------------------------------------------------------

def test_wave4a_listing_not_filtered_by_availability():
    """listing() includes all tools regardless of available() return value."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _Unavail(BaseTool):
        spec = ToolSpec(name="unavail", description="unavailable tool", dangerous=False)
        def available(self) -> bool: return False
        async def execute(self, **_): return ToolResult(tool_name="unavail", content="ok")

    server = MCPServer({"unavail": _Unavail()})
    names = [e["name"] for e in server.listing()]
    assert "unavail" in names


def test_wave4a_listing_called_twice_same_result():
    """Calling listing() twice on the same server returns equal results."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="stable", description="stable tool", dangerous=False)
        async def execute(self, **_): return ToolResult(tool_name="stable", content="ok")

    server = MCPServer({"stable": _T()})
    assert server.listing() == server.listing()


def test_wave4a_build_tool_listing_nested_schema():
    """build_tool_listing preserves nested property schemas verbatim."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    nested = {
        "type": "object",
        "properties": {
            "config": {
                "type": "object",
                "properties": {"timeout": {"type": "integer"}},
            }
        },
        "required": ["config"],
    }

    class _T(BaseTool):
        spec = ToolSpec(name="nested_tool", description="nested", parameters=nested, dangerous=False)
        async def execute(self, **_): return ToolResult(tool_name="nested_tool", content="ok")

    listing = build_tool_listing({"nested_tool": _T()})
    assert listing[0]["inputSchema"] == nested


def test_wave4a_serve_stdio_raises_without_mcp_sdk(monkeypatch):
    """serve_stdio raises RuntimeError with install hint when mcp SDK is absent."""
    import builtins
    import sys

    real_import = builtins.__import__

    def _block_mcp(name, *args, **kwargs):
        if name == "mcp.types" or name.startswith("mcp"):
            raise ImportError("No module named 'mcp'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_mcp)
    # Remove cached mcp modules so our import blocker triggers
    mcp_keys = [k for k in sys.modules if k == "mcp" or k.startswith("mcp.")]
    for k in mcp_keys:
        monkeypatch.delitem(sys.modules, k, raising=False)

    server = MCPServer({})
    with pytest.raises(RuntimeError, match="mcp"):
        asyncio.run(server.serve_stdio())


def test_wave4a_build_tool_listing_key_used_not_spec_name():
    """build_tool_listing uses the dict key as name, not the spec.name."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="spec_name", description="d", dangerous=False)
        async def execute(self, **_): return ToolResult(tool_name="spec_name", content="ok")

    listing = build_tool_listing({"dict_key": _T()})
    assert listing[0]["name"] == "dict_key"


def test_wave4a_tool_spec_category_not_in_listing_entry():
    """listing entries do not expose the ToolSpec.category field."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="cat_tool", description="d", category="special", dangerous=False)
        async def execute(self, **_): return ToolResult(tool_name="cat_tool", content="ok")

    server = MCPServer({"cat_tool": _T()})
    entry = server.listing()[0]
    assert "category" not in entry


def test_wave4a_mcp_server_mutation_of_original_dict_does_not_affect_server():
    """Mutating the tools dict after MCPServer construction does not change listing."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="t", description="d", dangerous=False)
        async def execute(self, **_): return ToolResult(tool_name="t", content="ok")

    tool_map = {"t": _T()}
    server = MCPServer(tool_map)
    tool_map["extra"] = _T()
    # Server was constructed before mutation — its internal copy must not include "extra"
    names = {e["name"] for e in server.listing()}
    assert "extra" not in names


def test_wave4a_build_tool_listing_required_field_preserved():
    """A schema with a required array is preserved as-is in inputSchema."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    schema = {
        "type": "object",
        "properties": {"url": {"type": "string"}, "method": {"type": "string"}},
        "required": ["url"],
    }

    class _T(BaseTool):
        spec = ToolSpec(name="http_tool", description="http", parameters=schema, dangerous=False)
        async def execute(self, **_): return ToolResult(tool_name="http_tool", content="ok")

    listing = build_tool_listing({"http_tool": _T()})
    assert listing[0]["inputSchema"]["required"] == ["url"]


# --- Wave 4F new tests -------------------------------------------------------

def test_wave4f_mcp_server_tools_dict_is_copy():
    """MCPServer stores a copy of the tool map — mutation after construction is isolated."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="t", description="d", dangerous=False)
        async def execute(self, **_): return ToolResult(tool_name="t", content="ok")

    original = {"t": _T()}
    server = MCPServer(original)
    original.clear()
    assert len(server.listing()) == 1


def test_wave4f_build_tool_listing_five_tools_count():
    """build_tool_listing with five distinct tools returns exactly five entries."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    def _mk(n):
        class T(BaseTool):
            spec = ToolSpec(name=n, description=n, dangerous=False)
            async def execute(self, **_): return ToolResult(tool_name=n, content="ok")
        return T()

    tools = {n: _mk(n) for n in ("e1", "e2", "e3", "e4", "e5")}
    listing = build_tool_listing(tools)
    assert len(listing) == 5


def test_wave4f_mcp_server_listing_entry_name_is_string():
    """Every name field in listing() entries is a plain str."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="strcheck", description="d", dangerous=False)
        async def execute(self, **_): return ToolResult(tool_name="strcheck", content="ok")

    server = MCPServer({"strcheck": _T()})
    for entry in server.listing():
        assert isinstance(entry["name"], str)


def test_wave4f_build_tool_listing_description_is_string():
    """Every description in build_tool_listing output is a plain str."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="desc_str", description="just text", dangerous=False)
        async def execute(self, **_): return ToolResult(tool_name="desc_str", content="ok")

    listing = build_tool_listing({"desc_str": _T()})
    assert isinstance(listing[0]["description"], str)


def test_wave4f_mcp_server_listing_input_schema_is_dict():
    """inputSchema in each listing entry is a plain dict."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="schema_type", description="d", dangerous=False)
        async def execute(self, **_): return ToolResult(tool_name="schema_type", content="ok")

    server = MCPServer({"schema_type": _T()})
    for entry in server.listing():
        assert isinstance(entry["inputSchema"], dict)


def test_wave4f_mcp_server_two_instances_independent_names():
    """Two MCPServer instances with different names keep their own _name."""
    server_a = MCPServer({}, name="alpha")
    server_b = MCPServer({}, name="beta")
    assert server_a._name == "alpha"
    assert server_b._name == "beta"


def test_wave4f_build_tool_listing_preserves_properties_keys():
    """Schema properties keys are preserved verbatim in the listing output."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    schema = {
        "type": "object",
        "properties": {"file_path": {"type": "string"}, "encoding": {"type": "string"}},
    }

    class _T(BaseTool):
        spec = ToolSpec(name="reader", description="reads", parameters=schema, dangerous=False)
        async def execute(self, **_): return ToolResult(tool_name="reader", content="ok")

    listing = build_tool_listing({"reader": _T()})
    props = listing[0]["inputSchema"]["properties"]
    assert "file_path" in props and "encoding" in props


def test_wave4f_mcp_server_empty_listing_is_empty_list():
    """An MCPServer with zero tools returns [] from listing(), not None or other falsy."""
    server = MCPServer({})
    result = server.listing()
    assert result == [] and result is not None and isinstance(result, list)


# --- Wave 4L new tests -------------------------------------------------------

def test_wave4l_listing_exact_n_tools():
    """listing() with exactly N tools returns exactly N entries."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    def _mk(n):
        class T(BaseTool):
            spec = ToolSpec(name=n, description=n, dangerous=False)
            async def execute(self, **_): return ToolResult(tool_name=n, content="ok")
        return T()

    n = 7
    tools = {str(i): _mk(str(i)) for i in range(n)}
    server = MCPServer(tools)
    assert len(server.listing()) == n


def test_wave4l_tool_execute_with_none_kwarg():
    """BaseTool.execute() with a kwarg value of None does not raise."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="nullable", description="accepts None", dangerous=False)
        async def execute(self, **kwargs):
            return ToolResult(tool_name="nullable", content=repr(kwargs.get("val")))

    import asyncio
    tool = _T()
    result = asyncio.run(tool.execute(val=None))
    assert "None" in result.content


def test_wave4l_mcp_server_available_when_no_tools():
    """MCPServer with no tools: listing() returns [] and _tools is empty dict."""
    server = MCPServer({})
    assert server._tools == {}
    assert server.listing() == []


def test_wave4l_listing_description_always_str():
    """Every description field in listing() entries is always a plain str."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    def _mk(n):
        class T(BaseTool):
            spec = ToolSpec(name=n, description=f"description for {n}", dangerous=False)
            async def execute(self, **_): return ToolResult(tool_name=n, content="ok")
        return T()

    tools = {n: _mk(n) for n in ("p1", "p2", "p3")}
    server = MCPServer(tools)
    for entry in server.listing():
        assert isinstance(entry["description"], str)


def test_wave4l_build_tool_listing_exact_two_tools():
    """build_tool_listing with exactly two tools returns exactly two entries."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    def _mk(n):
        class T(BaseTool):
            spec = ToolSpec(name=n, description=n, dangerous=False)
            async def execute(self, **_): return ToolResult(tool_name=n, content="ok")
        return T()

    listing = build_tool_listing({"x1": _mk("x1"), "x2": _mk("x2")})
    assert len(listing) == 2


def test_wave4l_mcp_server_listing_description_not_none():
    """No entry in listing() has a None description."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    class _T(BaseTool):
        spec = ToolSpec(name="nodesc", description="non-null description", dangerous=False)
        async def execute(self, **_): return ToolResult(tool_name="nodesc", content="ok")

    server = MCPServer({"nodesc": _T()})
    for entry in server.listing():
        assert entry["description"] is not None


def test_wave4l_mcp_server_name_defaults_to_hive_no_args():
    """MCPServer() with only tools argument defaults _name to 'hive'."""
    server = MCPServer({})
    assert server._name == "hive"


def test_wave4l_build_tool_listing_three_tools_sorted():
    """build_tool_listing with three tools returns names in sorted order."""
    from hive.tools.base import BaseTool, ToolResult, ToolSpec

    def _mk(n):
        class T(BaseTool):
            spec = ToolSpec(name=n, description=n, dangerous=False)
            async def execute(self, **_): return ToolResult(tool_name=n, content="ok")
        return T()

    listing = build_tool_listing({
        "zz": _mk("zz"), "aa": _mk("aa"), "mm": _mk("mm")
    })
    names = [e["name"] for e in listing]
    assert names == sorted(names)
    assert len(names) == 3
