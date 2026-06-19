"""
builtins — Hive's built-in tools (KEEP from Tools/registry.py builtins).

Safe: read_file / write_file / shell / web_get. Dangerous (always gated by the
executor via the PROTECTED approval gate): spend_money / deploy / external_message.
Destructive shell/file actions are caught by gate.is_dangerous even though `shell`
is not flagged dangerous itself, so routine commands stay fast.
"""
from __future__ import annotations

import ipaddress
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from hive.core.types import ToolResult
from hive.tools import discovery as _discovery
from hive.tools.base import BaseTool, ToolSpec
from hive.tools.registry import ToolRegistry
from hive.tools.shell_provider import LocalShellProvider, ShellProvider

_BLOCKED_NETS = [ipaddress.ip_network(n) for n in [
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "127.0.0.0/8", "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10",
]]


def _validate_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Blocked scheme: {parsed.scheme!r}")
    if parsed.username or parsed.password:
        raise ValueError("URL userinfo not allowed")
    host = parsed.hostname or ""
    try:
        addr = ipaddress.ip_address(host)
        if any(addr in net for net in _BLOCKED_NETS):
            raise ValueError(f"Blocked address: {addr}")
    except ValueError as exc:
        if "Blocked" in str(exc):
            raise


def _check_redirect(response: httpx.Response) -> None:
    """httpx event hook: block redirects to private/loopback addresses."""
    if response.is_redirect:
        location = response.headers.get("location", "")
        if location:
            try:
                _validate_url(location)
            except ValueError as exc:
                raise ValueError(f"SSRF redirect blocked: {exc}") from exc


class ReadFile(BaseTool):
    spec = ToolSpec(
        name="read_file", description="Read a UTF-8 text file (truncated).",
        parameters={"type": "object", "properties": {"path": {"type": "string"}},
                    "required": ["path"]}, category="files")

    async def execute(self, **params: Any) -> ToolResult:
        path = str(params.get("path", ""))
        text = Path(path).read_text(encoding="utf-8", errors="replace")[:20_000]
        return ToolResult(tool_name="read_file", content=text)


class WriteFile(BaseTool):
    spec = ToolSpec(
        name="write_file", description="Write a UTF-8 text file (creates parents).",
        parameters={"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"},
            "mode": {"type": "string", "enum": ["w", "a"], "default": "w"}},
            "required": ["path", "content"]}, category="files")

    async def execute(self, **params: Any) -> ToolResult:
        path = str(params.get("path", ""))
        content = str(params.get("content", ""))
        mode = str(params.get("mode", "w"))
        if mode not in {"w", "a"}:
            return ToolResult(tool_name="write_file", content=f"invalid mode: {mode}", success=False)
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open(mode, encoding="utf-8") as f:
            f.write(content)
        action = "appended" if mode == "a" else "wrote"
        return ToolResult(tool_name="write_file", content=f"{action} {len(content)} chars to {path}")


class DeleteFile(BaseTool):
    spec = ToolSpec(
        name="delete_file",
        description="Delete a file (subject to path safety checks).",
        parameters={"type": "object", "properties": {"path": {"type": "string"}},
                    "required": ["path"]},
        category="files",
    )

    async def execute(self, **params: Any) -> ToolResult:
        path = str(params.get("path", ""))
        p = Path(path)
        if not p.exists():
            return ToolResult(tool_name="delete_file", content=f"file not found: {path}", success=False)
        p.unlink()
        return ToolResult(tool_name="delete_file", content=f"deleted: {path}")


class Shell(BaseTool):
    spec = ToolSpec(
        name="shell", description="Run a shell command (destructive commands are gated).",
        parameters={"type": "object", "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"]}, category="system")

    def __init__(self, provider: ShellProvider | None = None) -> None:
        self._provider = provider or LocalShellProvider()

    async def execute(self, **params: Any) -> ToolResult:
        cmd = str(params.get("cmd", ""))
        result = await self._provider.run(cmd)
        return ToolResult(tool_name="shell", content=result.stdout[:8_000],
                          success=result.returncode == 0)


class WebGet(BaseTool):
    spec = ToolSpec(
        name="web_get", description="HTTP GET a URL and return text (truncated).",
        parameters={"type": "object", "properties": {"url": {"type": "string"}},
                    "required": ["url"]}, category="web")

    async def execute(self, **params: Any) -> ToolResult:
        url = str(params.get("url", ""))
        try:
            _validate_url(url)
        except ValueError as exc:
            return ToolResult(tool_name="web_get", content=f"[blocked: {exc}]", success=False)
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            max_redirects=10,
            event_hooks={"response": [_check_redirect]},
        ) as c:
            r = await c.get(url)
            return ToolResult(tool_name="web_get", content=r.text[:12_000],
                              success=r.is_success)


