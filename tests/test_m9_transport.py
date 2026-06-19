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


# --- Six additional tests (batch 3) -------------------------------------------

def test_build_tool_listing_sorted_order():
    """build_tool_listing() must return descriptors in sorted name order."""
    from hive.tools.mcp.server import build_tool_listing
    from hive.tools.builtins import ReadFile, WriteFile
    tools = {"write_file": WriteFile(), "read_file": ReadFile()}
    listing = build_tool_listing(tools)
    names = [d["name"] for d in listing]
    assert names == sorted(names)


def test_build_tool_listing_uses_spec_description():
    """build_tool_listing() must pick up the description from ToolSpec."""
    from hive.tools.mcp.server import build_tool_listing
    from hive.tools.builtins import ReadFile
    listing = build_tool_listing({"read_file": ReadFile()})
    assert listing[0]["description"] == ReadFile.spec.description


def test_mcp_tool_remote_name_defaults_to_spec_name():
    """When remote_name is omitted, MCPTool uses spec.name as the remote call target."""
    import asyncio
    from hive.tools.mcp.client import MCPTool, mcp_tool_to_spec

    calls = []

    async def _caller(name, args):
        calls.append(name)
        return "ok"

    spec = mcp_tool_to_spec({"name": "ping", "description": "p", "inputSchema": {}})
    tool = MCPTool(spec, _caller)  # no remote_name
    asyncio.run(tool.execute())
    assert calls == ["ping"]


def test_mcp_client_as_tools_empty_descriptors_returns_empty_list():
    """as_tools() with an empty descriptor list must return an empty list."""
    from hive.tools.mcp.client import MCPClient
    c = MCPClient(url="https://x/sse")
    assert c.as_tools([]) == []


def test_mcp_tool_to_spec_empty_description_string():
    """mcp_tool_to_spec with no description key still creates a valid spec with empty string."""
    from hive.tools.mcp.client import mcp_tool_to_spec
    spec = mcp_tool_to_spec({"name": "nodesc", "inputSchema": {}})
    assert spec.description == ""
    assert spec.name == "nodesc"


def test_mcp_server_custom_name_stored():
    """MCPServer must store a custom name when one is supplied."""
    from hive.tools.mcp.server import MCPServer
    from hive.tools.builtins import ReadFile
    server = MCPServer({"read_file": ReadFile()}, name="my-agent")
    assert server._name == "my-agent"


# --- Wave 3O additional tests ---------------------------------------------------

def test_mcp_server_listing_returns_list():
    """MCPServer.listing() returns a list of tool descriptors."""
    from hive.tools.mcp.server import MCPServer
    from hive.tools.builtins import ReadFile
    server = MCPServer({"read_file": ReadFile()})
    listing = server.listing()
    assert isinstance(listing, list) and len(listing) == 1


def test_mcp_server_listing_has_input_schema_key():
    """Each listing entry has an 'inputSchema' key."""
    from hive.tools.mcp.server import MCPServer
    from hive.tools.builtins import ReadFile
    server = MCPServer({"read_file": ReadFile()})
    for entry in server.listing():
        assert "inputSchema" in entry


def test_mcp_client_stores_url():
    """MCPClient stores the URL passed at construction."""
    from hive.tools.mcp.client import MCPClient
    c = MCPClient(url="https://example.com/sse")
    assert c._url == "https://example.com/sse"


def test_mcp_client_stores_command():
    """MCPClient stores the command passed at construction."""
    from hive.tools.mcp.client import MCPClient
    c = MCPClient(command="my-server", args=["--flag"])
    assert c._command == "my-server"
    assert c._args == ["--flag"]


def test_mcp_tool_to_spec_with_prefix():
    """mcp_tool_to_spec with prefix='myserver' includes the prefix in the spec name."""
    from hive.tools.mcp.client import mcp_tool_to_spec
    spec = mcp_tool_to_spec({"name": "search", "description": "d", "inputSchema": {}}, prefix="myserver")
    assert "myserver" in spec.name and "search" in spec.name


def test_mcp_tool_execute_returns_tool_result():
    """MCPTool.execute() returns a ToolResult."""
    import asyncio
    from hive.tools.mcp.client import MCPTool, mcp_tool_to_spec
    from hive.core.types import ToolResult

    async def _caller(name, args):
        return "data"

    spec = mcp_tool_to_spec({"name": "fetch", "description": "f", "inputSchema": {}})
    tool = MCPTool(spec, _caller, remote_name="fetch")
    result = asyncio.run(tool.execute())
    assert isinstance(result, ToolResult)


