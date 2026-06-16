"""
config.py — canonical HiveOS configuration as a typed, immutable object.

Built explicitly (no import-time side effects): importing this module reads no
env, writes no files, creates no dirs. `HiveConfig.from_env()` snapshots the
environment into a frozen dataclass; HiveOS.build() (P7) builds it once and
injects it. Tests construct alternate configs; `core.doctor` diffs/migrates shapes
against it (OpenClaw rule: runtime reads only the canonical shape —
docs/references/OPENCLAW_REFERENCE.md §2; rationale in ARCHITECTURE_REVIEW §F3).

Model strings/endpoints stay env-driven (MiniMax moves M2 -> M3). The PROTECTED
SOUL.md is referenced in place via core.soul (never relocated until P9).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from hive.core.soul import REPO_ROOT  # SOUL is loaded lazily by callers, not at config import


def _maybe_load_dotenv(root: Path) -> None:
    """Best-effort .env load; explicit, never at import."""
    try:
        from dotenv import load_dotenv

        load_dotenv(root / ".env")
    except Exception:  # noqa: BLE001 - dotenv optional
        pass


@dataclass(frozen=True, slots=True)
class HiveConfig:
    root: Path
    data_dir: Path
    state_db: Path
    # Executor provider selection (minimax | anthropic)
    exec_provider: str
    # MiniMax (primary executor)
    minimax_anthropic_base: str
    minimax_openai_base: str
    minimax_api_key: str
    # Anthropic (alternative executor)
    anthropic_base: str
    anthropic_api_key: str
    exec_model: str
    exec_fallback_model: str
    aux_model: str
    # ChatGPT Plus planner (thinking only, via Codex OAuth)
    planner_cmd: str
    planner_enabled: bool
    planner_timeout: float
    # Budgeter
    remains_url: str
    daily_call_cap: int
    window_warn_pct: float
    # Gateway
    host: str
    port: int
    secret: str
    # Memory
    mnemosyne_mcp_url: str
    mnemosyne_home: Path  # local SQLite path for the Mnemosyne provider
    obsidian_vault: Path
    # Autonomy
    heartbeat_sec: int
    max_concurrent_agents: int
    # Hive's own GitHub identity
    github_token: str
    github_repo: str
    github_owner: str
    # Telegram surface (optional)
    telegram_token: str
    telegram_webhook_secret: str
    # Self-mod sandbox (optional): docker image to run candidate tests in
    sandbox_image: str
    # MCP stdio servers to load at startup: ';'-separated command lines (A2)
    mcp_servers: tuple[str, ...]
    # Agent loop limits (configurable so Hive can handle long development tasks)
    max_iterations: int   # LLM-tool turns per conversation turn (HIVE_MAX_ITERATIONS)
    max_per_tool: int     # per-tool call budget within one turn (HIVE_MAX_PER_TOOL)
    # Self-improvement: min recent failures before triggering self-diagnose (HIVE_SELFMOD_THRESHOLD)
    selfmod_failure_threshold: int

    @classmethod
    def from_env(cls, root: Path | str | None = None, *, load_dotenv: bool = True) -> "HiveConfig":
        root = Path(root) if root else REPO_ROOT   # coerce: callers may pass a str path
        if load_dotenv:
            _maybe_load_dotenv(root)
        data_dir = Path(os.getenv("HIVE_DATA_DIR", str(root / "data")))
        return cls(
            root=root,
            data_dir=data_dir,
            state_db=Path(os.getenv("HIVE_STATE_DB", str(data_dir / "hive.sqlite"))),
            exec_provider=os.getenv("HIVE_EXEC_PROVIDER", "minimax"),
            minimax_anthropic_base=os.getenv("MINIMAX_ANTHROPIC_BASE", "https://api.minimax.io/anthropic"),
            minimax_openai_base=os.getenv("MINIMAX_OPENAI_BASE", "https://api.minimax.io/v1"),
            minimax_api_key=os.getenv("MINIMAX_API_KEY", ""),
            anthropic_base=os.getenv("ANTHROPIC_BASE", "https://api.anthropic.com"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            exec_model=os.getenv("HIVE_EXEC_MODEL", "MiniMax-M3"),
            exec_fallback_model=os.getenv("HIVE_EXEC_FALLBACK_MODEL", "MiniMax-M2.7"),
            aux_model=os.getenv("HIVE_AUX_MODEL", "MiniMax-M2.7"),
            planner_cmd=os.getenv("HIVE_PLANNER_CMD", "codex exec"),
            planner_enabled=os.getenv("HIVE_PLANNER_ENABLED", "false").lower() == "true",
            planner_timeout=float(os.getenv("HIVE_PLANNER_TIMEOUT", "120")),
            remains_url=os.getenv("HIVE_REMAINS_URL", "https://api.minimax.io/v1/token_plan/remains"),
            daily_call_cap=int(os.getenv("HIVE_DAILY_CALL_CAP", "3000")),
            window_warn_pct=float(os.getenv("HIVE_WINDOW_WARN_PCT", "70")),
            host=os.getenv("HIVE_HOST", "0.0.0.0"),
            port=int(os.getenv("HIVE_PORT", "8088")),
            secret=os.getenv("HIVE_SECRET", "change_me"),
            mnemosyne_mcp_url=os.getenv("MNEMOSYNE_MCP_URL", ""),
            mnemosyne_home=Path(os.getenv("MNEMOSYNE_HOME", str(data_dir / "mnemosyne"))),
            obsidian_vault=Path(os.getenv("OBSIDIAN_VAULT_PATH", str(root / "vault"))),
            heartbeat_sec=int(os.getenv("HIVE_HEARTBEAT_SEC", "900")),
            max_concurrent_agents=int(os.getenv("HIVE_MAX_AGENTS", "3")),
            github_token=os.getenv("HIVE_GITHUB_TOKEN", ""),
            github_repo=os.getenv("HIVE_GITHUB_REPO", ""),
            github_owner=os.getenv("HIVE_GITHUB_OWNER", ""),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET", ""),
            sandbox_image=os.getenv("HIVE_SANDBOX_IMAGE", ""),
            mcp_servers=tuple(s.strip() for s in os.getenv("HIVE_MCP_SERVERS", "").split(";")
                              if s.strip()),
            max_iterations=int(os.getenv("HIVE_MAX_ITERATIONS", "30")),
            max_per_tool=int(os.getenv("HIVE_MAX_PER_TOOL", "50")),
            selfmod_failure_threshold=int(os.getenv("HIVE_SELFMOD_THRESHOLD", "3")),
        )

    def validate(self) -> list[str]:
        """Return a list of validation warnings/errors. Empty means OK."""
        issues = []
        if not self.exec_model:
            issues.append("HIVE_EXEC_MODEL is empty")
        if self.secret == "change_me":
            issues.append("HIVE_SECRET is the default 'change_me' — change it for production")
        if self.port < 1 or self.port > 65535:
            issues.append(f"HIVE_PORT={self.port} is out of range")
        if self.daily_call_cap < 1:
            issues.append("HIVE_DAILY_CALL_CAP must be >= 1")
        return issues

    def ensure_dirs(self) -> None:
        """Create runtime dirs. Called explicitly by the builder/doctor, never at import."""
        self.data_dir.mkdir(parents=True, exist_ok=True)


_CONFIG: HiveConfig | None = None


def get_config() -> HiveConfig:
    """Process-wide config, built once on first access."""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = HiveConfig.from_env()
    return _CONFIG


def set_config(cfg: HiveConfig) -> None:
    """Inject an explicit config (HiveOS.build wiring; test isolation)."""
    global _CONFIG
    _CONFIG = cfg
