"""
client.py — MCP client + tool adapter (TAKE/ADAPT, SYNTHESIS Part B).

Discovery-first means most capability arrives as MCP servers, so Hive consumes
them through the official `mcp` Python SDK. The SDK is imported lazily inside the
connection methods so this module (and the adapter below) import without the
dependency installed — the offline-testable seam is the ToolSpec<->MCP conversion
and the MCPTool wrapper, which turns a remote MCP tool into a registry BaseTool.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping

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
        self.spec = spec
        self._caller = caller
        self._remote = remote_name or spec.name

    async def execute(self, **params: Any) -> ToolResult:
        content = await self._caller(self._remote, params)
        return ToolResult(tool_name=self.spec.name, content=content)


class MCPClient:
    """Thin wrapper over the official MCP SDK (lazy import). stdio by default; pass
    `url=` for an SSE (HTTP) server such as a remote Mnemosyne (A6)."""

    def __init__(self, command: str = "", args: list[str] | None = None,
                 *, url: str = "", environment: Mapping[str, str] | None = None) -> None:
        self._command = command
        self._args = args or []
        self._url = url
        self._environment = dict(environment or {})
        self._session: Any = None

    async def connect(self) -> None:  # pragma: no cover - needs the mcp SDK + a server
        try:
            from mcp import ClientSession
        except ImportError as exc:
            raise RuntimeError(
                "the 'mcp' package is required for MCPClient; pip install mcp") from exc
        if self._url:  # SSE/HTTP transport (A6: remote Mnemosyne etc.)
            import httpx2
            from mcp.client.sse import sse_client

            def no_redirect_client(*, headers: dict[str, str] | None = None,
                                   timeout: Any = None, auth: Any = None) -> Any:
                """SDK factory with redirect following disabled for pinned origins."""
                return httpx2.AsyncClient(headers=headers, timeout=timeout, auth=auth,
                                          follow_redirects=False)

            self._ctx = sse_client(self._url, httpx_client_factory=no_redirect_client)
        else:           # stdio transport (local subprocess server)
            from mcp import StdioServerParameters
            from mcp.client import stdio as mcp_stdio

            # The SDK currently merges `get_default_environment()` into the
            # supplied mapping. Temporarily replace that function while its
            # context enters, so the child receives this exact allowlist.
            # A concurrent SDK spawn can only receive the stricter empty base.
            original_environment = mcp_stdio.get_default_environment
            mcp_stdio.get_default_environment = lambda: {}
            try:
                self._ctx = mcp_stdio.stdio_client(
                    StdioServerParameters(command=self._command, args=self._args,
                                          env=self._environment))
                read, write = await self._ctx.__aenter__()
            finally:
                mcp_stdio.get_default_environment = original_environment
        if self._url:
            read, write = await self._ctx.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()

    async def aclose(self) -> None:
        """Close a fully or partly opened SDK session and its transport."""
        error: BaseException | None = None
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except BaseException as exc:  # keep closing the transport on failure
                error = exc
            finally:
                self._session = None
        context = getattr(self, "_ctx", None)
        if context is not None:
            try:
                await context.__aexit__(None, None, None)
            except BaseException as exc:  # preserve the first failure after cleanup
                if error is None:
                    error = exc
            finally:
                self._ctx = None
        if error is not None:
            raise error

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
