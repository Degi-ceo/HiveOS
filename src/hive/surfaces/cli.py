"""
cli.py — terminal surface + `hive` entry point.

Thin console for the assembled HiveOS:
  hive chat            interactive REPL (one HiveOS session)
  hive serve           run the FastAPI gateway (uvicorn on cfg.host:cfg.port)
  hive doctor [--fix]  environment health checks
  hive ask "<msg>"     one-shot turn, prints the reply
  hive mcp-serve       serve Hive's tool registry as an MCP stdio server

Kept deliberately small — richer surfaces (voice/telegram) are later phases. This
exists now so the declared `hive` console script actually resolves and runs.
"""
from __future__ import annotations

import asyncio
import sys


def _run_async(coro):
    return asyncio.run(coro)


async def _chat() -> int:
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    print("HiveOS chat — Ctrl-D to exit.")
    try:
        while True:
            try:
                line = input("you> ").strip()
            except EOFError:
                break
            if not line:
                continue
            print("hive>", await hive.ask(line))
    finally:
        await hive.aclose()
    return 0


async def _ask(message: str) -> int:
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    try:
        print(await hive.ask(message))
    finally:
        await hive.aclose()
    return 0


def _serve() -> int:
    import uvicorn

    from hive.gateway.app import create_app
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    uvicorn.run(create_app(hive), host=hive.config.host, port=hive.config.port)
    return 0


async def _heartbeat() -> int:
    """Run the never-idle autonomy loop (deploy: hiveos-orchestrator.service)."""
    from hive.autonomy.heartbeat import Heartbeat
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    try:
        await Heartbeat(hive).run()
    finally:
        await hive.aclose()
    return 0


async def _consolidate() -> int:
    """One-shot sleep-time memory consolidation (deploy: hiveos-keeper.timer)."""
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    try:
        n = await hive.consolidate()
        print(f"consolidated {n} item(s)")
    finally:
        await hive.aclose()
    return 0


async def _mcp_serve() -> int:
    """Serve Hive's tool registry as an MCP stdio server (M9-a).
    Lets Claude Code and other MCP clients call Hive's built-in tools directly."""
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    try:
        await hive.mcp_server().serve_stdio()
    finally:
        await hive.aclose()
    return 0


_USAGE = "usage: hive [chat|ask|serve|heartbeat|consolidate|doctor|mcp-serve]"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "chat"

    if cmd in ("-h", "--help", "help"):
        print(_USAGE)        # explicit help → stdout, exit 0 (standard CLI contract)
        return 0
    if cmd == "doctor":
        from hive.core import doctor
        return 0 if doctor.run(fix="--fix" in args) else 1
    if cmd == "serve":
        return _serve()
    if cmd == "heartbeat":
        return _run_async(_heartbeat())
    if cmd == "consolidate":
        return _run_async(_consolidate())
    if cmd == "ask":
        if len(args) < 2:
            print("usage: hive ask \"<message>\"", file=sys.stderr)
            return 2
        return _run_async(_ask(" ".join(args[1:])))
    if cmd == "chat":
        return _run_async(_chat())
    if cmd == "mcp-serve":
        return _run_async(_mcp_serve())

    print(f"unknown command: {cmd}\n{_USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
