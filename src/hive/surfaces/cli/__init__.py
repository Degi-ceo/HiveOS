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
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
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
  /help    — show this help
  /status  — show model, memory, session info
  /session — show the active conversation ID
  /clear   — clear the screen
  /quit    — exit the REPL
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
    if name == "/session":
        print(_dim(f"  active session={session_id or '(none)'}"))
        return True
    if name == "/clear":
        print("\033[2J\033[H", end="")
        return True
    if name in ("/quit", "/exit"):
        return False
    print(_yellow(f"  unknown command: {name!r}  (try /help)"))
    return True


# ---------------------------------------------------------------------------
# Chat REPL
# ---------------------------------------------------------------------------

async def _chat(session_id: str | None = None) -> int:
    from hive.core.config import HiveConfig
    from hive.runtime import HiveOS

    cfg = HiveConfig.from_env()
    hive = HiveOS.build(cfg, validate_inbound_channels=False)

    try:
        # Build injects credentials from Hive's native vault.  Checking before
        # build made ``hive chat`` reject a valid vault-only installation.
        provider = str(getattr(cfg, "exec_provider", "minimax")).lower()
        key_env = "ANTHROPIC_API_KEY" if provider == "anthropic" else "MINIMAX_API_KEY"
        config_key_name = "anthropic_api_key" if provider == "anthropic" else "minimax_api_key"
        config_key = getattr(cfg, config_key_name, "")
        api_key = config_key or os.environ.get(key_env, "")
        if not api_key or api_key in ("YOUR_KEY_HERE", "your-key-here", ""):
            print(_yellow("  No API key configured. Run: ") + _bold("hive init"))
            return 1

        _print_banner(cfg)
        if not session_id:
            import uuid
            session_id = str(uuid.uuid4())
        else:
            session_id = str(session_id).strip()
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
                if not _handle_slash(line, hive=hive, session_id=session_id):
                    break
                continue
            print(_dim("  thinking..."), end="\r", flush=True)
            print(" " * 14 + "\r", end="")
            await _terminal_turn(hive, line, session_id=session_id)
    finally:
        await hive.aclose()
    return 0


# ---------------------------------------------------------------------------
# `hive init` — first-time setup wizard
# ---------------------------------------------------------------------------

def _init() -> int:
    """Interactive first-run wizard: set API keys, run doctor, seed memories."""
    import pathlib

    print(_bold("\n  HiveOS — first-time setup\n"))

    env_candidates = [
        pathlib.Path.cwd() / ".env",
        pathlib.Path(__file__).parents[4] / ".env",
    ]
    env_path = next((p for p in env_candidates if p.exists()), env_candidates[0])
    env_example = env_path.parent / ".env.example"

    if not env_path.exists() and env_example.exists():
        import shutil
        shutil.copy(env_example, env_path)
        print(f"  Created {env_path} from .env.example")

    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text().splitlines()

    def _get_env_val(key: str) -> str:
        for line in lines:
            if line.startswith(f"{key}="):
                return line[len(key) + 1:].strip().strip('"').strip("'")
        return os.environ.get(key, "")

    def _set_env_val(key: str, val: str) -> None:
        nonlocal lines
        new_line = f'{key}="{val}"'
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = new_line
                return
        lines.append(new_line)

    changed = False

    current_key = _get_env_val("MINIMAX_API_KEY")
    if not current_key or current_key in ("YOUR_KEY_HERE", "your-key-here"):
        print("  Enter your MiniMax API key (or press Enter to skip):")
        val = input("  MINIMAX_API_KEY> ").strip()
        if val:
            _set_env_val("MINIMAX_API_KEY", val)
            changed = True

    current_secret = _get_env_val("HIVE_SECRET")
    if not current_secret or current_secret in ("change-me", "your-secret-here", ""):
        import secrets as _sec
        new_secret = _sec.token_hex(24)
        print(f"  Generated new HIVE_SECRET: {new_secret[:8]}...")
        _set_env_val("HIVE_SECRET", new_secret)
        changed = True

    current_mnem = _get_env_val("HIVE_MNEMOSYNE_HOME")
    if not current_mnem:
        default_mnem = str(pathlib.Path.home() / ".hive" / "mnemosyne")
        print(f"  Mnemosyne memory path [{default_mnem}] (Enter to use default):")
        val = input("  HIVE_MNEMOSYNE_HOME> ").strip() or default_mnem
        _set_env_val("HIVE_MNEMOSYNE_HOME", val)
        changed = True

    if changed:
        env_path.write_text("\n".join(lines) + "\n")
        print(f"  Saved {env_path}")

    print(_dim("\n  Running hive doctor --fix..."))
    from hive.core import doctor
    doctor.run(fix=True)

    seed_script = pathlib.Path(__file__).parents[4] / "scripts" / "seed_memories.py"
    if seed_script.exists():
        print(_dim("  Seeding identity memories..."))
        import subprocess
        subprocess.run([sys.executable, str(seed_script)], check=False)

    print(_bold("\n  Setup complete! Run: ") + _cyan("hive chat") + "\n")
    return 0


# ---------------------------------------------------------------------------
# Other commands
# ---------------------------------------------------------------------------

def _run_async(coro):
    return asyncio.run(coro)


async def _terminal_turn(hive, message: str, *, session_id: str) -> int:
    """Render the audited, user-visible turn lifecycle for a local operator.

    The stream deliberately exposes tool names and terminal statuses, but not
    raw model reasoning, arguments, or tool output. Those values may contain
    private context or credentials; the durable audit and trace stores remain
    the controlled source for authorised detailed inspection.
    """
    saw_terminal_event = False
    error_code: int | None = None
    announced_run_id = ""
    stream = hive.stream_ask_iterations(
        message, session_id=session_id, channel_hint="cli",
    )
    try:
        async for event in stream:
            event_type = str(event.get("type", ""))
            run_id = str(event.get("run_id", ""))
            if run_id and run_id != announced_run_id:
                print(_dim(f"  run: {run_id}"))
                announced_run_id = run_id
            if event_type == "model_decision":
                names = [str(call.get("name", "tool"))
                         for call in event.get("tool_calls", [])]
                if names:
                    print(_dim("  plan: requested " + ", ".join(names)))
            elif event_type == "tool_call_start":
                print(_dim(f"  tool: {event.get('name', 'unknown')} started"))
            elif event_type == "tool_call_end":
                duration = event.get("duration_ms")
                timing = f" ({duration} ms)" if duration is not None else ""
                summary = f" — {event['summary']}" if event.get("summary") else ""
                print(_dim(
                    f"  tool: {event.get('name', 'unknown')} "
                    f"{event.get('status', 'finished')}{timing}{summary}"
                ))
            elif event_type == "subagent_start":
                print(_dim(f"  subagent: {event.get('agent', 'specialist')} started"))
            elif event_type == "subagent_end":
                print(_dim(
                    f"  subagent: {event.get('agent', 'specialist')} "
                    f"{event.get('status', 'finished')}"
                ))
            elif event_type == "loop_guard":
                print(_yellow(f"  safety stop: {event.get('reason', 'loop guard')}"))
            elif event_type in ("final", "max_turns"):
                print(_cyan("hive> ") + str(event.get("text", "")))
                saw_terminal_event = True
            elif event_type == "error":
                error_class = str(event.get("class", "RuntimeError"))
                if error_class == "NoCredentialsError":
                    print(_yellow("  No executor API key configured. Run: ")
                          + _bold("hive init"))
                else:
                    print(_yellow(f"  Hive turn failed: {error_class}"))
                error_code = 1
                break
    finally:
        await stream.aclose()
    return error_code if error_code is not None else (0 if saw_terminal_event else 1)