# --- Wave 3Q additional tests ---------------------------------------------------

def test_mcp_server_listing_description_field():
    """Each entry in listing() has a 'description' field."""
    from hive.tools.mcp.server import MCPServer
    from hive.tools.builtins import ReadFile
    server = MCPServer({"read_file": ReadFile()})
    for entry in server.listing():
        assert "description" in entry


def test_mcp_server_listing_name_matches_key():
    """The name field in each listing entry matches the tool key."""
    from hive.tools.mcp.server import MCPServer
    from hive.tools.builtins import ReadFile
    server = MCPServer({"read_file": ReadFile()})
    names = [e["name"] for e in server.listing()]
    assert "read_file" in names


def test_mcp_client_url_only_has_empty_command():
    """MCPClient constructed with url= only has empty command."""
    from hive.tools.mcp.client import MCPClient
    c = MCPClient(url="https://example.com/mcp")
    assert c._command == ""
    assert c._url == "https://example.com/mcp"


def test_mcp_client_command_only_has_empty_url():
    """MCPClient constructed with command= only has empty url."""
    from hive.tools.mcp.client import MCPClient
    c = MCPClient(command="npx", args=["-y", "my-server"])
    assert c._url == ""
    assert c._command == "npx"


def test_mcp_tool_to_spec_no_prefix_uses_name_directly():
    """mcp_tool_to_spec without prefix uses the name directly in the spec."""
    from hive.tools.mcp.client import mcp_tool_to_spec
    spec = mcp_tool_to_spec({"name": "lookup", "description": "do a lookup", "inputSchema": {}})
    assert "lookup" in spec.name


def test_mcp_server_default_name_is_hive():
    """MCPServer with no name kwarg defaults to name='hive'."""
    from hive.tools.mcp.server import MCPServer
    from hive.tools.builtins import ReadFile
    server = MCPServer({"read_file": ReadFile()})
    assert server._name == "hive"


# --- Wave 3W-B additional tests (transport) ------------------------------------

def test_wave3w_build_tool_listing_empty_dict_returns_empty_list():
    """build_tool_listing() with an empty tool dict returns an empty list."""
    from hive.tools.mcp.server import build_tool_listing
    assert build_tool_listing({}) == []


def test_wave3w_build_tool_listing_multiple_tools_sorted():
    """build_tool_listing() with multiple tools returns them sorted by name."""
    from hive.tools.mcp.server import build_tool_listing
    from hive.tools.builtins import ReadFile, WriteFile
    listing = build_tool_listing({"write_file": WriteFile(), "read_file": ReadFile()})
    names = [d["name"] for d in listing]
    assert names == sorted(names)
    assert len(names) == 2


def test_wave3w_mcp_tool_to_spec_inputschema_preserved():
    """mcp_tool_to_spec must copy the inputSchema as-is into the ToolSpec parameters."""
    from hive.tools.mcp.client import mcp_tool_to_spec
    schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    spec = mcp_tool_to_spec({"name": "search", "description": "search", "inputSchema": schema})
    assert spec.parameters == schema


def test_wave3w_mcp_tool_spec_assigned_at_construction():
    """MCPTool stores exactly the spec passed in at construction."""
    from hive.tools.mcp.client import MCPTool, mcp_tool_to_spec

    async def _caller(name, args): return "ok"

    descriptor = {"name": "mytool", "description": "desc", "inputSchema": {}}
    spec = mcp_tool_to_spec(descriptor)
    tool = MCPTool(spec, _caller, remote_name="mytool")
    assert tool.spec is spec


def test_wave3w_mcp_server_stores_tools_internally():
    """MCPServer must expose all registered tools through its internal _tools dict."""
    from hive.tools.mcp.server import MCPServer
    from hive.tools.builtins import ReadFile, WriteFile
    tools = {"read_file": ReadFile(), "write_file": WriteFile()}
    server = MCPServer(tools)
    assert set(server._tools.keys()) == {"read_file", "write_file"}


