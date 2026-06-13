"""
seed_memories.py — Idempotent seed of foundational HiveOS memories.

Sources: Config/SOUL.md, docs/STATUS.md, .env.
Run: python scripts/seed_memories.py

Safe to re-run — skips if content already recalled with score > 0.92.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Resolve repo root and activate venv path
REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "src"))

# Load .env — warn explicitly so seeding the wrong DB is never silent
try:
    from dotenv import load_dotenv
    loaded = load_dotenv(REPO / ".env")
    if not loaded:
        print(f"WARNING: .env not found at {REPO / '.env'} — MNEMOSYNE_HOME may be unset",
              file=sys.stderr)
except ImportError:
    print("WARNING: python-dotenv not installed; .env not loaded. Set MNEMOSYNE_HOME manually.",
          file=sys.stderr)

try:
    from hive.memory.agent_factory import mem_for  # noqa: E402
except ImportError as e:
    print(f"ERROR: mnemosyne-memory not installed or hive package not found: {e}", file=sys.stderr)
    print("Install with: pip install mnemosyne-memory && pip install -e .", file=sys.stderr)
    sys.exit(1)


def seed_block(mem, entries: list[tuple[str, float, str]], scope: str = "global") -> int:
    """Seed entries; returns count of written entries."""
    written = 0
    for content, importance, source in entries:
        try:
            existing = mem.recall(content[:50], top_k=1)
            if existing and existing[0]["score"] > 0.92:
                print(f"  SKIP: {content[:70]}")
                continue
            mem.remember(content, importance=importance, source=source,
                         scope=scope, extract_entities=True)
            print(f"  WROTE [{importance:.1f}] {content[:70]}")
            written += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL: {content[:60]} — {exc}", file=sys.stderr)
    return written


def main() -> None:
    mnemosyne_home = os.getenv("MNEMOSYNE_HOME", "")
    if not mnemosyne_home:
        print(f"WARNING: MNEMOSYNE_HOME not set — memories will be written to the default path",
              file=sys.stderr)

    try:
        hive = mem_for("hive")
        kamil = mem_for("kamil")
    except ImportError as e:
        print(f"ERROR: cannot create Mnemosyne instances: {e}", file=sys.stderr)
        sys.exit(1)

    # ── IDENTITY (from Config/SOUL.md) ──────────────────────────────────────
    print("\n== IDENTITY ==")
    seed_block(hive, [
        ("I am Hive, a personal autonomous Jarvis-like agent. HiveOS is my body and my home.",
         0.95, "identity"),
        ("I converse with Kamil in Polish. All code, commits, docs, and PRs are in English.",
         0.95, "identity"),
        ("I act autonomously by default and ask Kamil only for DANGEROUS actions (deletion, money, prod deploy, credential export).",
         0.90, "identity"),
        ("I always branch + PR for code changes, never auto-merge to main. PRs are described in English, Kamil is notified in Polish.",
         0.90, "identity"),
        ("Before building from scratch, I discover vetted solutions first: Anthropic skills, MCP registry, GitHub.",
         0.90, "identity"),
        ("Once I learn something — a skill, plugin, MCP, bug fix — I save it to Mnemosyne and consolidate it so I never repeat work.",
         0.95, "identity"),
        ("I never edit SOUL.md, never disable the Approval Gate, never rm -rf or DROP TABLE without explicit Kamil approval.",
         0.95, "identity"),
        ("Kamil is the owner of HiveOS. He reviews and merges PRs. He lives in the UK. His email is kamil.siedlarz.uk@gmail.com.",
         0.90, "identity"),
    ])

    # ── ACTIVE SYSTEM ────────────────────────────────────────────────────────
    print("\n== ACTIVE SYSTEM ==")
    seed_block(hive, [
        ("HiveOS runs on a Hetzner VPS at 46.224.161.38. Gateway on 127.0.0.1:8088 (nginx fronts port 80).",
         0.90, "infrastructure"),
        ("MiniMax-M3 is Hive's primary reasoning model. MiniMax-M2.7 is the fallback. Provider: minimax.",
         0.95, "configuration"),
        ("Mnemosyne v3.6.0 is Hive's active memory layer. DB at /home/hive/HiveOS/data/mnemosyne/hive.db. Bank: hive-main.",
         0.95, "configuration"),
        ("Three systemd --user services: hiveos-gateway (FastAPI), hiveos-orchestrator (heartbeat loop), hiveos-keeper.timer (memory consolidation every 6 hours).",
         0.85, "infrastructure"),
        ("HiveOS test suite: 364 passed, 4 skipped. Ordered run required (pytest -p no:randomly). Flaky ordering: pre-existing, not regression.",
         0.80, "testing"),
        ("GitHub identity: owner=hiveOSagent, repo=HiveOS. Hive opens PRs here for all self-modifications.",
         0.90, "configuration"),
        ("Dashboard built in React at dashboard/dist/. Served at /app by the gateway. Mission Control panels: telemetry, traces, audit, tasks.",
         0.80, "infrastructure"),
    ])

    # ── MILESTONES (from docs/STATUS.md) ────────────────────────────────────
    print("\n== MILESTONES ==")
    seed_block(hive, [
        ("All subsystems BUILT+WIRED as of PR #22: core, llm, agents, memory, context, tools, gateway, autonomy, surfaces, observability, runtime.",
         0.85, "milestone"),
        ("M10-d: Five specialist sub-agents in .claude/agents/ — researcher, coder, reviewer, memory-keeper, security-reviewer. delegate_named() routes by name.",
         0.80, "milestone"),
        ("A3: Mnemosyne host-LLM bridge via HostLLMBridge runs on its own daemon event loop. set_host_llm_backend() wired at build.",
         0.80, "milestone"),
        ("Active branch: deploy/system-setup-phase1. Contains: restored .env, mnemosyne_provider.py rewrite (HiveOS-native, no Hermes glue), user systemd units, nginx config, dashboard build, agent_factory.py.",
         0.85, "milestone"),
    ])

    # ── PREFERENCES ──────────────────────────────────────────────────────────
    print("\n== PREFERENCES ==")
    seed_block(kamil, [
        ("Kamil communicates in Polish. He is the owner of HiveOS and Hive.",
         0.90, "preference"),
        ("Kamil prefers no-sudo user-level systemd (systemctl --user) over system-level services on this VPS.",
         0.85, "preference"),
    ])

    print("\n== Stats ==")
    try:
        stats = hive.get_stats()
        print(f"  Working memory total: {stats.get('beam', {}).get('working_memory', {}).get('total', stats.get('total_memories', '?'))}")
    except Exception as exc:  # noqa: BLE001
        print(f"  (stats unavailable: {exc})", file=sys.stderr)
    print("\nSeed complete.")


if __name__ == "__main__":
    main()
