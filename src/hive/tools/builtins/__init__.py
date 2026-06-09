"""
builtins — Hive's built-in tools (KEEP from Tools/registry.py builtins).

Safe: read_file / write_file / shell / web_get. Dangerous (always gated by the
executor via the PROTECTED approval gate): spend_money / deploy / external_message.
Destructive shell/file actions are caught by gate.is_dangerous even though `shell`
is not flagged dangerous itself, so routine commands stay fast.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import httpx

from hive.core.types import ToolResult
from hive.tools.base import BaseTool, ToolSpec
from hive.tools.registry import ToolRegistry


class ReadFile(BaseTool):
    spec = ToolSpec(
        name="read_file", description="Read a UTF-8 text file (truncated).",
        parameters={"type": "object", "properties": {"path": {"type": "string"}},
                    "required": ["path"]}, category="files")

    async def execute(self, path: str, **_: Any) -> ToolResult:
        text = Path(path).read_text(encoding="utf-8")[:20_000]
        return ToolResult(tool_name="read_file", content=text)


class WriteFile(BaseTool):
    spec = ToolSpec(
        name="write_file", description="Write a UTF-8 text file (creates parents).",
        parameters={"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}, category="files")

    async def execute(self, path: str, content: str, **_: Any) -> ToolResult:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ToolResult(tool_name="write_file", content=f"wrote {len(content)} chars to {path}")


class Shell(BaseTool):
    spec = ToolSpec(
        name="shell", description="Run a shell command (destructive commands are gated).",
        parameters={"type": "object", "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"]}, category="system")

    async def execute(self, cmd: str, **_: Any) -> ToolResult:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _err = await proc.communicate()
        return ToolResult(tool_name="shell", content=out.decode()[:8_000],
                          success=proc.returncode == 0)


class WebGet(BaseTool):
    spec = ToolSpec(
        name="web_get", description="HTTP GET a URL and return text (truncated).",
        parameters={"type": "object", "properties": {"url": {"type": "string"}},
                    "required": ["url"]}, category="web")

    async def execute(self, url: str, **_: Any) -> ToolResult:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.get(url)
            return ToolResult(tool_name="web_get", content=r.text[:12_000],
                              success=r.is_success)


class _Gated(BaseTool):
    """Dangerous tools: real side effects live in the surface; here they confirm intent."""
    _name = ""
    _desc = ""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self._name, description=self._desc, dangerous=True,
                        category="gated")


class SpendMoney(_Gated):
    _name, _desc = "spend_money", "Spend money (requires approval)."

    async def execute(self, what: str = "", amount: str = "", **_: Any) -> ToolResult:
        return ToolResult(tool_name="spend_money", content=f"spend {amount} on {what}")


class Deploy(_Gated):
    _name, _desc = "deploy", "Deploy to a target (requires approval)."

    async def execute(self, target: str = "", **_: Any) -> ToolResult:
        return ToolResult(tool_name="deploy", content=f"deployed to {target}")


class ExternalMessage(_Gated):
    _name, _desc = "external_message", "Send an external message (requires approval)."

    async def execute(self, to: str = "", body: str = "", **_: Any) -> ToolResult:
        return ToolResult(tool_name="external_message", content=f"sent to {to}")


BUILTIN_TOOLS: tuple[type[BaseTool], ...] = (
    ReadFile, WriteFile, Shell, WebGet, SpendMoney, Deploy, ExternalMessage,
)


def register_builtins(registry: type[ToolRegistry] = ToolRegistry) -> dict[str, BaseTool]:
    """Instantiate + register every builtin. Returns the name->tool snapshot."""
    for tool_cls in BUILTIN_TOOLS:
        registry.add(tool_cls())
    return registry.snapshot()