def test_wave3w_mcp_client_args_none_becomes_empty_list():
    """Passing args=None to MCPClient must result in _args being [] (not None)."""
    from hive.tools.mcp.client import MCPClient
    c = MCPClient("mycmd", None)
    assert c._args == []
    assert isinstance(c._args, list)


def test_wave3w_mcp_tool_execute_async_caller_receives_params():
    """MCPTool.execute() passes all keyword arguments to the caller as a dict."""
    import asyncio
    from hive.tools.mcp.client import MCPTool, mcp_tool_to_spec

    received: list[dict] = []

    async def _caller(name, args):
        received.append(args)
        return "done"

    spec = mcp_tool_to_spec({"name": "op", "description": "d", "inputSchema": {}})
    tool = MCPTool(spec, _caller, remote_name="op")
    asyncio.run(tool.execute(foo="bar", baz=42))
    assert received == [{"foo": "bar", "baz": 42}]


def test_wave3w_mcp_server_listing_count_matches_tool_count():
    """MCPServer.listing() must return the same number of entries as tools registered."""
    from hive.tools.mcp.server import MCPServer
    from hive.tools.builtins import ReadFile, WriteFile
    tools = {"read_file": ReadFile(), "write_file": WriteFile()}
    server = MCPServer(tools)
    assert len(server.listing()) == len(tools)


def test_wave3w_mcp_tool_to_spec_category_is_mcp():
    """mcp_tool_to_spec must always set category='mcp' on the resulting ToolSpec."""
    from hive.tools.mcp.client import mcp_tool_to_spec
    spec = mcp_tool_to_spec({"name": "anything", "description": "", "inputSchema": {}})
    assert spec.category == "mcp"


# --- Wave 4B additional tests (transport) --------------------------------------

def test_wave4b_mcp_tool_to_spec_dangerous_always_true():
    """mcp_tool_to_spec always marks the spec dangerous regardless of schema content."""
    from hive.tools.mcp.client import mcp_tool_to_spec
    schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    spec = mcp_tool_to_spec({"name": "risky", "description": "risky op", "inputSchema": schema})
    assert spec.dangerous is True


def test_wave4b_mcp_tool_remote_stores_given_remote_name():
    """MCPTool._remote is set to the remote_name kwarg when supplied."""
    from hive.tools.mcp.client import MCPTool, mcp_tool_to_spec

    async def _caller(name, args): return "ok"

    spec = mcp_tool_to_spec({"name": "local_name", "description": "d", "inputSchema": {}})
    tool = MCPTool(spec, _caller, remote_name="actual_remote")
    assert tool._remote == "actual_remote"


def test_wave4b_mcp_client_as_tools_uses_descriptor_name_as_remote():
    """as_tools() sets each MCPTool's _remote to the descriptor's 'name' field."""
    from hive.tools.mcp.client import MCPClient

    c = MCPClient(url="https://x/sse")
    descriptors = [{"name": "do_thing", "description": "desc", "inputSchema": {}}]
    tools = c.as_tools(descriptors, prefix="srv.")
    assert tools[0]._remote == "do_thing"


def test_wave4b_build_tool_listing_missing_parameters_uses_default_schema():
    """build_tool_listing uses default schema when spec.parameters is falsy."""
    from hive.tools.mcp.server import build_tool_listing
    from hive.tools.base import BaseTool, ToolSpec

    class _MinimalTool(BaseTool):
        spec = ToolSpec(name="minimal", description="m", parameters=None)
        async def execute(self, **kwargs): ...

    listing = build_tool_listing({"minimal": _MinimalTool()})
    assert listing[0]["inputSchema"] == {"type": "object", "properties": {}}


def test_wave4b_mcp_server_listing_name_field_equals_dict_key():
    """Each listing entry's 'name' field equals the key used to register the tool."""
    from hive.tools.mcp.server import MCPServer
    from hive.tools.builtins import WriteFile
    server = MCPServer({"write_file": WriteFile()})
    entry = server.listing()[0]
    assert entry["name"] == "write_file"


def test_wave4b_mcp_tool_to_spec_missing_inputschema_uses_default():
    """mcp_tool_to_spec with no 'inputSchema' key falls back to the default schema."""
    from hive.tools.mcp.client import mcp_tool_to_spec
    spec = mcp_tool_to_spec({"name": "noinput", "description": "d"})
    assert spec.parameters == {"type": "object", "properties": {}}


