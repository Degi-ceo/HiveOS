"""Owner-managed, default-deny configuration for outbound MCP integrations."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_MAX_SCHEMA_BYTES = 32_768
_MAX_DESCRIPTOR_DESCRIPTION = 2_000


@dataclass(frozen=True, slots=True)
class TrustedMCPServer:
    identifier: str
    transport: str
    command: str = ""
    args: tuple[str, ...] = ()
    url: str = ""
    allowed_tools: frozenset[str] = frozenset()
    environment: tuple[tuple[str, str], ...] = ()

    def stdio_env(self) -> dict[str, str]:
        """Only platform necessities plus explicit literal manifest values."""
        env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "ComSpec", "PATHEXT") if key in os.environ}
        env.update(dict(self.environment))
        return env


def load_trusted_servers(root: Path, requested_ids: tuple[str, ...]) -> list[TrustedMCPServer]:
    """Load named manifest entries; absent/invalid manifests select nothing."""
    path = root / "Config" / "mcp-trust.json"
    if not path.is_file() or not requested_ids:
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("servers"), dict):
        return []
    output: list[TrustedMCPServer] = []
    # Duplicate IDs must not yield two clients with the same namespace.
    for identifier in dict.fromkeys(requested_ids):
        entry = raw["servers"].get(identifier)
        server = _parse_server(identifier, entry)
        if server is not None:
            output.append(server)
    return output


def _parse_server(identifier: str, entry: object) -> TrustedMCPServer | None:
    if (not isinstance(identifier, str) or not _IDENTIFIER_RE.fullmatch(identifier)
            or not isinstance(entry, dict) or entry.get("enabled") is not True):
        return None
    transport, tools = entry.get("transport"), entry.get("allowed_tools", [])
    if transport not in {"stdio", "sse"} or not isinstance(tools, list) or not all(isinstance(item, str) and item for item in tools):
        return None
    allowed_tools = frozenset(tools)
    if transport == "stdio":
        command, args, environment = entry.get("command"), entry.get("args", []), entry.get("environment", {})
        if (not isinstance(command, str) or not Path(command).is_absolute() or not Path(command).is_file()
                or not isinstance(args, list) or not all(isinstance(item, str) for item in args)
                or not isinstance(environment, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items())):
            return None
        return TrustedMCPServer(identifier, transport, command, tuple(args), allowed_tools=allowed_tools,
                                environment=tuple(sorted(environment.items())))
    url, allowed_hosts = entry.get("url"), entry.get("allowed_hosts", [])
    parsed = urlparse(url) if isinstance(url, str) else None
    if (parsed is None or parsed.scheme != "https" or not parsed.hostname
            or not isinstance(allowed_hosts, list) or parsed.hostname not in allowed_hosts):
        return None
    return TrustedMCPServer(identifier, transport, url=url, allowed_tools=allowed_tools)


def trusted_descriptors(descriptors: list[dict[str, Any]], server: TrustedMCPServer) -> list[dict[str, Any]]:
    """Return bounded, owner-allowlisted descriptors safe for Hive's tool surface.

    Remote descriptions are deliberately not exposed to the model: they are
    untrusted text and are unnecessary once the owner has explicitly selected a
    tool name in the manifest.  Invalid or duplicate descriptors are rejected
    rather than coerced into a partially trusted tool.
    """
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            continue
        name = descriptor.get("name")
        schema = descriptor.get("inputSchema", {"type": "object", "properties": {}})
        description = descriptor.get("description", "")
        if (not isinstance(name, str) or name not in server.allowed_tools
                or not _TOOL_NAME_RE.fullmatch(name) or name in seen
                or not isinstance(schema, dict) or not isinstance(description, str)
                or len(description) > _MAX_DESCRIPTOR_DESCRIPTION):
            continue
        try:
            if len(json.dumps(schema, separators=(",", ":"))) > _MAX_SCHEMA_BYTES:
                continue
        except (TypeError, ValueError):
            continue
        seen.add(name)
        output.append({
            "name": name,
            "description": f"Owner-trusted MCP tool '{server.identifier}.{name}'. Requires approval.",
            "inputSchema": schema,
        })
    return output
