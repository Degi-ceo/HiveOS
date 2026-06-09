"""
doctor.py — `hive doctor [--fix]` health checks + migration owner.

OpenClaw rule: runtime reads only the canonical config/schema shape; legacy shapes
are migrated HERE, never via runtime shims (docs/references/OPENCLAW_REFERENCE.md
§2). Each migration is a callable `(cfg, fix) -> (name, ok, detail)` — run in
order, idempotent, safe to re-run.
"""
from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path
from typing import Callable

from hive.core import config
from hive.core.soul import SOUL_PATH

# Non-critical checks: absence is a warning, not a doctor failure.
_WARN_ONLY = ("MINIMAX_API_KEY", "data dir", "mnemosyne")

Migration = Callable[[config.HiveConfig, bool], tuple[str, bool, str]]


# ---------------------------------------------------------------------------
# Migrations (append new ones here; keep old ones; they are idempotent)
# ---------------------------------------------------------------------------

def _m0_dirs(cfg: config.HiveConfig, fix: bool) -> tuple[str, bool, str]:
    """M0: ensure runtime dirs exist."""
    if fix:
        cfg.ensure_dirs()
    ok = cfg.data_dir.is_dir()
    return "data dir present", ok, str(cfg.data_dir)


def _m1_state_db_schema(cfg: config.HiveConfig, fix: bool) -> tuple[str, bool, str]:
    """M1: ensure state.sqlite has the canonical hive_sessions + hive_memories schema."""
    db_path = cfg.state_db
    if not db_path.parent.is_dir():
        return "state DB parent dir", False, str(db_path.parent)
    if not fix and not db_path.exists():
        return "state DB schema", True, "not yet created (run --fix)"
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS hive_sessions (
                id        TEXT NOT NULL,
                created   REAL NOT NULL DEFAULT (unixepoch()),
                updated   REAL NOT NULL DEFAULT (unixepoch()),
                state     TEXT NOT NULL DEFAULT 'active',
                sys_prompt TEXT
            );
            CREATE INDEX IF NOT EXISTS hive_sessions_state
                ON hive_sessions(state);
            CREATE TABLE IF NOT EXISTS hive_messages (
                rowid     INTEGER PRIMARY KEY,
                session   TEXT NOT NULL,
                role      TEXT NOT NULL,
                content   TEXT NOT NULL,
                ts        REAL NOT NULL DEFAULT (unixepoch()),
                FOREIGN KEY(session) REFERENCES hive_sessions(id)
            );
            CREATE INDEX IF NOT EXISTS hive_messages_session
                ON hive_messages(session, rowid);
            CREATE VIRTUAL TABLE IF NOT EXISTS hive_messages_fts
                USING fts5(content, content=hive_messages, content_rowid=rowid,
                           tokenize='porter ascii');
            CREATE TABLE IF NOT EXISTS hive_memories (
                id        INTEGER PRIMARY KEY,
                kind      TEXT NOT NULL,
                content   TEXT NOT NULL,
                source    TEXT,
                ts        REAL NOT NULL DEFAULT (unixepoch())
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS hive_memories_fts
                USING fts5(content, content=hive_memories, content_rowid=id,
                           tokenize='porter ascii');
        """)
        conn.commit()
        conn.close()
        return "state DB schema", True, str(db_path)
    except Exception as exc:  # noqa: BLE001
        return "state DB schema", False, str(exc)


def _m2_mnemosyne_home(cfg: config.HiveConfig, fix: bool) -> tuple[str, bool, str]:
    """M2: create MNEMOSYNE_HOME dir if Mnemosyne is configured."""
    home = cfg.mnemosyne_home
    if fix:
        home.mkdir(parents=True, exist_ok=True)
    ok = home.is_dir()
    return "mnemosyne home dir", ok, str(home)


_MIGRATIONS: list[Migration] = [_m0_dirs, _m1_state_db_schema, _m2_mnemosyne_home]


# ---------------------------------------------------------------------------
# Static checks (environment, protected files, importable core modules)
# ---------------------------------------------------------------------------

def _static_checks(cfg: config.HiveConfig) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    results.append(("SOUL.md present", SOUL_PATH.exists(), str(SOUL_PATH)))
    gate_path = cfg.root / "Core" / "approval_gate.py"
    results.append(("approval_gate present", gate_path.exists(), str(gate_path)))
    for mod in ("hive.core.registry", "hive.core.events", "hive.core.types",
                "hive.core.config", "hive.core.approval"):
        try:
            importlib.import_module(mod)
            results.append((f"import {mod}", True, "ok"))
        except Exception as exc:  # noqa: BLE001
            results.append((f"import {mod}", False, str(exc)))
    results.append(("MINIMAX_API_KEY set", bool(cfg.minimax_api_key), "env"))
    # Mnemosyne optional — warn only
    try:
        importlib.import_module("mnemosyne.core.beam")
        results.append(("mnemosyne package", True, "installed"))
    except ImportError:
        results.append(("mnemosyne package", False, "not installed (optional; pip install mnemosyne-memory)"))
    return results


def check(fix: bool = False) -> list[tuple[str, bool, str]]:
    """Return [(name, ok, detail)] diagnostics. Apply migrations when fix=True."""
    cfg = config.get_config()
    results = _static_checks(cfg)
    for migration in _MIGRATIONS:
        results.append(migration(cfg, fix))
    return results


def run(fix: bool = False) -> bool:
    """Print diagnostics; return True if all critical checks pass."""
    ok_all = True
    for name, ok, detail in check(fix=fix):
        mark = "✓" if ok else "✗"
        print(f"{mark} {name}: {detail}")
        if not ok and not any(w in name for w in _WARN_ONLY):
            ok_all = False
    return ok_all


def main() -> int:
    """`hive-doctor` console entry: honors --fix from argv."""
    import sys
    return 0 if run(fix="--fix" in sys.argv) else 1


if __name__ == "__main__":
    raise SystemExit(main())
