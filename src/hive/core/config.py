"""
config.py — canonical HiveOS configuration.

Env-driven (model strings/endpoints stay out of code because MiniMax moves
M2 -> M3). Loads .env if python-dotenv is present. Reads SOUL via core.soul so the
PROTECTED file is referenced in place. Successor to the old Core/settings.py with
the casing bug fixed (lowercase package, but the SOUL path points at Config/).

A future `hive doctor --fix` (core.doctor) migrates older shapes; runtime reads
only this canonical shape (OpenClaw rule, Docs/references/OPENCLAW_REFERENCE.md §2).
"""
from __future__ import annotations

import os
from pathlib import Path

from hive.core.soul import REPO_ROOT, SOUL  # noqa: F401  (SOUL re-exported)

# Best-effort .env load (no hard dependency).
try:  # pragma: no cover - trivial
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

ROOT = REPO_ROOT

# --- MiniMax (primary executor) -------------------------------------------------
MINIMAX_ANTHROPIC_BASE = os.getenv("MINIMAX_ANTHROPIC_BASE", "https://api.minimax.io/anthropic")
MINIMAX_OPENAI_BASE = os.getenv("MINIMAX_OPENAI_BASE", "https://api.minimax.io/v1")
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")

EXEC_MODEL = os.getenv("HIVE_EXEC_MODEL", "MiniMax-M3")
EXEC_FALLBACK_MODEL = os.getenv("HIVE_EXEC_FALLBACK_MODEL", "MiniMax-M2.7")
AUX_MODEL = os.getenv("HIVE_AUX_MODEL", "MiniMax-M2.7")

# --- ChatGPT Plus planner (thinking only, via Codex OAuth) ----------------------
PLANNER_CMD = os.getenv("HIVE_PLANNER_CMD", "codex exec")
PLANNER_ENABLED = os.getenv("HIVE_PLANNER_ENABLED", "false").lower() == "true"

# --- Budgeter -------------------------------------------------------------------
REMAINS_URL = os.getenv("HIVE_REMAINS_URL", "https://api.minimax.io/v1/token_plan/remains")
DAILY_CALL_CAP = int(os.getenv("HIVE_DAILY_CALL_CAP", "3000"))
WINDOW_WARN_PCT = float(os.getenv("HIVE_WINDOW_WARN_PCT", "70"))

# --- Gateway --------------------------------------------------------------------
HIVE_HOST = os.getenv("HIVE_HOST", "0.0.0.0")
HIVE_PORT = int(os.getenv("HIVE_PORT", "8088"))
HIVE_SECRET = os.getenv("HIVE_SECRET", "change_me")

# --- Memory ---------------------------------------------------------------------
DATA_DIR = Path(os.getenv("HIVE_DATA_DIR", str(ROOT / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_DB = os.getenv("HIVE_STATE_DB", str(DATA_DIR / "hive.sqlite"))
MNEMOSYNE_MCP = os.getenv("MNEMOSYNE_MCP_URL", "")
OBSIDIAN_VAULT = os.getenv("OBSIDIAN_VAULT_PATH", str(ROOT / "vault"))

# --- Autonomy -------------------------------------------------------------------
HEARTBEAT_SEC = int(os.getenv("HIVE_HEARTBEAT_SEC", "900"))
MAX_CONCURRENT_AGENTS = int(os.getenv("HIVE_MAX_AGENTS", "3"))

# --- GitHub identity ------------------------------------------------------------
GITHUB_TOKEN = os.getenv("HIVE_GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("HIVE_GITHUB_REPO", "")
GITHUB_OWNER = os.getenv("HIVE_GITHUB_OWNER", "")
