"""
server.py — expose Hive's tools over MCP (TAKE/ADAPT, SYNTHESIS Part B).

Lets other agents consume Hive's registry as an MCP server (mirrors Mnemosyne's
mcp_server.py). The `mcp` SDK is imported lazily so this module imports without the
dependency; `build_tool_listing` (the SDK-free part) is unit-tested and reused to
advertise tools regardless of transport.
"""
from __future__ import annotations

from typing import Any, Mapping

from hive.tools.base import BaseTool
from hive.tools.executor import DispatchStatus, ToolExecutor


def build_tool_listing(tools: Mapping[str, BaseTool]) -> list[dict[str, Any]]:
    """MCP tool descriptors for the given tools (deterministic order)."""
    return [
        {"name": name, "description": tool.spec.description,
         "inputSchema": tool.spec.parameters or {"type": "object", "properties": {}}}
        for name, tool in sorted(tools.items())
    ]


class MCPServer:
    def __init__(self, tools: Mapping[str, BaseTool], *, name: str = "hive",
                 executor: ToolExecutor | None = None) -> None:
        self._tools = dict(tools)
        self._name = name
        # The live runtime injects its executor. A standalone server still uses
        # the same execution boundary rather than invoking a tool directly.
        self._executor = executor or ToolExecutor(self._tools)

    def listing(self) -> list[dict[str, Any]]:
        return build_tool_listing(self._tools)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Dispatch an MCP request through the sole capability boundary."""
        dispatch = await self._executor.execute(name, arguments or {}, reason="MCP request")
        if dispatch.status is DispatchStatus.OK and dispatch.result is not None:
            return dispatch.result.content
        if dispatch.status is DispatchStatus.PENDING:
            return f"approval required (approval_id={dispatch.approval_id})"
        return f"error: {dispatch.error or 'tool execution failed'}"

    async def serve_stdio(self) -> None:  # pragma: no cover - needs the mcp SDK
        try:
            import mcp.types as mcp_types
            from mcp.server import Server
            from mcp.server.stdio import stdio_server
        except ImportError as exc:
            raise RuntimeError(
                "the 'mcp' package is required to serve; pip install mcp") from exc

        server = Server(self._name)

        @server.list_tools()
        async def _list() -> list[Any]:
            return [mcp_types.Tool(**d) for d in self.listing()]

        @server.call_tool()
        async def _call(name: str, arguments: dict[str, Any]) -> list[Any]:
            try:
                content = await self.call_tool(name, arguments)
                return [mcp_types.TextContent(type="text", text=content)]
            except Exception as exc:  # noqa: BLE001
                return [mcp_types.TextContent(type="text", text=f"error: {exc}")]

        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