async def _ask(message: str, *, session_id: str = "cli:oneshot") -> int:
    from hive.runtime import HiveOS

    hive = HiveOS.build(validate_inbound_channels=False)
    try:
        return await _terminal_turn(hive, message, session_id=session_id)
    finally:
        await hive.aclose()


def _serve() -> int:
    import uvicorn

    from hive.gateway.app import create_app
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    uvicorn.run(
        create_app(hive, close_runtime_on_shutdown=True),
        host=hive.config.host,
        port=hive.config.port,
    )
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


def _status(*, live: bool = False, gateway: bool = False) -> int:
    from hive.core.config import HiveConfig
    cfg = HiveConfig.from_env()

    ok = True
    issues = cfg.validate()

    print(_bold("\n  HiveOS Status\n"))
    print(f"  exec_provider : {cfg.exec_provider}")
    print(f"  exec_model    : {cfg.exec_model}")
    print(f"  host:port     : {cfg.host}:{cfg.port}")
    print(f"  state_db      : {cfg.state_db} " + ("(exists)" if cfg.state_db.exists() else "(missing)"))
    print(f"  mnemosyne     : {cfg.mnemosyne_home} " + ("(exists)" if cfg.mnemosyne_home.exists() else "(not created)"))
    print(f"  learning_loop : {'enabled' if cfg.learning_loop_enabled else 'disabled'}")
    dead_tasks: int | str = 0
    if cfg.state_db.exists():
        try:
            uri = f"{cfg.state_db.resolve().as_uri()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM hive_tasks WHERE state='dead'"
                ).fetchone()
                dead_tasks = int(row[0]) if row else 0
            finally:
                conn.close()
        except sqlite3.Error:
            dead_tasks = "unavailable"
    print(f"  task_dead     : {dead_tasks}")
    if live and gateway:
        payload = _execution_gateway_get("/execution/status")
        executions = payload.get("executions") if isinstance(payload, dict) else None
        if isinstance(executions, dict):
            print("  executions   : " + ", ".join(
                f"{state}={int(executions.get(state, 0))}" for state in ("running", "ok", "error", "cancelled")
            ))
        else:
            ok = False
    elif live and cfg.state_db.exists():
        ledger = _open_run_ledger()
        try:
            runs = ledger.recent(limit=200)
        finally:
            ledger.close()
        counts = {state: 0 for state in ("running", "ok", "error", "cancelled")}
        for run in runs:
            state = str(run.get("state") or "")
            if state in counts:
                counts[state] += 1
        print("  executions   : " + ", ".join(
            f"{state}={counts[state]}" for state in ("running", "ok", "error", "cancelled")
        ))

    if issues:
        ok = False
        print(_yellow("\n  Config warnings:"))
        for issue in issues:
            print(_yellow(f"    • {issue}"))
    else:
        print(_green("\n  Config: OK"))

    return 0 if ok else 1


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
    print(f"  max regression: {cfg.learning_regression_threshold:.3f}")
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
# Durable run inspection — no model or gateway startup required
# ---------------------------------------------------------------------------

def _open_run_ledger():
    from hive.core.config import HiveConfig
    from hive.observability.runs import RunLedger

    return RunLedger(HiveConfig.from_env().state_db)


def _format_run_time(value: object) -> str:
    import datetime

    try:
        return datetime.datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "unknown"


def _runs(limit: int = 20) -> int:
    ledger = _open_run_ledger()
    try:
        rows = ledger.recent(limit=limit)
    finally:
        ledger.close()
    print(_bold("\n  HiveOS Runs\n"))
    if not rows:
        print(_dim("  (no recorded runs)"))
        return 0
    for row in rows:
        ended = row.get("ended_ts")
        elapsed = "running"
        if ended is not None:
            elapsed = f"{max(0.0, float(ended) - float(row['started_ts'])):.2f}s"
        print(
            f"  {str(row['state']).upper():<10} {str(row['run_id'])}  "
            f"{row['kind']:<12} {elapsed:<9} "
            f"{_format_run_time(row['started_ts'])}"
        )
    return 0


def _trace(run_id: str, limit: int = 200) -> int:
    ledger = _open_run_ledger()
    try:
        snapshot = ledger.snapshot(run_id)
        events = ledger.public_events(run_id, limit=limit) if snapshot is not None else []
    finally:
        ledger.close()
    if snapshot is None:
        print(_yellow(f"  Run not found: {run_id}"))
        return 1
    print(_bold(f"\n  HiveOS Run {run_id}\n"))
    print(f"  state={snapshot['state']}  phase={snapshot['phase']}  kind={snapshot['kind']}")
    if not events:
        print(_dim("\n  (no public lifecycle events)"))
        return 0
    print()
    for event in events:
        print(f"  {_format_run_time(event['ts'])}  {event['type']:<22} {event['data']}")
    return 0


