"""
client.py — MCP client + tool adapter (TAKE/ADAPT, SYNTHESIS Part B).

Discovery-first means most capability arrives as MCP servers, so Hive consumes
them through the official `mcp` Python SDK. The SDK is imported lazily inside the
connection methods so this module (and the adapter below) import without the
dependency installed — the offline-testable seam is the ToolSpec<->MCP conversion
and the MCPTool wrapper, which turns a remote MCP tool into a registry BaseTool.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from hive.core.types import ToolResult
from hive.tools.base import BaseTool, ToolSpec

# (tool_name, arguments) -> raw text result. Bound to a live MCP session, or faked in tests.
MCPCaller = Callable[[str, dict[str, Any]], Awaitable[str]]


def mcp_tool_to_spec(tool: dict[str, Any], *, prefix: str = "") -> ToolSpec:
    """Convert an MCP tool descriptor to a HiveOS ToolSpec.

    MCP tools are untrusted external capability, so every one is marked dangerous —
    the executor routes them through the approval gate by default.
    """
    name = tool.get("name", "")
    return ToolSpec(
        name=f"{prefix}{name}" if prefix else name,
        description=tool.get("description", ""),
        parameters=tool.get("inputSchema", {"type": "object", "properties": {}}),
        dangerous=True,
        category="mcp",
    )


class MCPTool(BaseTool):
    """Adapts a remote MCP tool to the BaseTool contract via an injected caller."""

    def __init__(self, spec: ToolSpec, caller: MCPCaller, *, remote_name: str | None = None) -> None:
        self._spec = spec
        self._caller = caller
        self._remote = remote_name or spec.name

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, **params: Any) -> ToolResult:
        content = await self._caller(self._remote, params)
        return ToolResult(tool_name=self._spec.name, content=content)


class MCPClient:
    """Thin wrapper over the official MCP SDK stdio client (lazy import)."""

    def __init__(self, command: str, args: list[str] | None = None) -> None:
        self._command = command
        self._args = args or []
        self._session: Any = None

    async def connect(self) -> None:  # pragma: no cover - needs the mcp SDK + a server
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise RuntimeError(
                "the 'mcp' package is required for MCPClient; pip install mcp") from exc
        self._params = StdioServerParameters(command=self._command, args=self._args)
        self._ctx = stdio_client(self._params)
        read, write = await self._ctx.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()

    async def list_tools(self) -> list[dict[str, Any]]:  # pragma: no cover - live
        resp = await self._session.list_tools()
        return [{"name": t.name, "description": t.description,
                 "inputSchema": t.inputSchema} for t in resp.tools]

    async def call(self, name: str, arguments: dict[str, Any]) -> str:  # pragma: no cover - live
        result = await self._session.call_tool(name, arguments)
        return "".join(getattr(block, "text", "") for block in result.content)

    def as_tools(self, descriptors: list[dict[str, Any]], *, prefix: str = "") -> list[MCPTool]:
        """Wrap listed MCP descriptors as registry-ready BaseTools."""
        return [MCPTool(mcp_tool_to_spec(d, prefix=prefix), self.call,
                        remote_name=d.get("name", "")) for d in descriptors]