class _Gated(BaseTool):
    """Dangerous tools: real side effects live in the surface; here they confirm intent."""
    _name = ""
    _desc = ""

    def __init__(self) -> None:
        self.spec = ToolSpec(name=self._name, description=self._desc, dangerous=True,
                             category="gated")


class SpendMoney(_Gated):
    _name, _desc = "spend_money", "Spend money (requires approval)."

    async def execute(self, **params: Any) -> ToolResult:
        what = str(params.get("what", ""))
        amount = str(params.get("amount", ""))
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

    async def execute(self, **params: Any) -> ToolResult:
        target = str(params.get("target", ""))
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
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return ToolResult(tool_name="deploy", content=f"hiveos-{target}: timeout after 30s")
        if proc.returncode is None:
            return ToolResult(tool_name="deploy", content=f"hiveos-{target}: no return code", success=False)
        status = "ok" if proc.returncode == 0 else f"exit {proc.returncode}"
        return ToolResult(
            tool_name="deploy",
            content=f"hiveos-{target}: {status}\n{out.decode(errors='replace')}".strip(),
        )


class ExternalMessage(_Gated):
    _name, _desc = "external_message", "Send an external message via telegram/email/slack (requires approval)."

    def __init__(self, telegram_token: str = "",
                 smtp_host: str = "", smtp_port: int = 587,
                 smtp_user: str = "", smtp_pass: str = "", smtp_to: str = "",
                 slack_webhook: str = "") -> None:
        super().__init__()
        self._telegram_token = telegram_token
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._smtp_user = smtp_user
        self._smtp_pass = smtp_pass
        self._smtp_to = smtp_to
        self._slack_webhook = slack_webhook

    async def execute(self, **params: Any) -> ToolResult:
        to = str(params.get("to", ""))
        body = str(params.get("body", ""))
        channel = str(params.get("channel", "telegram")).lower()

        if channel == "email":
            return await self._send_email(body)
        if channel == "slack":
            return await self._send_slack(body)
        # default: telegram
        return await self._send_telegram(to, body)

    async def _send_telegram(self, to: str, body: str) -> ToolResult:
        if not self._telegram_token:
            return ToolResult(tool_name="external_message",
                              content="[external_message: TELEGRAM_BOT_TOKEN not set]")
        from hive.gateway.channels.base import OutgoingMessage
        from hive.gateway.channels.telegram import TelegramChannel
        ch = TelegramChannel(self._telegram_token)
        try:
            result = await ch.send(OutgoingMessage(chat_id=to, text=body))
        finally:
            await ch.aclose()
        if result.ok:
            return ToolResult(tool_name="external_message",
                              content=f"sent to {to} (msg_id={result.message_id})")
        return ToolResult(tool_name="external_message",
                          content=f"[external_message: send failed — {result.error}]")

    async def _send_email(self, body: str) -> ToolResult:
        if not all([self._smtp_host, self._smtp_user, self._smtp_pass, self._smtp_to]):
            return ToolResult(tool_name="external_message", success=False,
                              content="[email: set HIVE_SMTP_HOST/USER/PASS/TO]")
        import asyncio
        import email.mime.text as _mime
        import smtplib

        msg = _mime.MIMEText(body)
        msg["Subject"] = f"[Hive] {body[:60]}"
        msg["From"] = self._smtp_user
        msg["To"] = self._smtp_to

        def _send() -> None:
            with smtplib.SMTP(self._smtp_host, self._smtp_port) as s:
                s.starttls()
                s.login(self._smtp_user, self._smtp_pass)
                s.sendmail(self._smtp_user, [self._smtp_to], msg.as_string())

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _send)
        return ToolResult(tool_name="external_message",
                          content=f"Email sent to {self._smtp_to}", cost_usd=0.0)

    async def _send_slack(self, body: str) -> ToolResult:
        if not self._slack_webhook:
            return ToolResult(tool_name="external_message", success=False,
                              content="[slack: set HIVE_SLACK_WEBHOOK]")
        import asyncio
        import json as _json
        import urllib.request

        payload = _json.dumps({"text": body}).encode()

        def _post() -> int:
            req = urllib.request.Request(
                self._slack_webhook, data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
                return r.status

        loop = asyncio.get_event_loop()
        status = await loop.run_in_executor(None, _post)
        ok = status == 200
        return ToolResult(tool_name="external_message",
                          content=f"Slack: {'ok' if ok else 'failed'}", cost_usd=0.0,
                          success=ok)


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

    def __init__(self, memory: Any = None, github_token: str = "",
                 enable_security_audit: bool = True) -> None:
        # Only use memory for caching if it duck-types discovery.MemoryLike.
        self._memory = memory if (hasattr(memory, "recall") and hasattr(memory, "learn")) else None
        self._token = github_token
        self._enable_security_audit = enable_security_audit

    async def execute(self, **params: Any) -> ToolResult:
        import json
        need = str(params.get("need", ""))
        security_delegate = None
        if self._enable_security_audit:
            async def _sec(task: str) -> str:
                from hive.agents.delegate import delegate_named  # local import, DAG OK
                results = await delegate_named([task], "security-reviewer")
                return results[0].content if results else "[no result]"
            security_delegate = _sec
        result = await _discovery.discover(
            need, memory=self._memory, github_token=self._token,
            security_delegate=security_delegate)
        return ToolResult(tool_name="discover", content=json.dumps(result)[:8_000])


class DelegateToSpecialist(BaseTool):
    """Delegate a task to a named specialist sub-agent (SOUL.md: Hive is the CEO).

    Available agents: researcher, coder, reviewer, memory-keeper, security-reviewer.
    The specialist runs in an isolated leaf context — it cannot re-delegate."""

    spec = ToolSpec(
        name="delegate_to_specialist",
        description="Delegate a task to a named specialist sub-agent. "
                    "Use before doing deep work yourself. "
                    "Available agents: researcher, coder, reviewer, memory-keeper, security-reviewer.",
        parameters={
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "enum": ["researcher", "coder", "reviewer", "memory-keeper", "security-reviewer"],
                    "description": "Specialist to delegate to.",
                },
                "task": {
                    "type": "string",
                    "description": "Full task description for the specialist.",
                },
            },
            "required": ["agent", "task"],
        },
        category="agents",
    )

    async def execute(self, **params: Any) -> ToolResult:
        from hive.agents.delegate import delegate_named
        agent = str(params.get("agent", ""))
        task = str(params.get("task", ""))
        try:
            results = await delegate_named([task], agent)
            content = results[0].content if results else "[no result]"
        except KeyError as exc:
            content = f"[delegate error: {exc}]"
        except Exception as exc:  # noqa: BLE001
            content = f"[delegate error: {type(exc).__name__}: {exc}]"
        return ToolResult(tool_name="delegate_to_specialist", content=content[:12_000])