def _report(run_id: str) -> int:
    ledger = _open_run_ledger()
    try:
        snapshot = ledger.snapshot(run_id)
        events = ledger.public_events(run_id) if snapshot is not None else []
    finally:
        ledger.close()
    if snapshot is None:
        print(_yellow(f"  Run not found: {run_id}"))
        return 1
    counts: dict[str, int] = {}
    for event in events:
        counts[event["type"]] = counts.get(event["type"], 0) + 1
    print(_bold(f"\n  HiveOS Run Report {run_id}\n"))
    print(f"  state       : {snapshot['state']}")
    print(f"  phase       : {snapshot['phase']}")
    print(f"  kind        : {snapshot['kind']}")
    print(f"  events      : {len(events)}")
    print(f"  duration    : {float(snapshot['duration_ms']) / 1000:.2f}s")
    specialists = snapshot["specialists"]
    if specialists["total"]:
        print("  specialists : " + ", ".join(
            f"{state}={specialists[state]}"
            for state in ("queued", "running", "review_required", "completed", "failed", "cancelled", "interrupted")
            if specialists[state]
        ))
    if counts:
        print("  lifecycle   : " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    return 0


def _run_show(run_id: str) -> int:
    """Show a redacted, computed execution snapshot for one durable run."""
    from hive.autonomy.tasks import TaskBoard
    from hive.core.config import HiveConfig

    cfg = HiveConfig.from_env()
    ledger = _open_run_ledger()
    try:
        snapshot = ledger.snapshot(run_id)
    finally:
        ledger.close()
    if snapshot is None:
        print(_yellow(f"  Run not found: {run_id}"))
        return 1
    print(_bold(f"\n  HiveOS Run: {run_id}\n"))
    print(f"  state       : {snapshot['state']}")
    print(f"  phase       : {snapshot['phase']}")
    print(f"  kind        : {snapshot['kind']}")
    print(f"  parent      : {snapshot['parent_run_id'] or '-'}")
    print(f"  started     : {_format_run_time(snapshot['started_ts'])}")
    if snapshot["ended_ts"] is not None:
        print(f"  ended       : {_format_run_time(snapshot['ended_ts'])}")
    if snapshot["interrupted_local"]:
        print(_yellow("  interrupted : local process ended before completion"))
    if snapshot["active_tool"]:
        print(f"  active tool : {snapshot['active_tool']}")
    children = snapshot["child_runs"]
    if children["total"]:
        print("  child runs  : " + ", ".join(
            f"{state}={children[state]}" for state in ("running", "ok", "error", "cancelled")
        ))
    specialists = snapshot["specialists"]
    if specialists["total"]:
        print("  specialists : " + ", ".join(
            f"{state}={specialists[state]}"
            for state in ("queued", "running", "review_required", "completed", "failed", "cancelled", "interrupted")
            if specialists[state]
        ))
    for specialist in specialists["active"]:
        print(f"    active specialist: {specialist['role']} {specialist['status']} "
              f"attempt={specialist['attempt']}")
    if not cfg.state_db.exists():
        return 0
    board = TaskBoard(cfg.state_db)
    try:
        tasks = board.search(run_id=run_id, limit=100)
    finally:
        board.close()
    if tasks:
        print("\n  Correlated tasks:")
        for task in tasks:
            print(f"    [{task.id}] {task.state:<18} {task.kind} "
                  f"attempt={task.attempts}/{task.max_attempts}")
    return 0


def _runs_tree(run_id: str) -> int:
    """Render a bounded, safe recursive child-run tree."""
    ledger = _open_run_ledger()
    try:
        tree = ledger.tree(run_id)
    finally:
        ledger.close()
    if tree is None:
        print(_yellow(f"  Run not found: {run_id}"))
        return 1

    def render(node: dict, prefix: str = "") -> None:
        print(f"  {prefix}{node['state']:<10} {node['run_id']}  {node['kind']}  phase={node['phase']}")
        for child in node["children"]:
            render(child, prefix + "  ")

    print(_bold(f"\n  HiveOS Run Tree: {run_id}\n"))
    render(tree["root"])
    if tree["truncated"]:
        print(_yellow(f"\n  Tree truncated at {tree['node_count']} safe node(s)."))
    return 0


def _execution_gateway_get(path: str) -> dict | None:
    """Read one execution projection from the local authenticated gateway."""
    from hive.core.config import HiveConfig

    cfg = HiveConfig.from_env()
    credential = str(cfg.secret or "")
    if not credential.strip():
        print(_yellow("  Refused: HIVE_SECRET is empty."))
        return None
    return _gateway_request(cfg, "GET", path, credential=credential)


def _run_show_gateway(run_id: str) -> int:
    payload = _execution_gateway_get(f"/runs/{urllib.parse.quote(run_id, safe='')}")
    if payload is None:
        return 1
    print(_bold(f"\n  HiveOS Run (gateway): {run_id}\n"))
    for label, key in (("state", "state"), ("phase", "phase"), ("kind", "kind"), ("parent", "parent_run_id")):
        print(f"  {label:<12}: {payload.get(key) or '-'}")
    if payload.get("active_tool"):
        print(f"  active tool : {payload['active_tool']}")
    if payload.get("interrupted_local"):
        print(_yellow("  interrupted : local process ended before completion"))
    children = payload.get("child_runs")
    if isinstance(children, dict) and children.get("total"):
        print("  child runs  : " + ", ".join(
            f"{state}={int(children.get(state, 0))}" for state in ("running", "ok", "error", "cancelled")
        ))
    specialists = payload.get("specialists")
    if isinstance(specialists, dict) and specialists.get("total"):
        states = ("queued", "running", "review_required", "completed", "failed", "cancelled", "interrupted")
        print("  specialists : " + ", ".join(
            f"{state}={int(specialists.get(state, 0))}" for state in states if specialists.get(state)
        ))
        active = specialists.get("active")
        if isinstance(active, list):
            for specialist in active[:100]:
                if not isinstance(specialist, dict):
                    continue
                print(f"    active specialist: {str(specialist.get('role') or 'specialist')[:64]} "
                      f"{str(specialist.get('status') or 'failed')[:32]} "
                      f"attempt={max(0, min(int(specialist.get('attempt', 0) or 0), 1000))}")
    return 0


def _runs_tree_gateway(run_id: str) -> int:
    payload = _execution_gateway_get(f"/runs/{urllib.parse.quote(run_id, safe='')}/tree")
    root = payload.get("root") if isinstance(payload, dict) else None
    if not isinstance(root, dict):
        return 1

    def render(node: dict, prefix: str = "") -> None:
        print(f"  {prefix}{node.get('state', '?'):<10} {node.get('run_id', '?')}  "
              f"{node.get('kind', '?')}  phase={node.get('phase', '?')}")
        for child in node.get("children", []):
            if isinstance(child, dict):
                render(child, prefix + "  ")

    print(_bold(f"\n  HiveOS Run Tree (gateway): {run_id}\n"))
    render(root)
    if payload.get("truncated"):
        print(_yellow(f"\n  Tree truncated at {int(payload.get('node_count', 0))} safe node(s)."))
    return 0


def _watch_gateway(run_id: str, limit: int = 500, *, follow: bool = False) -> int:
    """Replay the authenticated public execution stream without local DB access."""
    safe_run_id = urllib.parse.quote(run_id, safe="")
    last_event_id = 0
    try:
        while True:
            payload = _execution_gateway_get(
                f"/runs/{safe_run_id}/events?after_id={last_event_id}&limit={max(1, min(limit, 500))}"
            )
            if payload is None:
                return 1
            events = payload.get("events")
            if not isinstance(events, list):
                return 1
            for event in events:
                if not isinstance(event, dict):
                    continue
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                event_type = str(event.get("type", "status"))
                name = str(data.get("name") or data.get("agent") or "")
                status = str(data.get("status") or "")
                summary = str(data.get("summary") or "")
                suffix = f" {name}" if name else ""
                if status:
                    suffix += f" {status}"
                if summary:
                    suffix += f" — {summary}"
                print(f"  #{data.get('sequence', '?')} {event_type}{suffix}")
            last_event_id = int(payload.get("next_after_id", last_event_id) or last_event_id)
            snapshot = _execution_gateway_get(f"/runs/{safe_run_id}")
            if snapshot is None:
                return 1
            if not follow or str(snapshot.get("state")) in {"ok", "error", "cancelled"}:
                return 0
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(_dim("\n  watch stopped"))
        return 130


def _runs_recover() -> int:
    """Mark only locally-owned, no-longer-live runs as interrupted."""
    from hive.core.config import HiveConfig

    cfg = HiveConfig.from_env()
    if not cfg.state_db.exists():
        print(_dim("  (no run database yet)"))
        return 0
    ledger = _open_run_ledger()
    try:
        recovered = ledger.recover_interrupted()
    finally:
        ledger.close()
    print(_green(f"  Recovered {recovered} interrupted local run(s)."))
    return 0


def _eval(argv: list[str]) -> int:
    """Run the existing evaluation harness under the primary ``hive`` CLI."""
    from hive.evals.cli import main as eval_main

    return eval_main(argv)


def _tasks(limit: int = 20, state: str | None = None) -> int:
    """Inspect durable autonomous work without starting the full runtime."""
    from hive.core.config import HiveConfig
    from hive.core.redact import redact_known_secrets

    db_path = HiveConfig.from_env().state_db
    if not db_path.exists():
        print(_dim("  (no task database yet)"))
        return 0
    clauses = []
    params: list[object] = []
    if state:
        clauses.append("state=?")
        params.append(state)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            rows = conn.execute(
                "SELECT id, kind, state, source, attempts, max_attempts, run_id, last_error, updated_ts "
                f"FROM hive_tasks{where} ORDER BY id DESC LIMIT ?",
                tuple(params + [max(1, min(int(limit), 200))]),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(_yellow(f"  Could not read tasks: {type(exc).__name__}"))
        return 1
    print(_bold("\n  HiveOS Tasks\n"))
    if not rows:
        print(_dim("  (no matching tasks)"))
        return 0
    for task_id, kind, task_state, source, attempts, max_attempts, run_id, error, updated_ts in rows:
        detail = f" error={redact_known_secrets(str(error))[:120]}" if error else ""
        print(
            f"  [{task_id}] {str(task_state).upper():<18} {str(kind):<16} "
            f"attempt={attempts}/{max_attempts} run={str(run_id)[:8] or '-'} "
            f"source={source or '-'} {_format_run_time(updated_ts)}{detail}"
        )
    return 0


def _task_show(task_id: int) -> int:
    """Render one durable task with redacted failure context and payload."""
    from hive.autonomy.tasks import TaskBoard
    from hive.core.config import HiveConfig
    from hive.core.redact import redact_known_secrets

    cfg = HiveConfig.from_env()
    if not cfg.state_db.exists():
        print(_yellow("  Task database not found."))
        return 1
    board = TaskBoard(cfg.state_db)
    try:
        task = board.get(task_id)
    finally:
        board.close()
    if task is None:
        print(_yellow(f"  Task not found: {task_id}"))
        return 1
    print(_bold(f"\n  HiveOS Task {task.id}\n"))
    print(f"  state       : {task.state}")
    print(f"  kind        : {task.kind}")
    print(f"  source      : {task.source or '-'}")
    print(f"  run         : {task.run_id or '-'}")
    print(f"  attempts    : {task.attempts}/{task.max_attempts} (stalls={task.stall_count})")
    print(f"  updated     : {_format_run_time(task.updated_ts)}")
    if task.last_error:
        print(_yellow(f"  failure     : {redact_known_secrets(task.last_error)[:500]}"))
    payload = redact_known_secrets(json.dumps(task.payload, sort_keys=True, default=str))
    print(f"  payload     : {payload[:1000]}")
    return 0


def _record_task_operator_action(task, action: str, detail: str) -> None:
    """Append a public action marker only when a task has a correlated run."""
    if not task.run_id:
        return
    ledger = _open_run_ledger()
    try:
        ledger.record_operator_event({
            "type": "operator_action",
            "run_id": task.run_id,
            "name": f"task.{action}",
            "status": "completed",
            "summary": detail,
        })
    finally:
        ledger.close()


def _task_change(task_id: int, action: str) -> int:
    """Apply one intentionally narrow local queue transition.

    Cancellation is limited to work that has not started. Retry is limited to
    failed work that still has an attempt budget. Neither command can interrupt
    a running task or resurrect a dead-letter task.
    """
    from hive.autonomy.tasks import FAILED, PENDING, TaskBoard
    from hive.core.config import HiveConfig
    from hive.core.redact import redact_known_secrets

    cfg = HiveConfig.from_env()
    if not cfg.state_db.exists():
        print(_yellow("  Task database not found."))
        return 1
    board = TaskBoard(cfg.state_db)
    try:
        task = board.get(task_id)
        if task is None:
            print(_yellow(f"  Task not found: {task_id}"))
            return 1
        if action == "cancel":
            if task.state != PENDING:
                print(_yellow("  Refused: only pending tasks can be cancelled; running work is never interrupted."))
                return 2
            changed = board.cancel(task_id)
            detail = f"cancelled pending task {task_id}"
        elif action == "retry":
            if task.state != FAILED:
                print(_yellow("  Refused: only failed tasks can be retried."))
                return 2
            if task.attempts >= task.max_attempts:
                print(_yellow("  Refused: task has exhausted its retry budget and remains failed/dead-lettered."))
                return 2
            changed = board.retry(task_id)
            detail = f"requeued failed task {task_id}"
        else:  # pragma: no cover - internal call sites are fixed literals
            raise ValueError(f"unsupported task action: {action}")
    finally:
        board.close()
    if not changed:
        print(_yellow("  Task state changed concurrently; no action was applied."))
        return 2
    prior_error = redact_known_secrets(str(task.last_error or ""))[:300]
    _record_task_operator_action(task, action, detail)
    print(_green(f"  {detail.capitalize()}."))
    if action == "retry" and prior_error:
        print(_dim(f"  Recovery context retained until successful completion: {prior_error}"))
    return 0


# ---------------------------------------------------------------------------
# Durable conversation sessions
# ---------------------------------------------------------------------------

def _sessions(limit: int = 50) -> int:
    """List locally stored conversation metadata without starting a model."""
    from hive.context.session_store import SessionStore
    from hive.core.config import HiveConfig
    from hive.core.redact import redact_known_secrets

    db_path = HiveConfig.from_env().state_db
    if not db_path.exists():
        print(_dim("  (no session database yet)"))
        return 0
    store = SessionStore(db_path)
    try:
        rows = store.list_session_details(limit=limit)
    finally:
        store.close()
    print(_bold("\n  HiveOS Conversations\n"))
    if not rows:
        print(_dim("  (no conversations)"))
        return 0
    for row in rows:
        title = redact_known_secrets(str(row.get("title") or ""))[:56]
        title_part = f"  {title}" if title else ""
        print(
            f"  {row['id']:<36} messages={int(row['message_count']):<4} "
            f"links={int(row['link_count']):<3} state={row['status']:<8} "
            f"{_format_run_time(row['updated'])}{title_part}"
        )
    return 0


def _session_bind(surface: str, subject: str, session_id: str) -> int:
    """Bind an inbound channel subject to a named conversation.

    The raw subject is accepted only from the local operator command line and
    is immediately HMAC-derived before storage.  It is intentionally omitted
    from both normal output and error output.
    """
    from hive.context.session_store import SessionStore, opaque_subject_id
    from hive.core.config import HiveConfig

    cfg = HiveConfig.from_env()
    try:
        key = opaque_subject_id(surface, subject, cfg.secret)
        store = SessionStore(cfg.state_db)
        try:
            store.bind_link(surface, key, session_id)
        finally:
            store.close()
    except ValueError as exc:
        print(_yellow(f"  Could not bind session: {exc}"))
        return 2
    print(_green(f"  Linked {str(surface).strip().casefold()} to session {str(session_id).strip()}"))
    return 0


def _session_show(session_id: str, limit: int = 100) -> int:
    """Render a bounded, redacted local transcript without starting a model."""
    from hive.context.session_store import SessionStore
    from hive.core.config import HiveConfig
    from hive.core.redact import redact_known_secrets

    store = SessionStore(HiveConfig.from_env().state_db)
    try:
        rows = store.message_details(session_id, limit=limit)
    finally:
        store.close()
    if not rows:
        print(_dim("  (no conversation messages)"))
        return 0
    print(_bold(f"\n  HiveOS Conversation: {session_id}\n"))
    for row in rows:
        content = redact_known_secrets(str(row["content"])).replace("\n", " ")[:500]
        print(f"  {_format_run_time(row['ts'])}  {str(row['role']).upper():<9} {content}")
    return 0


def _session_links(session_id: str) -> int:
    """List safe link references; raw platform identifiers are never retained."""
    from hive.context.session_store import SessionStore
    from hive.core.config import HiveConfig

    store = SessionStore(HiveConfig.from_env().state_db)
    try:
        rows = store.link_details(session_id)
    finally:
        store.close()
    if not rows:
        print(_dim("  (no bound channels)"))
        return 0
    print(_bold(f"\n  HiveOS Channel Links: {session_id}\n"))
    for row in rows:
        print(f"  {row['surface']:<12} ref={row['ref']}  {_format_run_time(row['updated'])}")
    return 0


async def _memory_remember(content: str, *, topic: str = "", importance: float = 0.7) -> int:
    """Owner-only CLI path for a trusted durable memory write."""
    from hive.core.redact import contains_known_secret
    from hive.core.types import ContentTrust
    from hive.memory.provider import learn_with_provenance
    from hive.runtime import HiveOS

    if contains_known_secret(content):
        print(_yellow("  Refused: configured secret values cannot be stored as memory."))
        return 2
    hive = HiveOS.build(validate_inbound_channels=False)
    try:
        memory_id = learn_with_provenance(
            hive.memory, "owner-memory", topic or content[:60], content, "owner-cli",
            trust=ContentTrust.TRUSTED, importance=max(0.0, min(float(importance), 1.0)),
        )
    finally:
        await hive.aclose()
    print(_green(f"  Stored trusted memory {str(memory_id or 'stored')[:12]}"))
    return 0


async def _memory_search(query: str) -> int:
    from hive.core.redact import redact_known_secrets
    from hive.runtime import HiveOS

    hive = HiveOS.build(validate_inbound_channels=False)
    try:
        rows = hive.memory.recall(query, limit=20)
    finally:
        await hive.aclose()
    if not rows:
        print(_dim("  (no memory matches)"))
        return 0
    for row in rows:
        content = redact_known_secrets(str(row.get("content", ""))).replace("\n", " ")[:300]
        print(f"  [{row.get('trust', row.get('trust_tier', 'unknown'))}] "
              f"{row.get('topic', row.get('source', 'memory'))}: {content}")
    return 0


def _watch(run_id: str, limit: int = 500, *, follow: bool = False) -> int:
    """Replay safe operator events, optionally tailing an active local run."""
    from hive.core.config import HiveConfig
    from hive.observability.runs import RunLedger

    ledger = RunLedger(HiveConfig.from_env().state_db)
    last_event_id = 0
    try:
        while True:
            run = ledger.get(run_id)
            events = ledger.public_events(run_id, after_id=last_event_id, limit=limit)
            if run is None:
                print(_yellow("  Run not found."))
                return 1
            if last_event_id == 0:
                print(_bold(f"\n  HiveOS Watch: {run_id} ({run['state']})\n"))
            for event in events:
                event_id = int(event.get("id", 0))
                data = event["data"]
                event_type = str(event["type"])
                name = str(data.get("name") or data.get("agent") or "")
                status = str(data.get("status") or "")
                summary = str(data.get("summary") or "")
                duration = data.get("duration_ms")
                suffix = f" {name}" if name else ""
                if status:
                    suffix += f" {status}"
                if duration is not None:
                    suffix += f" ({duration} ms)"
                if summary:
                    suffix += f" — {summary}"
                print(f"  #{data.get('sequence', '?')} {event_type}{suffix}")
                last_event_id = event_id
            if not follow or str(run["state"]) in {"ok", "error", "cancelled"}:
                return 0
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(_dim("\n  watch stopped"))
        return 130
    finally:
        ledger.close()


def _session_args(args: list[str]) -> tuple[str | None, list[str]] | None:
    """Extract one ``--session ID`` option while preserving message words."""
    session_id: str | None = None
    remaining: list[str] = []
    index = 0
    while index < len(args):
        item = args[index]
        if item == "--session":
            if session_id is not None or index + 1 >= len(args):
                return None
            session_id = args[index + 1].strip()
            if not session_id or len(session_id) > 128:
                return None
            index += 2
            continue
        remaining.append(item)
        index += 1
    return session_id, remaining


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


def _gateway_url(cfg, path: str) -> str:
    """Build the configured gateway URL without a secret-bearing override."""
    host = str(cfg.host or "127.0.0.1").strip()
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{int(cfg.port)}{path}"


def _gateway_is_loopback(cfg) -> bool:
    """Return whether the configured gateway target is local to this terminal."""
    host = str(cfg.host or "").strip().strip("[]").casefold()
    return host in {"", "0.0.0.0", "::", "127.0.0.1", "::1", "localhost"}


def _gateway_request(cfg, method: str, path: str, *, credential: str,
                     body: dict | None = None, approver: bool = False) -> dict | None:
    """Make one bounded authenticated gateway call without printing its body on error."""
    if not _gateway_is_loopback(cfg):
        credential_name = "an approver credential" if approver else "a gateway credential"
        print(_yellow(
            f"  Refused: {credential_name} may only be sent to a local gateway. "
            "Run this command on the Hive host."
        ))
        return None
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        _gateway_url(cfg, path), data=data, method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Hive-Token": credential,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            raw = response.read(65_536)
    except urllib.error.HTTPError as exc:
        print(_yellow(f"  Gateway rejected the request (HTTP {exc.code})."))
        return None
    except urllib.error.URLError:
        print(_yellow("  Gateway is unavailable. Start it with: hive serve"))
        return None
    except OSError as exc:
        print(_yellow(f"  Gateway request failed: {type(exc).__name__}"))
        return None
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        print(_yellow("  Gateway returned an invalid response."))
        return None
    if not isinstance(decoded, dict):
        print(_yellow("  Gateway returned an unexpected response."))
        return None
    return decoded


def _approver_credential(cfg) -> tuple[str, str] | None:
    """Return the terminal decision credential under the gateway's exact policy."""
    key = str(cfg.approver_key or "")
    if key.strip():
        return key, "human:out_of_band"
    if cfg.autonomy_enabled:
        print(_yellow("  Refused: HIVE_AUTONOMY_ENABLED=true requires HIVE_APPROVER_KEY."))
        return None
    fallback = str(cfg.secret or "")
    if not fallback.strip():
        print(_yellow("  Refused: HIVE_APPROVER_KEY is unset and HIVE_SECRET is empty."))
        return None
    print(_yellow(
        "  Warning: HIVE_APPROVER_KEY is unset; supervised approval is temporarily "
        "falling back to HIVE_SECRET. Configure the separate key before enabling autonomy."
    ))
    return fallback, "human:supervised_fallback"


def _render_approvals(payload: dict) -> int:
    from hive.core.redact import redact_known_secrets

    pending = payload.get("pending")
    edits = payload.get("pending_edits", 0)
    print(_bold("\n  HiveOS Pending Approvals\n"))
    if not isinstance(pending, list) or not pending:
        print(_dim("  (no pending gated tool calls)"))
    else:
        print(_yellow(f"  Gated tool calls ({len(pending)}):"))
        for item in pending:
            if not isinstance(item, dict):
                continue
            summary = redact_known_secrets(str(item.get("reason", ""))).replace("\n", " ")[:160]
            approval_id = item.get("approval_id", item.get("id", "?"))
            print(f"    [{str(approval_id)[:8]}] "
                  f"{item.get('tool', '?')} — {summary}")
    if edits:
        print(_yellow(f"\n  Self-mod edits awaiting review: {edits}"))
    return 0


async def _approvals() -> int:
    """Read the active gateway queue rather than a new process-local gate."""
    from hive.core.config import HiveConfig

    cfg = HiveConfig.from_env()
    credential = str(cfg.secret or "")
    if not credential.strip():
        print(_yellow("  Refused: HIVE_SECRET is empty; cannot authenticate to the gateway."))
        return 2
    payload = _gateway_request(cfg, "GET", "/approvals", credential=credential)
    return _render_approvals(payload) if payload is not None else 1


def _approvals_decide(approval_id: str, approved: bool) -> int:
    """Resolve one approval through the gateway's out-of-band credential boundary."""
    from hive.core.config import HiveConfig
    from hive.core.redact import redact_known_secrets

    normalized_id = str(approval_id).strip()
    if not normalized_id or len(normalized_id) > 256:
        print(_yellow("  Refused: approval id is invalid."))
        return 2
    cfg = HiveConfig.from_env()
    credential_info = _approver_credential(cfg)
    if credential_info is None:
        return 2
    credential, principal = credential_info
    payload = _gateway_request(
        cfg, "POST", "/approvals/decide", credential=credential,
        body={"approval_id": normalized_id, "approved": approved}, approver=True,
    )
    if payload is None:
        return 1
    status = redact_known_secrets(str(payload.get("status", "completed")))[:160]
    executed = bool(payload.get("executed", False))
    decision = "approved" if approved else "rejected"
    print(_green(f"  Approval {normalized_id[:12]} {decision} via {principal}: "
                 f"status={status}, executed={executed}."))
    return 0


def _render_incidents(payload: dict, *, detail: bool = False) -> int:
    incidents = payload.get("incidents")
    if detail:
        incidents = [payload]
    print(_bold("\n  HiveOS Incidents\n"))
    if not isinstance(incidents, list) or not incidents:
        print(_dim("  (no incidents)"))
        return 0
    for item in incidents:
        if not isinstance(item, dict):
            continue
        print(f"  [{str(item.get('incident_id', '?'))[:12]}] {item.get('severity', 'error'):<8} "
              f"{item.get('status', '?'):<10} {str(item.get('summary', ''))[:140]}")
        if detail:
            print(_dim(f"    source={item.get('source', '?')} run={item.get('run_id') or '-'} "
                       f"task={item.get('task_id') or '-'} recoveries={item.get('recovery_count', 0)}"))
            for event in item.get("events", [])[:20]:
                if isinstance(event, dict):
                    print(_dim(f"    - {event.get('type', '?')}"))
    return 0


def _incidents(incident_id: str | None = None) -> int:
    from hive.core.config import HiveConfig

    cfg = HiveConfig.from_env()
    credential = str(cfg.secret or "")
    if not credential.strip():
        print(_yellow("  Refused: HIVE_SECRET is empty; cannot authenticate to the gateway."))
        return 2
    path = f"/incidents/{incident_id}" if incident_id else "/incidents"
    payload = _gateway_request(cfg, "GET", path, credential=credential)
    return _render_incidents(payload, detail=bool(incident_id)) if payload is not None else 1


def _incident_mutate(incident_id: str, action: str) -> int:
    from hive.core.config import HiveConfig

    normalized = str(incident_id).strip()
    if not normalized or len(normalized) > 128:
        print(_yellow("  Refused: incident id is invalid."))
        return 2
    cfg = HiveConfig.from_env()
    credential_info = _approver_credential(cfg)
    if credential_info is None:
        return 2
    credential, principal = credential_info
    payload = _gateway_request(
        cfg, "POST", f"/incidents/{normalized}/{action}", credential=credential,
        body={}, approver=True,
    )
    if payload is None:
        return 1
    if action in {"recover", "diagnose"} and payload.get("finalized") is False:
        print(_yellow(
            f"  Incident {normalized[:12]} {action} was superseded by another operator action; inspect its status."
        ))
        return 1
    print(_green(f"  Incident {normalized[:12]} {action} via {principal}."))
    return 0


def _incident_links(incident_id: str) -> int:
    from hive.core.config import HiveConfig

    cfg = HiveConfig.from_env()
    credential = str(cfg.secret or "")
    if not credential.strip():
        print(_yellow("  Refused: HIVE_SECRET is empty; cannot authenticate to the gateway."))
        return 2
    payload = _gateway_request(cfg, "GET", f"/incidents/{str(incident_id).strip()}/links", credential=credential)
    if payload is None:
        return 1
    print(_bold(f"\n  Incident links: {str(payload.get('incident_id', '?'))[:12]}\n"))
    for link in payload.get("links", []):
        if isinstance(link, dict):
            print(_dim("  " + " ".join(f"{key}={value}" for key, value in link.items())))
    for observation in payload.get("pr_observations", []):
        if isinstance(observation, dict):
            print(_dim(f"  PR #{observation.get('pr_number', '?')} status={observation.get('status', '?')} "
                       f"review={observation.get('review_state', '?')}"))
    return 0


def _render_goals(payload: dict, *, detail: bool = False) -> int:
    goals = payload.get("goals")
    if detail:
        goals = [payload]
    print(_bold("\n  HiveOS Operator Goals\n"))
    if not isinstance(goals, list) or not goals:
        print(_dim("  (no durable operator goals)"))
        return 0
    for item in goals:
        if not isinstance(item, dict):
            continue
        print(f"  [{str(item.get('goal_id', '?'))[:12]}] {item.get('status', '?'):<11} "
              f"replans={item.get('replan_count', 0)}/{item.get('max_replans', 2)} "
              f"{str(item.get('summary', ''))[:120]}")
        if detail:
            tasks = ",".join(str(value) for value in item.get("task_ids", [])[:20]) or "-"
            print(_dim(f"    generation={item.get('plan_generation', 0)} tasks={tasks}"))
            reason = str(item.get("last_reason", ""))[:160]
            if reason:
                print(_dim(f"    reason={reason}"))
    return 0


def _goals(goal_id: str | None = None) -> int:
    from hive.core.config import HiveConfig

    cfg = HiveConfig.from_env()
    credential = str(cfg.secret or "")
    if not credential.strip():
        print(_yellow("  Refused: HIVE_SECRET is empty; cannot authenticate to the gateway."))
        return 2
    path = f"/goals/{goal_id}" if goal_id else "/goals"
    payload = _gateway_request(cfg, "GET", path, credential=credential)
    return _render_goals(payload, detail=bool(goal_id)) if payload is not None else 1


def _render_delegations(payload: dict, *, detail: bool = False, tree: bool = False) -> int:
    if tree:
        root = payload.get("root") if isinstance(payload, dict) else None
        if not isinstance(root, dict):
            print(_dim("  (no durable delegation tree)"))
            return 0

        def render(node: dict, prefix: str = "") -> None:
            print(f"  {prefix}[{str(node.get('id', '?'))[:12]}] {node.get('role', '?'):<18} "
                  f"{node.get('state', '?'):<16} depth={node.get('depth', 0)} "
                  f"attempts={node.get('attempts', 0)}/{node.get('max_attempts', 0)}")
            children = node.get("children", [])
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        render(child, prefix + "  ")

        print(_bold("\n  HiveOS Delegation Tree\n"))
        render(root)
        if payload.get("truncated"):
            print(_dim("  (tree output bounded)"))
        return 0

    items = [payload] if detail else payload.get("delegations", [])
    print(_bold("\n  HiveOS Delegations\n"))
    if not isinstance(items, list) or not items:
        print(_dim("  (no durable delegations)"))
        return 0
    for item in items:
        if not isinstance(item, dict):
            continue
        print(f"  [{str(item.get('delegation_id', '?'))[:12]}] {str(item.get('role', '?')):<18} "
              f"{str(item.get('state', '?')):<16} depth={item.get('depth', 0)} "
              f"attempts={item.get('attempts', 0)}/{item.get('max_attempts', 0)} "
              f"children={item.get('children', 0)}")
    return 0


def _delegations(delegation_id: str | None = None, *, tree: bool = False) -> int:
    from hive.core.config import HiveConfig

    cfg = HiveConfig.from_env()
    credential = str(cfg.secret or "")
    if not credential.strip():
        print(_yellow("  Refused: HIVE_SECRET is empty; cannot authenticate to the gateway."))
        return 2
    if delegation_id:
        suffix = "/tree" if tree else ""
        path = f"/delegations/{delegation_id}{suffix}"
    else:
        path = "/delegations"
    payload = _gateway_request(cfg, "GET", path, credential=credential)
    return _render_delegations(payload, detail=bool(delegation_id and not tree), tree=tree) if payload is not None else 1


def _goal_create(summary: str) -> int:
    from hive.core.config import HiveConfig

    normalized = str(summary).strip()
    if not normalized or len(normalized) > 10_000:
        print(_yellow("  Refused: goal summary must be a non-empty bounded string."))
        return 2
    cfg = HiveConfig.from_env()
    credential_info = _approver_credential(cfg)
    if credential_info is None:
        return 2
    credential, principal = credential_info
    payload = _gateway_request(
        cfg, "POST", "/goals", credential=credential, body={"summary": normalized}, approver=True,
    )
    if payload is None:
        return 1
    print(_green(f"  Goal {str(payload.get('goal_id', '?'))[:12]} created via {principal}."))
    return _render_goals(payload, detail=True)


def _goal_mutate(goal_id: str, action: str) -> int:
    from hive.core.config import HiveConfig

    normalized = str(goal_id).strip()
    if not normalized or len(normalized) > 128:
        print(_yellow("  Refused: goal id is invalid."))
        return 2
    cfg = HiveConfig.from_env()
    credential_info = _approver_credential(cfg)
    if credential_info is None:
        return 2
    credential, principal = credential_info
    payload = _gateway_request(
        cfg, "POST", f"/goals/{normalized}/{action}", credential=credential, body={}, approver=True,
    )
    if payload is None:
        return 1
    print(_green(f"  Goal {normalized[:12]} {action}d via {principal}."))
    return _render_goals(payload, detail=True)


async def _selfmod_history(limit: int = 20) -> int:
    """List durable self-mod proposal outcomes without performing any mutation."""
    from hive.runtime import HiveOS

    hive = HiveOS.build()
    try:
        records = hive.self_mod_history(limit=max(1, min(limit, 100)))
    finally:
        await hive.aclose()
    print(_bold("\n  HiveOS Self-modification History\n"))
    if not records:
        print(_dim("  (no self-mod proposals yet)"))
        return 0
    for record in records:
        outcome = _green(str(record.get("outcome", "ok"))) if record.get("ok") else _yellow(
            str(record.get("outcome", "failed"))
        )
        print(f"  {outcome:<12} {record.get('tier', 'auto'):<6} "
              f"{record.get('branch') or '-':<28} {record.get('title', '')}")
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
    out.print("usage: hive [chat|init|ask|serve|heartbeat|consolidate|doctor|mcp-serve|version|status|logs|runs|trace|report|budget|approvals|incidents|learning|completion]",
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
        args=(("--session", str, "resume or create this named conversation"),),
        category="core",
    )
    _registry_mod.REGISTRY["ask"] = _registry_mod.CommandSpec(
        name="ask",
        help="one-shot turn",
        handler_name="_ask",
        args=(("--session", str, "use this named conversation"), ("MSG", str, "message")),
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
        help="config + environment health summary; use --live for execution totals",
        handler_name="_status",
        args=(("--live", None, "include durable execution totals"),),
        category="ops",
    )
    _registry_mod.REGISTRY["logs"] = _registry_mod.CommandSpec(
        name="logs",
        help="recent audit log entries",
        handler_name="_logs",
        args=(("--tail", _int_or(20), "lines to show"),),
        category="ops",
    )
    _registry_mod.REGISTRY["runs"] = _registry_mod.CommandSpec(
        name="runs",
        help="recent durable runs; use `runs show|tree ID` or `runs recover`",
        handler_name="_runs",
        args=(("--limit", _int_or(20), "max records to show"),),
        category="ops",
    )
    _registry_mod.REGISTRY["trace"] = _registry_mod.CommandSpec(
        name="trace",
        help="safe lifecycle timeline for one run",
        handler_name="_trace",
        args=(("RUN_ID", str, "run id"), ("--limit", _int_or(200), "max events to show")),
        category="ops",
    )
    _registry_mod.REGISTRY["report"] = _registry_mod.CommandSpec(
        name="report",
        help="compact safe evidence report for one run",
        handler_name="_report",
        args=(("RUN_ID", str, "run id"),),
        category="ops",
    )
    _registry_mod.REGISTRY["eval"] = _registry_mod.CommandSpec(
        name="eval",
        help="run or display Hive regression evaluations",
        handler_name="_eval",
        category="ops",
    )
    _registry_mod.REGISTRY["tasks"] = _registry_mod.CommandSpec(
        name="tasks",
        help="inspect tasks; use `tasks show|cancel|retry ID` for one task",
        handler_name="_tasks",
        args=(("--limit", _int_or(20), "max records to show"),
              ("--state", str, "filter by task state")),
        category="ops",
    )
    _registry_mod.REGISTRY["sessions"] = _registry_mod.CommandSpec(
        name="sessions",
        help="list conversations or bind an inbound channel to one",
        handler_name="_sessions",
        args=(("--limit", _int_or(50), "max conversations to show"),),
        category="ops",
    )
    _registry_mod.REGISTRY["watch"] = _registry_mod.CommandSpec(
        name="watch", help="replay or tail safe operator events for one run", handler_name="_watch",
        args=(("RUN_ID", str, "run id"),), category="ops",
    )
    _registry_mod.REGISTRY["memory"] = _registry_mod.CommandSpec(
        name="memory", help="owner memory write and search", handler_name="", category="ops",
    )
    _registry_mod.REGISTRY["budget"] = _registry_mod.CommandSpec(
        name="budget",
        help="budget forecast + warning status",
        handler_name="_budget",
        category="gateway",
    )
    _registry_mod.REGISTRY["approvals"] = _registry_mod.CommandSpec(
        name="approvals",
        help="active gateway queue; use `approvals decide ID approve|reject`",
        handler_name="_approvals",
        category="gateway",
    )
    _registry_mod.REGISTRY["incidents"] = _registry_mod.CommandSpec(
        name="incidents",
        help="redacted incident timeline; use `incidents show|links|diagnose|acknowledge|recover ID`",
        handler_name="_incidents",
        category="gateway",
    )
    _registry_mod.REGISTRY["goals"] = _registry_mod.CommandSpec(
        name="goals",
        help="durable operator goals; use `goals create|show|cancel|resume`",
        handler_name="_goals",
        category="gateway",
    )
    _registry_mod.REGISTRY["agents"] = _registry_mod.CommandSpec(
        name="agents",
        help="redacted durable delegation state; use `agents show|tree ID`",
        handler_name="_delegations",
        category="gateway",
    )
    _registry_mod.REGISTRY["selfmod-history"] = _registry_mod.CommandSpec(
        name="selfmod-history",
        help="durable self-mod proposal history",
        handler_name="_selfmod_history",
        args=(("--limit", _int_or(20), "max records to show"),),
        category="ops",
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

_USAGE = "usage: hive [chat|init|ask|serve|heartbeat|consolidate|doctor|mcp-serve|version|status|logs|runs|trace|watch|report|tasks|sessions|memory|eval|budget|approvals|incidents|selfmod-history|learning|completion]"


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

    if cmd == "status" and len(args_list) > 1:
        if len(args_list) in {2, 3} and args_list[1] == "--live" and set(args_list[2:]) <= {"--gateway"}:
            return _status(live=True, gateway="--gateway" in args_list)
        print("usage: hive status [--live [--gateway]]", file=sys.stderr)
        return 2

    if cmd in ("chat", "ask"):
        parsed_session = _session_args(args_list[1:])
        if parsed_session is None:
            print(f"usage: hive {cmd} [--session SESSION]" + (" \"<message>\"" if cmd == "ask" else ""),
                  file=sys.stderr)
            return 2
        session_id, remaining = parsed_session
        if cmd == "chat":
            if remaining:
                print("usage: hive chat [--session SESSION]", file=sys.stderr)
                return 2
            return _run_async(_chat(session_id=session_id))
        msg = " ".join(remaining).strip()
        if not msg:
            print("usage: hive ask \"<message>\"", file=sys.stderr)
            return 2
        return _run_async(_ask(msg, session_id=session_id or "cli:oneshot"))
    if cmd == "eval":
        return _eval(args_list[1:])
    if cmd == "sessions":
        if len(args_list) >= 2 and args_list[1] == "bind":
            if len(args_list) != 5:
                print("usage: hive sessions bind <surface> <subject> <session>", file=sys.stderr)
                return 2
            return _session_bind(args_list[2], args_list[3], args_list[4])
        if len(args_list) >= 3 and args_list[1] == "show":
            try:
                return _session_show(args_list[2], int(args_list[3]) if len(args_list) == 4 else 100)
            except ValueError:
                return 2
        if len(args_list) == 3 and args_list[1] == "links":
            return _session_links(args_list[2])
        if len(args_list) > 1 and args_list[1] != "--limit":
            print("usage: hive sessions [--limit N] | hive sessions bind <surface> <subject> <session> | hive sessions show <session> [limit] | hive sessions links <session>",
                   file=sys.stderr)
            return 2
    if cmd == "memory":
        if len(args_list) >= 3 and args_list[1] == "search":
            return _run_async(_memory_search(" ".join(args_list[2:])))
        if len(args_list) >= 3 and args_list[1] == "remember":
            return _run_async(_memory_remember(" ".join(args_list[2:])))
        print("usage: hive memory remember <text> | hive memory search <query>", file=sys.stderr)
        return 2
    if cmd == "approvals" and len(args_list) > 1:
        if len(args_list) == 4 and args_list[1] == "decide" and args_list[3] in {"approve", "reject"}:
            return _approvals_decide(args_list[2], args_list[3] == "approve")
        print("usage: hive approvals | hive approvals decide <approval-id> <approve|reject>",
              file=sys.stderr)
        return 2
    if cmd == "incidents" and len(args_list) > 1:
        if len(args_list) == 3 and args_list[1] == "show":
            return _incidents(args_list[2])
        if len(args_list) == 3 and args_list[1] == "links":
            return _incident_links(args_list[2])
        if len(args_list) == 3 and args_list[1] in {"acknowledge", "recover", "diagnose"}:
            return _incident_mutate(args_list[2], args_list[1])
        print("usage: hive incidents | hive incidents show|links|diagnose|acknowledge|recover <incident-id>",
              file=sys.stderr)
        return 2
    if cmd == "goals" and len(args_list) > 1:
        if len(args_list) == 3 and args_list[1] == "show":
            return _goals(args_list[2])
        if len(args_list) >= 3 and args_list[1] == "create":
            return _goal_create(" ".join(args_list[2:]))
        if len(args_list) == 3 and args_list[1] in {"cancel", "resume"}:
            return _goal_mutate(args_list[2], args_list[1])
        print("usage: hive goals | hive goals create <summary> | hive goals show|cancel|resume <goal-id>",
              file=sys.stderr)
        return 2
    if cmd == "agents" and len(args_list) > 1:
        if len(args_list) == 3 and args_list[1] == "show":
            return _delegations(args_list[2])
        if len(args_list) == 3 and args_list[1] == "tree":
            return _delegations(args_list[2], tree=True)
        print("usage: hive agents | hive agents show|tree <delegation-id>", file=sys.stderr)
        return 2
    if cmd == "tasks" and len(args_list) >= 2:
        if len(args_list) == 3 and args_list[1] == "show":
            try:
                return _task_show(int(args_list[2]))
            except ValueError:
                print("task id must be an integer", file=sys.stderr)
                return 2
        if len(args_list) == 3 and args_list[1] in {"cancel", "retry"}:
            try:
                return _task_change(int(args_list[2]), args_list[1])
            except ValueError:
                print("task id must be an integer", file=sys.stderr)
                return 2
        if args_list[1] in {"show", "cancel", "retry"}:
            print("usage: hive tasks show|cancel|retry <task-id>", file=sys.stderr)
            return 2
    if cmd == "runs" and len(args_list) >= 2:
        if len(args_list) == 3 and args_list[1] == "show":
            return _run_show(args_list[2])
        if len(args_list) == 4 and args_list[1] == "show" and args_list[3] == "--gateway":
            return _run_show_gateway(args_list[2])
        if len(args_list) == 3 and args_list[1] == "tree":
            return _runs_tree(args_list[2])
        if len(args_list) == 4 and args_list[1] == "tree" and args_list[3] == "--gateway":
            return _runs_tree_gateway(args_list[2])
        if len(args_list) == 2 and args_list[1] == "recover":
            return _runs_recover()
        if args_list[1] in {"show", "tree", "recover"}:
            print("usage: hive runs show|tree <run-id> [--gateway] | hive runs recover", file=sys.stderr)
            return 2
    if cmd == "watch" and len(args_list) in {2, 3, 4}:
        flags = set(args_list[2:])
        if len(flags) != len(args_list[2:]) or not flags <= {"--follow", "--gateway"}:
            print("usage: hive watch <run-id> [--follow] [--gateway]", file=sys.stderr)
            return 2
        if "--gateway" in flags:
            return _watch_gateway(args_list[1], follow="--follow" in flags)
        return _watch(args_list[1], follow="--follow" in flags)

    try:
        spec, parsed = _parser_mod.parse(args_list)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        # Learning subcommand errors return 1 (consistent with original _learning_dispatch)
        if cmd == "learning" and code == 2:
            return 1
        return code

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
    if cmd == "runs":
        return _runs(getattr(parsed, "limit", 20))
    if cmd == "trace":
        return _trace(getattr(parsed, "RUN_ID", ""), getattr(parsed, "limit", 200))
    if cmd == "watch":
        return _watch(getattr(parsed, "RUN_ID", ""))
    if cmd == "report":
        return _report(getattr(parsed, "RUN_ID", ""))
    if cmd == "tasks":
        return _tasks(getattr(parsed, "limit", 20), getattr(parsed, "state", None))
    if cmd == "incidents":
        return _incidents()
    if cmd == "sessions":
        return _sessions(getattr(parsed, "limit", 50) if len(args_list) > 1 else 50)
    if cmd == "selfmod-history":
        limit = getattr(parsed, "limit", 20)
        try:
            limit = int(limit)
        except (ValueError, TypeError):
            limit = 20
        return _run_async(_selfmod_history(limit=limit))
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
