"""cli surface — terminal + `hive` entry point.

Thin console for the assembled HiveOS:
  hive              interactive REPL (alias: hive chat)
  hive chat         interactive REPL
  hive init         first-time setup wizard
  hive serve        run the FastAPI gateway
  hive doctor [--fix] environment health checks
  hive ask "<msg>"  one-shot turn, prints the reply
  hive mcp-serve    serve Hive's tool registry as an MCP stdio server

Rendering flows through `get_output()` (Output singleton). Argument parsing
flows through `parser.parse()` → `registry.REGISTRY[cmd].handler(args)`.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from . import parser as _parser_mod
from . import registry as _registry_mod
from . import style as _style_mod  # noqa: F401 — re-exported for `from hive.surfaces.cli import style`

# ---------------------------------------------------------------------------
# Thin ANSI helpers — back-compat for tests/test_surfaces.py imports.
# Prefer `get_output()` for new code; these stay as wrappers around style.
# ---------------------------------------------------------------------------

def _ansi(code: str, text: str) -> str:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def _cyan(t: str) -> str:   return _ansi("36", t)
def _green(t: str) -> str:  return _ansi("32", t)
def _yellow(t: str) -> str: return _ansi("33", t)
def _bold(t: str) -> str:   return _ansi("1", t)
def _dim(t: str) -> str:    return _ansi("2", t)


# ---------------------------------------------------------------------------
# Banner — printed once on `hive chat` startup
# ---------------------------------------------------------------------------

_BANNER = r"""
 ██╗  ██╗██╗██╗   ██╗███████╗
 ██║  ██║██║██║   ██║██╔════╝
 ███████║██║██║   ██║█████╗
 ██╔══██║██║╚██╗ ██╔╝██╔══╝
 ██║  ██║██║ ╚████╔╝ ███████╗
 ╚═╝  ╚═╝╚═╝  ╚═══╝  ╚══════╝  OS
"""


def _print_banner(cfg=None) -> None:
    version = "0.3.0"
    try:
        from importlib.metadata import version as _v
        version = _v("hive")
    except Exception:
        pass

    memory_status = "local"
    model_name = "MiniMax"
    if cfg is not None:
        if getattr(cfg, "mnemosyne_home", None):
            memory_status = "mnemosyne"
        exec_model = getattr(cfg, "exec_model", None) or ""
        if exec_model:
            model_name = exec_model.split("/")[-1][:16]

    if not os.environ.get("NO_COLOR") and sys.stdout.isatty():
        print(_cyan(_BANNER))
        print(_bold(f"  Model: {model_name}") + _dim(f"  │  Memory: {memory_status}  │  v{version}"))
        print(_dim("  Type your message or /help · Ctrl-D to exit\n"))
    else:
        print(f"HiveOS v{version}  │  Model: {model_name}  │  Memory: {memory_status}")
        print("Type your message or /help. Ctrl-D to exit.\n")


# ---------------------------------------------------------------------------
# Slash-command handler
# ---------------------------------------------------------------------------

_SLASH_HELP = """
  /help                 — show this help
  /status               — show model, memory, session info
  /memory               — show local memory statistics
  /compact              — consolidate the current session memory
  /resume [session]     — display the active session identity
  /mcp                  — count configured MCP servers
  /tools                — list registered local tools
  /theme <name>         — select neon, minimal, or mono
  /model                — show configured provider and model
  /whoami               — show the configured owner identity
  /approvals            — count pending human approvals
  /budget               — show local call budget summary
  /doctor               — show local configuration health
  /clear                — clear the screen
  /quit                 — exit the REPL