BUILTIN_TOOLS: tuple[type[BaseTool], ...] = (
    ReadFile, WriteFile, DeleteFile, Shell, WebGet, SpendMoney, Deploy,
    DelegateToSpecialist,
)


def register_builtins(registry: type[ToolRegistry] = ToolRegistry, *,
                      memory: Any = None, github_token: str = "",
                      telegram_token: str = "",
                      smtp_host: str = "", smtp_port: int = 587,
                      smtp_user: str = "", smtp_pass: str = "", smtp_to: str = "",
                      slack_webhook: str = "",
                      shell_provider: ShellProvider | None = None) -> dict[str, BaseTool]:
    """Instantiate + register every builtin. Returns the name->tool snapshot.
    `memory`/`github_token` are injected into the discovery-first tool (A1).
    `telegram_token` enables ExternalMessage to send Telegram messages.
    SMTP params enable email sending; `slack_webhook` enables Slack messages.
    `shell_provider` overrides the default LocalShellProvider (e.g. DockerShellProvider)."""
    for tool_cls in BUILTIN_TOOLS:
        if tool_cls is Shell and shell_provider is not None:
            registry.add(Shell(provider=shell_provider))
        else:
            registry.add(tool_cls())
    registry.add(ExternalMessage(
        telegram_token=telegram_token,
        smtp_host=smtp_host, smtp_port=smtp_port,
        smtp_user=smtp_user, smtp_pass=smtp_pass, smtp_to=smtp_to,
        slack_webhook=slack_webhook,
    ))
    registry.add(DiscoverTool(memory=memory, github_token=github_token,
                              enable_security_audit=True))
    return registry.snapshot()
