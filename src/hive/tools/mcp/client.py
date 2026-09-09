"""
client.py — MCP client + tool adapter (TAKE/ADAPT, SYNTHESIS Part B).

Discovery-first means most capability arrives as MCP servers, so Hive consumes
them through the official `mcp` Python SDK. The SDK is imported lazily inside the
connection methods so this module (and the adapter below) import without the
dependency installed — the offline-testable seam is the ToolSpec<->MCP conversion
and the MCPTool wrapper, which turns a remote MCP tool into a registry BaseTool.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Awaitable, Callable

from hive.core.types import ContentEnvelope, ToolResult
from hive.tools.base import BaseTool, ToolSpec

# (tool_name, arguments) -> raw text result. Bound to a live MCP session, or faked in tests.
MCPCaller = Callable[[str, dict[str, Any]], Awaitable[str]]
_MCP_DESCRIPTION_MAX = 1_000
_MCP_SCHEMA_ANNOTATIONS = frozenset({"$comment", "description", "title"})
_MCP_SCHEMA_OMITTED_ANNOTATIONS = frozenset({"default", "examples"})
_MCP_SCHEMA_LITERAL_KEYWORDS = frozenset({"const", "enum"})


def sanitize_mcp_description(name: str, description: object) -> str:
    """Bound and envelope server-controlled description text for model schemas."""
    raw = str(description or "")
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", raw)
    cleaned = cleaned[:_MCP_DESCRIPTION_MAX]
    return ContentEnvelope.untrusted(
        cleaned, source=f"mcp-description:{name}",
    ).render_for_prompt()


def sanitize_mcp_schema(name: str, schema: object) -> dict[str, Any]:
    """Envelope free-text JSON Schema annotations without changing its contract.

    Structural strings (property names, types, formats, enums, constants, and required
    fields) must remain exact so remote tool calls keep their wire compatibility.
    Textual annotations are model-facing prose and therefore receive the same
    untrusted-data boundary as the top-level description.  ``default`` and ``examples``
    are optional JSON Schema annotations rather than validation constraints; omit them
    because wrapping their literal values would change the advertised wire value.
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    def _sanitize(value: Any, *, key: str = "") -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for item_key, item_value in value.items():
                if item_key in _MCP_SCHEMA_OMITTED_ANNOTATIONS:
                    continue
                if item_key in _MCP_SCHEMA_LITERAL_KEYWORDS:
                    sanitized[item_key] = item_value
                    continue
                sanitized[item_key] = _sanitize(item_value, key=item_key)
            return sanitized
        if isinstance(value, list):
            return [_sanitize(item, key=key) for item in value]
        if key in _MCP_SCHEMA_ANNOTATIONS and isinstance(value, str):
            return sanitize_mcp_description(f"{name}:schema:{key}", value)
        return value

    return _sanitize(schema)


def mcp_descriptor_digest(descriptors: list[dict[str, Any]]) -> str:
    """Hash the exact server tool manifest before descriptions reach the model."""
    normalized = sorted(
        descriptors,
        key=lambda item: json.dumps(
            item, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str,
        ),
    )
    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def mcp_tool_to_spec(tool: dict[str, Any], *, prefix: str = "") -> ToolSpec:
    """Convert an MCP tool descriptor to a HiveOS ToolSpec.

    MCP tools are untrusted external capability, so every one is marked dangerous —
    the executor routes them through the approval gate by default.
    """
    name = tool.get("name", "")
    return ToolSpec(
        name=f"{prefix}{name}" if prefix else name,
        description=sanitize_mcp_description(str(name), tool.get("description", "")),
        parameters=sanitize_mcp_schema(
            str(name), tool.get("inputSchema", {"type": "object", "properties": {}}),
        ),
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
        return ToolResult.from_envelope(
            self.spec.name,
            ContentEnvelope.untrusted(content, source=f"mcp:{self._remote}"),
        )


class MCPClient:
    """Thin wrapper over the official MCP SDK (lazy import). stdio by default; pass
    `url=` for an SSE (HTTP) server such as a remote Mnemosyne (A6)."""

    def __init__(self, command: str = "", args: list[str] | None = None,
                 *, url: str = "") -> None:
        self._command = command
        self._args = args or []
        self._url = url
        self._session: Any = None
        self._ctx: Any = None

    async def connect(self) -> None:  # pragma: no cover - needs the mcp SDK + a server
        try:
            from mcp import ClientSession
        except ImportError as exc:
            raise RuntimeError(
                "the 'mcp' package is required for MCPClient; pip install mcp") from exc
        if self._url:  # SSE/HTTP transport (A6: remote Mnemosyne etc.)
            from mcp.client.sse import sse_client
            self._ctx = sse_client(self._url)
        else:           # stdio transport (local subprocess server)
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client
            self._ctx = stdio_client(
                StdioServerParameters(command=self._command, args=self._args))
        read, write = await self._ctx.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()

    async def aclose(self) -> None:  # pragma: no cover - needs a live SDK transport
        """Close a connected session and its transport context."""
        session, self._session = self._session, None
        context, self._ctx = self._ctx, None
        if session is not None:
            await session.__aexit__(None, None, None)
        if context is not None:
            await context.__aexit__(None, None, None)

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
