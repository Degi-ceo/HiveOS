"""
registry.py — audited tool registry for Hive.

Safe tools run immediately. Dangerous ones route through the Approval Gate and
do not execute until Kamil approves. Every call is appended to data/audit.log.
Register tools with @tool(name, dangerous=False).
"""
from __future__ import annotations
import time
import json
import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Callable, Awaitable

import httpx

from core import settings
from core.approval_gate import gate

log = logging.getLogger("hiveos.tools")
AUDIT = Path(settings.DATA_DIR) / "audit.log"

_REGISTRY: dict[str, dict] = {}


def tool(name: str, dangerous: bool = False) -> Callable:
    def deco(fn: Callable[..., Awaitable]) -> Callable:
        _REGISTRY[name] = {"fn": fn, "dangerous": dangerous}
        return fn
    return deco


def _audit(tool: str, args: dict, result, status: str) -> None:
    with AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": time.time(), "tool": tool, "args": args,
            "status": status, "result": str(result)[:500],
        }) + "\n")


def list_tools() -> list[str]:
    return sorted(_REGISTRY)


async def call(name: str, args: dict, reason: str = "") -> dict:
    spec = _REGISTRY.get(name)
    if not spec:
        return {"status": "error", "error": f"unknown tool {name}"}
    if spec["dangerous"] or gate.is_dangerous(name, args):
        aid = gate.request(name, args, reason)
        _audit(name, args, f"awaiting approval {aid}", "pending")
        return {"status": "pending_approval", "approval_id": aid}
    try:
        result = await spec["fn"](**args)
        _audit(name, args, result, "ok")
        return {"status": "ok", "result": result}
    except Exception as e:  # noqa: BLE001
        _audit(name, args, str(e), "error")
        return {"status": "error", "error": str(e)}


async def execute_approved(item: dict) -> dict:
    spec = _REGISTRY[item["tool"]]
    result = await spec["fn"](**item["args"])
    _audit(item["tool"], item["args"], result, "ok_approved")
    return {"status": "ok", "result": result}


# --- safe built-in tools ----------------------------------------------------

@tool("read_file")
async def read_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")[:20000]


@tool("write_file")
async def write_file(path: str, content: str) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {path}"


@tool("shell")
async def shell(cmd: str) -> str:
    """Non-destructive shell; destructive patterns are caught by the gate first."""
    p = await asyncio.create_subprocess_shell(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    out, _ = await p.communicate()
    return out.decode()[:8000]


@tool("web_get")
async def web_get(url: str) -> str:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(url)
        return r.text[:12000]


# --- dangerous tools (always gated) -----------------------------------------

@tool("spend_money", dangerous=True)
async def spend_money(what: str, amount: str) -> str:
    return f"approved spend: {amount} on {what}"


@tool("deploy", dangerous=True)
async def deploy(target: str) -> str:
    return f"deployed to {target}"


@tool("external_message", dangerous=True)
async def external_message(to: str, body: str) -> str:
    return f"sent to {to}"
