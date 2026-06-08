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


def build_tool_listing(tools: Mapping[str, BaseTool]) -> list[dict[str, Any]]:
    """MCP tool descriptors for the given tools (deterministic order)."""
    return [
        {"name": name, "description": tool.spec.description,
         "inputSchema": tool.spec.parameters or {"type": "object", "properties": {}}}
        for name, tool in sorted(tools.items())
    ]


class MCPServer:
    def __init__(self, tools: Mapping[str, BaseTool], *, name: str = "hive") -> None:
        self._tools = dict(tools)
        self._name = name

    def listing(self) -> list[dict[str, Any]]:
        return build_tool_listing(self._tools)

    async def serve_stdio(self) -> None:  # pragma: no cover - needs the mcp SDK
        try:
            from mcp.server import Server
            from mcp.server.stdio import stdio_server
            import mcp.types as mcp_types
        except ImportError as exc:
            raise RuntimeError(
                "the 'mcp' package is required to serve; pip install mcp") from exc

        server = Server(self._name)

        @server.list_tools()
        async def _list() -> list[Any]:
            return [mcp_types.Tool(**d) for d in self.listing()]

        @server.call_tool()
        async def _call(name: str, arguments: dict[str, Any]) -> list[Any]:
            tool = self._tools[name]
            result = await tool.execute(**arguments)
            return [mcp_types.TextContent(type="text", text=result.content)]

        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