"""

def _handle_slash(cmd: str, hive=None, session_id: str = "") -> bool:
    """Handle /command. Returns True if handled (loop should continue), False to exit."""
    parts = cmd.strip().split()
    name = parts[0].lower() if parts else ""
    if name == "/help":
        print(_SLASH_HELP)
        return True
    if name == "/status":
        model = "MiniMax"
        memory = "local"
        if hive is not None:
            model = getattr(getattr(hive, "config", None), "exec_model", None) or "MiniMax"
            memory = getattr(getattr(hive, "memory", None), "name", "local")
        print(_dim(f"  model={model}  memory={memory}  session={session_id or '(none)'}"))
        return True
    if name == "/clear":
        print("\033[2J\033[H", end="")
        return True
    if name in ("/quit", "/exit"):
        return False
    print(_yellow(f"  unknown command: {name!r}  (try /help)"))
    return True


async def _handle_slash_async(cmd: str, hive=None, session_id: str = "") -> bool:
    """Handle REPL-only commands, then defer basic commands to the stable handler."""
    parts = cmd.strip().split()
    name = parts[0].lower() if parts else ""
    if name == "/compact":
        if hive is None:
            print(_yellow("  memory is unavailable"))
            return True
        count = await hive.consolidate(session_id=session_id)
        print(_dim(f"  consolidated {count} item(s)"))
        return True
    if name == "/memory":
        memory = getattr(hive, "memory", None)
        stats = memory.memory_stats() if memory is not None else {}
        if not isinstance(stats, dict):
            stats = {}
        details = "  ".join(f"{key}={value}" for key, value in sorted(stats.items()))
        print(_dim(f"  memory={getattr(memory, 'name', 'unavailable')}  {details}".rstrip()))
        return True
    if name == "/theme":
        from .themes import REGISTRY, set_theme
        if len(parts) != 2:
            print(_yellow(f"  choose one: {', '.join(sorted(REGISTRY))}"))
            return True
        try:
            set_theme(parts[1].lower())
        except ValueError as exc:
            print(_yellow(f"  {exc}"))
        else:
            print(_dim(f"  theme={parts[1].lower()}"))
        return True
    if name == "/resume":
        requested = parts[1] if len(parts) > 1 else session_id
        if requested != session_id:
            print(_yellow("  only the active REPL session can be resumed here"))
        else:
            print(_dim(f"  active session={session_id or '(none)'}"))
        return True
    if name == "/mcp":
        config = getattr(hive, "config", None)
        count = len(getattr(config, "mcp_servers", ()) or ())
        print(_dim(f"  configured MCP servers={count}"))
        return True
    if name == "/tools":
        executor = getattr(hive, "tool_executor", None)
        names = executor.list_tools() if executor is not None else []
        print(_dim("  tools=" + (", ".join(sorted(names)) if names else "none")))
        return True
    if name == "/model":
        config = getattr(hive, "config", None)
        model = getattr(config, "exec_model", None) or "MiniMax"
        provider = getattr(config, "exec_provider", None) or "minimax"
        print(_dim(f"  provider={provider}  model={model}"))
        return True
    if name == "/whoami":
        config = getattr(hive, "config", None)
        owner = getattr(config, "telegram_admin_chat_id", None) or "owner is not configured"
        print(_dim(f"  owner={owner}"))
        return True
    if name == "/approvals":
        from Core.approval_gate import gate
        print(_dim(f"  pending approvals={len(gate.pending())}"))
        return True
    if name == "/budget":
        budgeter = getattr(hive, "budgeter", None)
        forecast = budgeter.forecast() if budgeter is not None else {}
        calls = forecast.get("calls_today", 0)
        remaining = forecast.get("remaining_calls", "unknown")
        print(_dim(f"  calls today={calls}  remaining={remaining}"))
        return True
    if name == "/doctor":
        config = getattr(hive, "config", None)
        warnings = list(config.validate()) if config is not None else ["configuration unavailable"]
        if warnings:
            print(_yellow("  doctor: " + "; ".join(str(item) for item in warnings)))
        else:
            print(_dim("  doctor: configuration OK"))
        return True
    return _handle_slash(cmd, hive=hive, session_id=session_id)

# ---------------------------------------------------------------------------
# Chat REPL
# ---------------------------------------------------------------------------

async def _chat() -> int:
    from hive.core.config import HiveConfig
    from hive.runtime import HiveOS

    cfg = HiveConfig.from_env()

    api_key = getattr(cfg, "minimax_api_key", "") or os.environ.get("MINIMAX_API_KEY", "")
    if not api_key or api_key in ("YOUR_KEY_HERE", "your-key-here", ""):
        print(_yellow("  No API key configured. Run: ") + _bold("hive init"))
        return 1

    hive = HiveOS.build(cfg)
    _print_banner(cfg)

    import uuid
    session_id = str(uuid.uuid4())

    try:
        while True:
            try:
                line = input(_green("you> ")).strip()
            except EOFError:
                print()
                break
            if not line:
                continue
            if line.lower() in ("exit", "quit", "bye"):
                break
            if line.startswith("/"):
                if not await _handle_slash_async(line, hive=hive, session_id=session_id):
                    break
                continue
            print(_dim("  thinking..."), end="\r", flush=True)
            reply = await hive.ask(line, session_id=session_id, channel_hint="cli")
            print(" " * 14 + "\r", end="")
            print(_cyan("hive> ") + str(reply))
    finally:
        await hive.aclose()
    return 0


# ---------------------------------------------------------------------------
# `hive init` — first-time setup wizard
# ---------------------------------------------------------------------------

def _init(*, non_interactive: bool = False, json_output: bool = False) -> int:
    """Run first-time setup without exposing stored secrets.

    Non-interactive mode never prompts and can emit one JSON summary for CI.
    """
    import contextlib
    import io
    import json
    import pathlib
    import secrets
    import shutil
    import subprocess

    steps: list[str] = []
    missing: list[str] = []

    def report(step: str, message: str) -> None:
        steps.append(step)
        if not json_output:
            print(message)

    if not json_output:
        print(_bold("\n  HiveOS — first-time setup\n"))
    env_candidates = [pathlib.Path.cwd() / ".env", pathlib.Path(__file__).parents[4] / ".env"]
    env_path = next((path for path in env_candidates if path.exists()), env_candidates[0])
    env_example = env_path.parent / ".env.example"
    report("workspace", f"  Workspace configuration: {env_path}")
    if not env_path.exists() and env_example.exists():
        shutil.copy(env_example, env_path)
        report("workspace", f"  Created {env_path} from .env.example")
    lines = env_path.read_text().splitlines() if env_path.exists() else []

    def get_env(key: str) -> str:
        for line in lines:
            if line.startswith(f"{key}="):
                return line[len(key) + 1:].strip().strip('"').strip("'")
        return os.environ.get(key, "")

    def set_env(key: str, value: str) -> None:
        new_line = f'{key}="{value}"'
        for index, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[index] = new_line
                return
        lines.append(new_line)

    changed = False
    provider = get_env("HIVE_EXEC_PROVIDER") or "minimax"
    if not get_env("HIVE_EXEC_PROVIDER"):
        set_env("HIVE_EXEC_PROVIDER", provider)
        changed = True
    report("provider", f"  Execution provider: {provider}")
    api_key = get_env("MINIMAX_API_KEY")
    if not api_key or api_key in ("YOUR_KEY_HERE", "your-key-here"):
        if non_interactive:
            missing.append("MINIMAX_API_KEY")
        else:
            value = input("  MINIMAX_API_KEY (press Enter to skip)> ").strip()
            if value:
                set_env("MINIMAX_API_KEY", value)
                changed = True
            else:
                missing.append("MINIMAX_API_KEY")
    report("credentials", "  MiniMax credential: configured" if "MINIMAX_API_KEY" not in missing else "  MiniMax credential: not configured")
    secret = get_env("HIVE_SECRET")
    if not secret or secret in ("change-me", "your-secret-here"):
        set_env("HIVE_SECRET", secrets.token_hex(24))
        changed = True
        report("secret_hardening", "  Generated a new HIVE_SECRET.")
    else:
        report("secret_hardening", "  HIVE_SECRET already configured.")
    memory_home = get_env("MNEMOSYNE_HOME")
    if not memory_home:
        default_home = str(pathlib.Path.home() / ".hive" / "mnemosyne")
        value = default_home if non_interactive else (input(f"  Mnemosyne memory path [{default_home}]> ").strip() or default_home)
        set_env("MNEMOSYNE_HOME", value)
        memory_home = value
        changed = True
    report("memory", "  Memory home: configured")
    telegram_configured = bool(get_env("TELEGRAM_BOT_TOKEN"))
    report("channels", "  Telegram channel: configured" if telegram_configured else "  Telegram channel: not configured (optional)")
    if changed:
        env_path.write_text("\n".join(lines) + "\n")
        report("configuration", f"  Saved {env_path}")
    from hive.core import doctor
    if json_output:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            doctor_ok = bool(doctor.run(fix=not non_interactive))
    else:
        print(_dim("\n  Running hive doctor..."))
        doctor_ok = bool(doctor.run(fix=not non_interactive))
    report("doctor", "  Doctor: passed" if doctor_ok else "  Doctor: reported issues")
    seed_script = pathlib.Path(__file__).parents[4] / "scripts" / "seed_memories.py"
    seeded = False
    if seed_script.exists() and not non_interactive:
        report("seed", "  Seeding identity memories...")
        seeded = subprocess.run([sys.executable, str(seed_script)], check=False).returncode == 0
    else:
        report("seed", "  Memory seeding skipped in non-interactive mode.")
    result = {"ok": doctor_ok, "mode": "non-interactive" if non_interactive else "interactive", "steps": steps, "missing": missing, "provider": provider, "memory_configured": bool(memory_home), "telegram_configured": telegram_configured, "seeded": seeded}
    if json_output:
        print(json.dumps(result, sort_keys=True))
    elif doctor_ok:
        print(_bold("\n  Setup complete! Run: ") + _cyan("hive chat") + "\n")
    return 0 if doctor_ok else 1

# ---------------------------------------------------------------------------
# Other commands
# ---------------------------------------------------------------------------

def _run_async(coro):
    return asyncio.run(coro)


async def _ask(message: str) -> int:
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    try:
        print(await hive.ask(message, channel_hint="cli"))
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
    from hive.autonomy.heartbeat import Heartbeat
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    try:
        await Heartbeat(hive).run()
    finally:
        await hive.aclose()
    return 0


async def _consolidate() -> int:
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    try:
        n = await hive.consolidate()
        print(f"consolidated {n} item(s)")
    finally:
        await hive.aclose()
    return 0


async def _mcp_serve() -> int:
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    try:
        await hive.serve_mcp()
    finally:
        await hive.aclose()
    return 0


# ---------------------------------------------------------------------------
# `hive version` / `hive status`
# ---------------------------------------------------------------------------

def _version() -> int:
    version = "0.3.0"
    try:
        from importlib.metadata import version as _v
        version = _v("hive")
    except Exception:
        pass
    from hive.core.config import HiveConfig
    cfg = HiveConfig.from_env()
    print(f"hive {version}")
    print(f"  model:    {cfg.exec_model}")
    print(f"  provider: {cfg.exec_provider}")
    print(f"  memory:   {cfg.mnemosyne_home}")
    return 0


def _status_snapshot(cfg) -> dict:
    """Build a secret-free, read-only status snapshot for terminal and JSON use."""
    import json

    history: list[float] = []
    history_path = cfg.data_dir / "budget_history.json"
    try:
        raw = json.loads(history_path.read_text()) if history_path.exists() else []
        if isinstance(raw, list):
            history = [float(value) for value in raw[-14:]]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        history = []
    channels = {
        "telegram": bool(cfg.telegram_token),
        "slack": bool(cfg.slack_bot_token or cfg.slack_webhook),
        "discord": bool(cfg.discord_bot_token or cfg.discord_webhook),
        "email": bool(cfg.smtp_host and cfg.smtp_from),
    }
    issues = list(cfg.validate())
    return {
        "ok": not issues,
        "provider": cfg.exec_provider,
        "model": cfg.exec_model,
        "host": cfg.host,
        "port": cfg.port,
        "state_db_exists": cfg.state_db.exists(),
        "memory_exists": cfg.mnemosyne_home.exists(),
        "learning_loop_enabled": cfg.learning_loop_enabled,
        "channels": channels,
        "budget_history_usd": history,
        "warnings": issues,
    }


def _status(*, json_output: bool = False) -> int:
    """Render a rich, secret-free local health snapshot."""
    import json

    from hive.core.config import HiveConfig

    from . import style
    from .output import get_output

    snapshot = _status_snapshot(HiveConfig.from_env())
    if json_output:
        print(json.dumps(snapshot, sort_keys=True))
        return 0 if snapshot["ok"] else 1

    out = get_output()
    out.print("\n  HiveOS Status\n", token="bold cyan")
    out.table(["Runtime", "Value"], [
        ["exec_provider", str(snapshot["provider"])],
        ["exec_model", str(snapshot["model"])],
        ["gateway", f'{snapshot["host"]}:{snapshot["port"]}'],
        ["state DB", "ready" if snapshot["state_db_exists"] else "missing"],
        ["memory", "ready" if snapshot["memory_exists"] else "not created"],
        ["learning loop", "enabled" if snapshot["learning_loop_enabled"] else "disabled"],
    ])
    out.rule()
    pills = [style.status_pill(name, "ok" if enabled else "off")
             for name, enabled in snapshot["channels"].items()]
    out.print("Channels  " + " ".join(pills))
    history = snapshot["budget_history_usd"]
    trend = style.sparkline(history, width=14) if history else "no history"
    out.print(f"Budget trend (up to 14 closed days)  {trend}")
    if snapshot["warnings"]:
        out.print("\nConfig warnings:", token="bold amber")
        for warning in snapshot["warnings"]:
            out.print(f"  • {warning}", token="amber")
    else:
        out.print("\nConfig: OK", token="cyan")
    return 0 if snapshot["ok"] else 1

# ---------------------------------------------------------------------------
# Learning commands (SPRINT_6 P-F)
# ---------------------------------------------------------------------------

def _learning_status(limit: int = 10) -> int:
    from hive.core.config import HiveConfig
    from hive.core.learning import storage
    cfg = HiveConfig.from_env()
    db = str(cfg.state_db)
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    storage.ensure_schema(db)
    counts = storage.count_by_verdict(db)
    print(_bold("\n  Learning loop status\n"))
    print(f"  enabled      : {cfg.learning_loop_enabled}")
    print(f"  eval_timeout : {cfg.learning_eval_timeout}s")
    print(f"  state_db     : {db}")
    print(f"  accept count : {counts.get('accept', 0)}")
    print(f"  reject count : {counts.get('reject', 0)}")
    recent = storage.query_loops(db, limit=max(1, limit))
    if not recent:
        print(_dim("\n  (no loop outcomes recorded yet)"))
        return 0
    print(_bold(f"\n  Recent loops (last {len(recent)}):\n"))
    for o in recent:
        vcol = _green if o.verdict == "accept" else _yellow
        print(f"  {vcol(o.verdict.upper()):>7}  id={o.id:<4} "
              f"pytest={o.pytest_candidate:.2f}/{o.pytest_baseline:.2f}  "
              f"evals={o.evals_candidate:.2f}/{o.evals_baseline:.2f}  "
              f"{_dim('symptom=' + (o.symptom[:40] or ''))}")
    return 0


def _learning_replay(loop_id: int) -> int:
    from hive.core.config import HiveConfig
    from hive.core.learning import storage
    cfg = HiveConfig.from_env()
    db = str(cfg.state_db)
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    storage.ensure_schema(db)
    loops = storage.query_loops(db, limit=1000)
    match = next((o for o in loops if o.id == loop_id), None)
    if match is None:
        print(_yellow(f"\n  No loop found with id={loop_id}"))
        return 1
    print(_bold(f"\n  Loop {match.id} (recorded {match.ts})\n"))
    print(f"  verdict       : {match.verdict}")
    print(f"  symptom       : {match.symptom}")
    print(f"  pytest        : candidate={match.pytest_candidate:.3f} "
          f"baseline={match.pytest_baseline:.3f}")
    print(f"  evals         : candidate={match.evals_candidate:.3f} "
          f"baseline={match.evals_baseline:.3f}")
    print(f"  worktree      : {match.worktree_branch}")
    print(f"  pr_url        : {match.pr_url or '(none)'}")
    print(f"  reject_reason : {match.reject_reason or '(none)'}")
    return 0


def _learning_dispatch(args: list[str]) -> int:
    """Route `hive learning <sub> ...` to status / replay."""
    if not args:
        print(_USAGE)
        return 1
    sub = args[0]
    if sub == "status":
        limit = 10
        for i, a in enumerate(args[1:], 1):
            if a == "--limit" and i < len(args) - 1:
                try:
                    limit = int(args[i + 1])
                except ValueError:
                    pass
        return _learning_status(limit)
    if sub == "replay":
        if len(args) < 2:
            print(_yellow("\n  Usage: hive learning replay <loop_id>"))
            return 1
        try:
            loop_id = int(args[1])
        except ValueError:
            print(_yellow(f"\n  Invalid loop_id: {args[1]}"))
            return 1
        return _learning_replay(loop_id)
    print(_yellow(f"\n  Unknown learning subcommand: {sub}"))
    print("  Try: hive learning status | hive learning replay <id>")
    return 1


# ---------------------------------------------------------------------------
# `hive logs [--tail N]`
# ---------------------------------------------------------------------------

def _logs(tail: int = 20) -> int:
    import datetime
    import sqlite3

    from hive.core.config import HiveConfig
    cfg = HiveConfig.from_env()

    if not cfg.state_db.exists():
        print(_yellow("  No state database found. Run: hive doctor --fix"))
        return 1

    try:
        conn = sqlite3.connect(str(cfg.state_db))
        try:
            rows = conn.execute(
                "SELECT ts, level, event, detail FROM audit_log ORDER BY ts DESC LIMIT ?",
                (tail,)
            ).fetchall()
            if not rows:
                print(_dim("  (no audit entries yet)"))
                return 0
            for ts, level, event, detail in reversed(rows):
                dt = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                level_colored = _green(level) if level == "INFO" else _yellow(level)
                print(f"  {_dim(dt)}  {level_colored}  {event}  {_dim(str(detail or '')[:60])}")
        except sqlite3.OperationalError:
            print(_dim("  (audit_log table not yet created — run: hive doctor --fix)"))
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print(_yellow(f"  Could not read logs: {exc}"))
        return 1
    return 0


# ---------------------------------------------------------------------------
# `hive budget` / `hive approvals`
# ---------------------------------------------------------------------------

async def _budget() -> int:
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    try:
        fc = hive.budgeter.forecast()
        warn = hive.budgeter.warning_status()
    finally:
        await hive.aclose()

    print(_bold("\n  HiveOS Budget\n"))
    print(f"  calls today   : {fc['calls_today']} / {fc['daily_cap']}")
    print(f"  pct used      : {fc['pct_used']:.1f}%")
    print(f"  remaining     : {fc['remaining_calls']} calls")
    days = fc.get("days_remaining")
    print(f"  days at rate  : {f'{days:.1f}' if days is not None else 'n/a'}")
    cost = fc.get("cost_usd", 0.0)
    print(f"  cost today    : ${cost:.6f}")

    if warn:
        print(_yellow(f"\n  ⚠ Budget warning: {warn}"))
    else:
        print(_green("\n  Budget: OK"))
    return 0


async def _approvals() -> int:
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    try:
        pending_edits = hive.pending_review_edits()
        from hive.core.approval import gate as _gate
        pending_gate = _gate.pending()
    finally:
        await hive.aclose()

    print(_bold("\n  HiveOS Pending Approvals\n"))

    if not pending_edits and not pending_gate:
        print(_dim("  (no pending approvals)"))
        return 0

    if pending_edits:
        print(_yellow(f"  Self-mod edits awaiting review ({len(pending_edits)}):"))
        for edit in pending_edits:
            print(f"    [{edit.get('approval_id', '?')[:8]}] "
                  f"{edit.get('op', '?')}  {edit.get('summary', '')}")

    if pending_gate:
        print(_yellow(f"\n  Gated tool calls ({len(pending_gate)}):"))
        for item in pending_gate:
            print(f"    [{str(item.get('approval_id', '?'))[:8]}] "
                  f"{item.get('tool', '?')} — {str(item.get('args', {}))[:60]}")

    return 0


# ---------------------------------------------------------------------------
# Registry population — every command, declarative.
# ---------------------------------------------------------------------------

def _int_or(default: int):
    def _coerce(value: str) -> int:
        try:
            return int(value)
        except (ValueError, TypeError):
            return default
    return _coerce


# ---------------------------------------------------------------------------
# Categorized help overview (P-J J3) + completion dispatch.
# ---------------------------------------------------------------------------

def _build_help_overview() -> None:
    """Render a categorized help overview (J3).

    Groups CommandSpec entries by `category` and prints colorized tables.
    Pure I/O — no return value.
    """
    from .output import get_output
    out = get_output()
    out.print("usage: hive [chat|init|ask|serve|heartbeat|consolidate|doctor|mcp-serve|version|status|logs|budget|approvals|learning|completion]",
              token="bold cyan")
    out.print("HiveOS terminal surface — REPL, gateway, ops commands.", token="bold cyan")
    out.rule()
    by_cat: dict[str, list] = {}
    for spec in _registry_mod.REGISTRY.values():
        by_cat.setdefault(getattr(spec, "category", "general"), []).append(spec)
    for cat in sorted(by_cat):
        out.print(f"[{cat}]", token="bold")
        for spec in sorted(by_cat[cat], key=lambda s: s.name):
            out.print(f"  {spec.name:<14} {spec.help}")
        out.rule()


def _completion(argv: list[str]) -> int:
    """Handler for `hive completion <shell>`. Prints installable script."""
    if not argv or argv[0] not in ("bash", "zsh", "fish"):
        sys.stderr.write("usage: hive completion <bash|zsh|fish>\n")
        sys.stderr.write("error: unknown shell\n")
        return 2
    from .completion import CompletionSpec, bash_completion, fish_completion, zsh_completion
    specs = [
        CompletionSpec(
            name=s.name, category=getattr(s, "category", "general"),
            help=s.help, subcommands=tuple(s.subcommands.keys()) if s.subcommands else (),
        )
        for s in _registry_mod.REGISTRY.values()
    ]
    if argv[0] == "bash":
        print(bash_completion(specs), end="")
    elif argv[0] == "zsh":
        print(zsh_completion(specs), end="")
    else:
        print(fish_completion(specs), end="")
    return 0


def _populate_registry() -> None:
    _registry_mod.REGISTRY["chat"] = _registry_mod.CommandSpec(
        name="chat",
        help="interactive REPL (default)",
        handler_name="_chat",
        category="core",
    )
    _registry_mod.REGISTRY["ask"] = _registry_mod.CommandSpec(
        name="ask",
        help="one-shot turn",
        handler_name="_ask",
        args=(("MSG", str, "message"),),
        category="core",
    )
    _registry_mod.REGISTRY["serve"] = _registry_mod.CommandSpec(
        name="serve",
        help="run the FastAPI gateway",
        handler_name="_serve",
        category="runtime",
    )
    _registry_mod.REGISTRY["init"] = _registry_mod.CommandSpec(
        name="init",
        help="first-time setup wizard",
        handler_name="_init",
        args=(("--non-interactive", None, "never prompt (CI-safe)"), ("--json", None, "emit JSON summary")),
        category="runtime",
    )
    _registry_mod.REGISTRY["doctor"] = _registry_mod.CommandSpec(
        name="doctor",
        help="environment health checks",
        handler_name="",  # dispatched inline by main
        args=(("--fix", None, "auto-repair common issues"),),
        category="runtime",
    )
    _registry_mod.REGISTRY["mcp-serve"] = _registry_mod.CommandSpec(
        name="mcp-serve",
        help="serve Hive's tool registry as an MCP stdio server",
        handler_name="_mcp_serve",
        category="runtime",
    )
    _registry_mod.REGISTRY["heartbeat"] = _registry_mod.CommandSpec(
        name="heartbeat",
        help="run the autonomy heartbeat once",
        handler_name="_heartbeat",
        category="runtime",
    )
    _registry_mod.REGISTRY["consolidate"] = _registry_mod.CommandSpec(
        name="consolidate",
        help="consolidate short-term memory into long-term",
        handler_name="_consolidate",
        category="runtime",
    )
    _registry_mod.REGISTRY["version"] = _registry_mod.CommandSpec(
        name="version",
        help="print version and config summary",
        handler_name="_version",
        category="core",
    )
    _registry_mod.REGISTRY["status"] = _registry_mod.CommandSpec(
        name="status",
        help="config + environment health summary",
        handler_name="_status",
        args=(("--json", None, "emit JSON snapshot"),),
        category="ops",
    )
    _registry_mod.REGISTRY["logs"] = _registry_mod.CommandSpec(
        name="logs",
        help="recent audit log entries",
        handler_name="_logs",
        args=(("--tail", _int_or(20), "lines to show"),),
        category="ops",
    )
    _registry_mod.REGISTRY["budget"] = _registry_mod.CommandSpec(
        name="budget",
        help="budget forecast + warning status",
        handler_name="_budget",
        category="gateway",
    )
    _registry_mod.REGISTRY["approvals"] = _registry_mod.CommandSpec(
        name="approvals",
        help="pending approval queue",
        handler_name="_approvals",
        category="gateway",
    )
    _registry_mod.REGISTRY["learning"] = _registry_mod.CommandSpec(
        name="learning",
        help="learning loop introspection (SPRINT_6 P-F)",
        handler_name="_learning_dispatch",
        category="runtime",
        subcommands={
            "status": _registry_mod.CommandSpec(
                name="status",
                help="show aggregate + recent loop outcomes",
                handler_name="_learning_status",
                args=(("--limit", _int_or(10), "max recent loops"),),
                category="ops",
            ),
            "replay": _registry_mod.CommandSpec(
                name="replay",
                help="replay a recorded loop",
                handler_name="_learning_replay",
                args=(("ID", str, "loop id"),),
                category="ops",
            ),
        },
    )
    _registry_mod.REGISTRY["completion"] = _registry_mod.CommandSpec(
        name="completion",
        help="emit shell completion script (bash|zsh|fish)",
        handler_name="_completion",
        category="core",
        args=(("SHELL", str, "bash|zsh|fish"),),
    )


_populate_registry()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_USAGE = "usage: hive [chat|init|ask|serve|heartbeat|consolidate|doctor|mcp-serve|version|status|logs|budget|approvals|learning|completion]"


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)

    if not args_list:
        return _run_async(_chat())
    if args_list[0] in ("-h", "--help", "help"):
        _build_help_overview()
        return 0

    cmd = args_list[0]
    if cmd not in _registry_mod.REGISTRY:
        print(f"unknown command: {cmd}\n{_USAGE}", file=sys.stderr)
        return 2

    if cmd == "doctor":
        from hive.core import doctor
        fix = "--fix" in args_list
        return 0 if doctor.run(fix=fix) else 1

    if cmd == "ask":
        msg = " ".join(args_list[1:]).strip()
        if not msg:
            print("usage: hive ask \"<message>\"", file=sys.stderr)
            return 2
        return _run_async(_ask(msg))

    try:
        spec, parsed = _parser_mod.parse(args_list)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        # Learning subcommand errors return 1 (consistent with original _learning_dispatch)
        if cmd == "learning" and code == 2:
            return 1
        return code

    if cmd == "status":
        if "--json" not in args_list:
            return _status()
        return _status(json_output=bool(getattr(parsed, "json", False)))

    if cmd == "init":
        if "--non-interactive" not in args_list and "--json" not in args_list:
            return _init()
        return _init(
            non_interactive=bool(getattr(parsed, "non_interactive", False)),
            json_output=bool(getattr(parsed, "json", False)),
        )

    if cmd == "completion":
        # `hive completion <bash|zsh|fish>` — argv[0] is the shell name.
        return _completion(args_list[1:])

    if cmd == "logs":
        tail = getattr(parsed, "tail", 20)
        try:
            tail = int(tail)
        except (ValueError, TypeError):
            tail = 20
        return _logs(tail)
    if cmd == "learning" and spec.name == "status":
        limit = getattr(parsed, "limit", "10")
        try:
            limit = int(limit)
        except (ValueError, TypeError):
            limit = 10
        return _learning_status(limit=limit)
    if cmd == "learning" and spec.name == "replay":
        raw = getattr(parsed, "ID", None)
        try:
            return _learning_replay(int(raw))
        except (ValueError, TypeError):
            print(_yellow(f"\n  Invalid loop_id: {raw}"))
            return 1
    if cmd == "learning" and getattr(parsed, "subcommand", None) is None:
        # `hive learning` with no sub → preserve original _learning_dispatch behavior
        return _learning_dispatch(args_list[1:])

    handler = globals().get(spec.handler_name) if spec.handler_name else None
    if handler is None:
        print(f"unknown command: {cmd}\n{_USAGE}", file=sys.stderr)
        return 2
    if asyncio.iscoroutinefunction(handler):
        return _run_async(handler())
    return handler()


if __name__ == "__main__":
    raise SystemExit(main())
