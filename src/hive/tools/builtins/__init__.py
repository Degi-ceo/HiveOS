"""
builtins — Hive's built-in tools (KEEP from Tools/registry.py builtins).

Safe: read_file / write_file / shell / web_get. Dangerous (always gated by the
executor via the PROTECTED approval gate): spend_money / deploy / external_message.
Destructive shell/file actions are caught by gate.is_dangerous even though `shell`
is not flagged dangerous itself, so routine commands stay fast.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from hive.core.types import ToolResult
from hive.tools import discovery as _discovery
from hive.tools.base import BaseTool, ToolSpec
from hive.tools.registry import ToolRegistry
from hive.tools.shell_provider import LocalShellProvider, ShellProvider


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

    def __init__(self, provider: ShellProvider | None = None) -> None:
        self._provider = provider or LocalShellProvider()

    async def execute(self, cmd: str, **_: Any) -> ToolResult:
        result = await self._provider.run(cmd)
        return ToolResult(tool_name="shell", content=result.stdout[:8_000],
                          success=result.returncode == 0)


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
        return ToolResult(
            tool_name="spend_money",
            content=(
                f"[spend_money: no payment backend configured; "
                f"requested: {amount} for '{what}'. "
                f"Wire a Stripe/Revolut adapter to enable this.]"
            ),
        )


_SAFE_DEPLOY_TARGETS = {"gateway", "orchestrator", "keeper"}


class Deploy(_Gated):
    _name, _desc = "deploy", "Deploy to a target (requires approval)."

    async def execute(self, target: str = "", **_: Any) -> ToolResult:
        if target not in _SAFE_DEPLOY_TARGETS:
            return ToolResult(
                tool_name="deploy",
                content=(
                    f"[deploy: unknown target {target!r}; "
                    f"valid targets: {sorted(_SAFE_DEPLOY_TARGETS)}]"
                ),
            )
        import asyncio
        proc = await asyncio.create_subprocess_shell(
            f"systemctl restart hiveos-{target}.service",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        status = "ok" if proc.returncode == 0 else f"exit {proc.returncode}"
        return ToolResult(
            tool_name="deploy",
            content=f"hiveos-{target}: {status}\n{out.decode(errors='replace')}".strip(),
        )


class ExternalMessage(_Gated):
    _name, _desc = "external_message", "Send an external message (requires approval)."

    def __init__(self, telegram_token: str = "") -> None:
        self._telegram_token = telegram_token

    async def execute(self, to: str = "", body: str = "", **_: Any) -> ToolResult:
        if not self._telegram_token:
            return ToolResult(
                tool_name="external_message",
                content="[external_message: TELEGRAM_BOT_TOKEN not set]",
            )
        from hive.gateway.channels.base import OutgoingMessage
        from hive.gateway.channels.telegram import TelegramChannel
        channel = TelegramChannel(self._telegram_token)
        try:
            result = await channel.send(OutgoingMessage(chat_id=to, text=body))
        finally:
            await channel.aclose()
        if result.ok:
            return ToolResult(tool_name="external_message",
                              content=f"sent to {to} (msg_id={result.message_id})")
        return ToolResult(tool_name="external_message",
                          content=f"[external_message: send failed — {result.error}]")


class DiscoverTool(BaseTool):
    """Discovery-first (HARD SOUL rule): search official sources for an existing
    skill/MCP/library BEFORE building. Read-only (network search), so not gated.
    Caches via memory when the provider supports recall/learn (LocalMemoryProvider)."""

    spec = ToolSpec(
        name="discover",
        description="Search official sources (MCP registry, GitHub) for an existing "
                    "skill/MCP server/library before building; results cached to memory.",
        parameters={"type": "object", "properties": {"need": {"type": "string"}},
                    "required": ["need"]}, category="discovery")

    def __init__(self, memory: Any = None, github_token: str = "") -> None:
        # Only use memory for caching if it duck-types discovery.MemoryLike.
        self._memory = memory if (hasattr(memory, "recall") and hasattr(memory, "learn")) else None
        self._token = github_token

    async def execute(self, need: str, **_: Any) -> ToolResult:
        import json
        result = await _discovery.discover(need, memory=self._memory, github_token=self._token)
        return ToolResult(tool_name="discover", content=json.dumps(result)[:8_000])


BUILTIN_TOOLS: tuple[type[BaseTool], ...] = (
    ReadFile, WriteFile, Shell, WebGet, SpendMoney, Deploy,
)


def register_builtins(registry: type[ToolRegistry] = ToolRegistry, *,
                      memory: Any = None, github_token: str = "",
                      telegram_token: str = "") -> dict[str, BaseTool]:
    """Instantiate + register every builtin. Returns the name->tool snapshot.
    `memory`/`github_token` are injected into the discovery-first tool (A1).
    `telegram_token` enables ExternalMessage to send real Telegram messages."""
    for tool_cls in BUILTIN_TOOLS:
        registry.add(tool_cls())
    registry.add(ExternalMessage(telegram_token=telegram_token))
    registry.add(DiscoverTool(memory=memory, github_token=github_token))
    return registry.snapshot()
