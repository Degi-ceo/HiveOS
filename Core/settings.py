"""
settings.py — central HiveOS configuration, all from environment.

Model strings and endpoints are kept in env (NOT hardcoded) because MiniMax
is actively moving M2 -> M2.x -> M3. Migrating Hive is a one-line .env change.
"""
from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- MiniMax (primary executor) ---------------------------------------------
# Anthropic-compatible endpoint is recommended by MiniMax for interleaved thinking.
MINIMAX_ANTHROPIC_BASE = os.getenv("MINIMAX_ANTHROPIC_BASE", "https://api.minimax.io/anthropic")
MINIMAX_OPENAI_BASE = os.getenv("MINIMAX_OPENAI_BASE", "https://api.minimax.io/v1")
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")

# Pin model strings here via env. Defaults reflect current primary.
EXEC_MODEL = os.getenv("HIVE_EXEC_MODEL", "MiniMax-M3")
EXEC_FALLBACK_MODEL = os.getenv("HIVE_EXEC_FALLBACK_MODEL", "MiniMax-M2.7")
AUX_MODEL = os.getenv("HIVE_AUX_MODEL", "MiniMax-M2.7")  # memory-keeper, summaries

# --- ChatGPT Plus planner (thinking ONLY, via Codex OAuth) ------------------
# Heavy planning/architecture only. Never used for execution.
PLANNER_CMD = os.getenv("HIVE_PLANNER_CMD", "codex exec")  # headless Codex
PLANNER_ENABLED = os.getenv("HIVE_PLANNER_ENABLED", "false").lower() == "true"

# --- Budgeter ---------------------------------------------------------------
REMAINS_URL = os.getenv("HIVE_REMAINS_URL", "https://api.minimax.io/v1/token_plan/remains")
DAILY_CALL_CAP = int(os.getenv("HIVE_DAILY_CALL_CAP", "3000"))
WINDOW_WARN_PCT = float(os.getenv("HIVE_WINDOW_WARN_PCT", "70"))

# --- Gateway ---------------------------------------------------------------
HIVE_HOST = os.getenv("HIVE_HOST", "0.0.0.0")
HIVE_PORT = int(os.getenv("HIVE_PORT", "8088"))
HIVE_SECRET = os.getenv("HIVE_SECRET", "change_me")

# --- Memory ----------------------------------------------------------------
DATA_DIR = Path(os.getenv("HIVE_DATA_DIR", str(ROOT / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SQLITE_PATH = os.getenv("HIVE_SQLITE_PATH", str(DATA_DIR / "hiveos.db"))
MNEMOSYNE_MCP = os.getenv("MNEMOSYNE_MCP_URL", "")     # e.g. http://localhost:8080/sse
OBSIDIAN_REST = os.getenv("OBSIDIAN_REST_URL", "")     # e.g. https://127.0.0.1:27124
OBSIDIAN_TOKEN = os.getenv("OBSIDIAN_TOKEN", "")
OBSIDIAN_VAULT = os.getenv("OBSIDIAN_VAULT_PATH", str(ROOT / "vault"))

# --- Autonomy --------------------------------------------------------------
HEARTBEAT_SEC = int(os.getenv("HIVE_HEARTBEAT_SEC", "900"))   # 15 min
MAX_CONCURRENT_AGENTS = int(os.getenv("HIVE_MAX_AGENTS", "3"))

# --- GitHub identity (Hive's own account) ----------------------------------
GITHUB_TOKEN = os.getenv("HIVE_GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("HIVE_GITHUB_REPO", "")   # e.g. hive-bot/hiveos
GITHUB_OWNER = os.getenv("HIVE_GITHUB_OWNER", "")

SOUL = (ROOT / "config" / "SOUL.md").read_text(encoding="utf-8")