def test_wave4b_mcp_tool_execute_calls_remote_name_not_spec_name():
    """MCPTool.execute() dispatches with the remote_name, not the (prefixed) spec name."""
    import asyncio
    from hive.tools.mcp.client import MCPTool, mcp_tool_to_spec

    calls = []

    async def _caller(name, args):
        calls.append(name)
        return "result"

    spec = mcp_tool_to_spec({"name": "prefixed.tool", "description": "d", "inputSchema": {}})
    tool = MCPTool(spec, _caller, remote_name="tool")
    asyncio.run(tool.execute())
    assert calls == ["tool"]
    assert spec.name == "prefixed.tool"


def test_wave4b_mcp_server_empty_tools_listing_is_empty_list():
    """MCPServer with an empty tools dict returns an empty list from listing()."""
    from hive.tools.mcp.server import MCPServer
    server = MCPServer({})
    assert server.listing() == []


def test_wave4b_mcp_client_as_tools_single_descriptor_length_one():
    """as_tools() with a single descriptor returns exactly one MCPTool."""
    from hive.tools.mcp.client import MCPClient

    c = MCPClient(url="https://x/sse")
    tools = c.as_tools([{"name": "one", "description": "only", "inputSchema": {}}])
    assert len(tools) == 1


# --- Wave 4H additional tests (transport) --------------------------------------

def test_wave4h_mcp_tool_execute_with_int_kwarg():
    """MCPTool.execute() passes integer kwarg values to the caller unchanged."""
    import asyncio
    from hive.tools.mcp.client import MCPTool, mcp_tool_to_spec

    received: list[dict] = []

    async def _caller(name, args):
        received.append(args)
        return "ok"

    spec = mcp_tool_to_spec({"name": "add", "description": "add", "inputSchema": {}})
    tool = MCPTool(spec, _caller, remote_name="add")
    asyncio.run(tool.execute(x=1, y=2))
    assert received == [{"x": 1, "y": 2}]


def test_wave4h_mcp_tool_execute_with_list_kwarg():
    """MCPTool.execute() passes list kwarg values to the caller unchanged."""
    import asyncio
    from hive.tools.mcp.client import MCPTool, mcp_tool_to_spec

    received: list[dict] = []

    async def _caller(name, args):
        received.append(args)
        return "ok"

    spec = mcp_tool_to_spec({"name": "batch", "description": "batch", "inputSchema": {}})
    tool = MCPTool(spec, _caller, remote_name="batch")
    asyncio.run(tool.execute(items=["a", "b", "c"]))
    assert received[0]["items"] == ["a", "b", "c"]


def test_wave4h_mcp_tool_to_spec_nested_input_schema():
    """mcp_tool_to_spec preserves a nested inputSchema with nested object properties."""
    from hive.tools.mcp.client import mcp_tool_to_spec

    schema = {
        "type": "object",
        "properties": {
            "opts": {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            }
        },
    }
    spec = mcp_tool_to_spec({"name": "complex", "description": "c", "inputSchema": schema})
    assert spec.parameters == schema
    assert spec.parameters["properties"]["opts"]["type"] == "object"


def test_wave4h_mcp_tool_to_spec_name_with_dots_preserved():
    """mcp_tool_to_spec preserves dots in the tool name."""
    from hive.tools.mcp.client import mcp_tool_to_spec

    spec = mcp_tool_to_spec({"name": "memory.store", "description": "d", "inputSchema": {}})
    assert spec.name == "memory.store"


def test_wave4h_mcp_server_three_tools_listing_sorted():
    """MCPServer with three tools returns listing in sorted name order."""
    from hive.tools.mcp.server import MCPServer
    from hive.tools.builtins import ReadFile, WriteFile, Shell

    tools = {"write_file": WriteFile(), "shell": Shell(), "read_file": ReadFile()}
    server = MCPServer(tools)
    names = [e["name"] for e in server.listing()]
    assert names == sorted(names)
    assert len(names) == 3


def test_wave4h_mcp_server_listing_each_entry_has_name_key():
    """Every entry returned by MCPServer.listing() has a 'name' key."""
    from hive.tools.mcp.server import MCPServer
    from hive.tools.builtins import ReadFile, WriteFile

    server = MCPServer({"read_file": ReadFile(), "write_file": WriteFile()})
    for entry in server.listing():
        assert "name" in entry


def test_wave4h_mcp_tool_caller_stored_at_construction():
    """MCPTool._caller is exactly the callable passed in at construction."""
    from hive.tools.mcp.client import MCPTool, mcp_tool_to_spec

    async def _caller(name, args): return "ok"

    spec = mcp_tool_to_spec({"name": "t", "description": "d", "inputSchema": {}})
    tool = MCPTool(spec, _caller, remote_name="t")
    assert tool._caller is _caller


def test_wave4h_mcp_client_default_construction_all_empty():
    """MCPClient() with no arguments has empty command, args list, and url."""
    from hive.tools.mcp.client import MCPClient

    c = MCPClient()
    assert c._command == ""
    assert c._args == []
    assert c._url == ""
    assert c._session is None


# --- Wave 4O additional tests (transport) --------------------------------------

def test_wave4o_mcp_client_all_empty_strings_builds():
    """MCPClient with all empty-string arguments still constructs without error."""
    from hive.tools.mcp.client import MCPClient
    c = MCPClient("", [], url="")
    assert c._command == ""
    assert c._args == []
    assert c._url == ""


def test_wave4o_mcp_tool_execute_with_extra_kwargs():
    """MCPTool.execute() forwards extra unexpected kwargs to the caller as-is."""
    from hive.tools.mcp.client import MCPTool, mcp_tool_to_spec

    received: list[dict] = []

    async def _caller(name, args):
        received.append(args)
        return "done"

    spec = mcp_tool_to_spec({"name": "op", "description": "d", "inputSchema": {}})
    tool = MCPTool(spec, _caller, remote_name="op")
    asyncio.run(tool.execute(alpha="a", beta=99, gamma=True))
    assert received[0] == {"alpha": "a", "beta": 99, "gamma": True}


def test_wave4o_build_tool_listing_tool_with_no_description():
    """build_tool_listing handles a tool whose spec.description is an empty string."""
    from hive.tools.mcp.server import build_tool_listing
    from hive.tools.base import BaseTool, ToolSpec

    class _NoDescTool(BaseTool):
        spec = ToolSpec(name="nodesc_tool", description="", parameters={})
        async def execute(self, **kwargs): ...

    listing = build_tool_listing({"nodesc_tool": _NoDescTool()})
    assert listing[0]["name"] == "nodesc_tool"
    assert listing[0]["description"] == ""


def test_wave4o_mcp_tool_to_spec_returns_tool_descriptor():
    """mcp_tool_to_spec returns a ToolSpec (ToolDescriptor) instance."""
    from hive.tools.mcp.client import mcp_tool_to_spec
    from hive.tools.base import ToolSpec
    spec = mcp_tool_to_spec({"name": "t", "description": "d", "inputSchema": {}})
    assert isinstance(spec, ToolSpec)


def test_wave4o_tool_descriptor_from_mcp_tool_has_expected_category():
    """A ToolSpec created by mcp_tool_to_spec has category='mcp'."""
    from hive.tools.mcp.client import mcp_tool_to_spec
    spec = mcp_tool_to_spec({"name": "x", "description": "y", "inputSchema": {}})
    assert spec.category == "mcp"


def test_wave4o_mcp_server_list_tools_returns_list():
    """MCPServer.listing() always returns a list type."""
    from hive.tools.mcp.server import MCPServer
    from hive.tools.builtins import WriteFile
    server = MCPServer({"write_file": WriteFile()})
    result = server.listing()
    assert isinstance(result, list)


def test_wave4o_mcp_tool_with_empty_name():
    """MCPTool constructed with a spec whose name is empty string stores it correctly."""
    from hive.tools.mcp.client import MCPTool, mcp_tool_to_spec

    async def _caller(name, args): return "ok"

    spec = mcp_tool_to_spec({"name": "", "description": "d", "inputSchema": {}})
    tool = MCPTool(spec, _caller, remote_name="")
    assert tool.spec.name == ""
    assert tool._remote == ""


def test_wave4o_listing_with_two_tools_has_length_two():
    """MCPServer with exactly two tools returns a listing of length 2."""
    from hive.tools.mcp.server import MCPServer
    from hive.tools.builtins import ReadFile, WriteFile
    server = MCPServer({"read_file": ReadFile(), "write_file": WriteFile()})
    assert len(server.listing()) == 2
