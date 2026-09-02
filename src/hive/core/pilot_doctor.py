"""Read-only readiness inspection for the manually operated Telegram pilot.

Unlike ``hive doctor``, this module never creates directories, opens a write
connection, runs migrations, starts a runtime, or contacts a provider.  It returns
only bounded booleans and aggregate state counts so an operator can decide whether
the pilot needs review without reading messages, tokens, identities, or errors.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from hive.core.config import HiveConfig
from hive.core.sqlite_ops import verify_database


def inspect(cfg: HiveConfig) -> dict[str, Any]:
    """Return a privacy-safe, side-effect-free pilot readiness report."""
    state_ok, _details = verify_database(cfg.state_db)
    reviews = _review_counts(cfg.state_db) if state_ok else {"memory": 0, "telegram": 0}
    telegram_configured = bool(
        cfg.telegram_token and cfg.telegram_webhook_secret and cfg.telegram_allowed_user_ids
    )
    autonomy_disabled = not cfg.autonomy_enabled and not cfg.autonomous_selfmod_enabled
    review_total = reviews["memory"] + reviews["telegram"]
    if not state_ok or not autonomy_disabled:
        status = "blocked"
    elif review_total:
        status = "requires_owner_review"
    elif not telegram_configured:
        status = "degraded"
    else:
        status = "ready"
    return {
        "status": status,
        "state_integrity_ok": state_ok,
        "telegram_ingress_configured": telegram_configured,
        "autonomy_disabled": autonomy_disabled,
        "reviews": {"memory": reviews["memory"], "telegram": reviews["telegram"], "total": review_total},
    }


def _review_counts(path: Path) -> dict[str, int]:
    """Read fixed state predicates using a read-only connection, or return zero for absent tables."""
    connection: sqlite3.Connection | None = None
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        memory = _count_if_present(
            connection, tables, "memory_projection_outbox", "state='requires_review'"
        )
        telegram = _count_if_present(
            connection, tables, "telegram_updates", "state='ambiguous'"
        )
        return {"memory": memory, "telegram": telegram}
    except sqlite3.Error:
        # ``verify_database`` already supplied the public integrity verdict.  Do
        # not expose a database exception that could carry a local path.
        return {"memory": 0, "telegram": 0}
    finally:
        if connection is not None:
            connection.close()


def _count_if_present(connection: sqlite3.Connection, tables: set[str], table: str, predicate: str) -> int:
    if table not in tables:
        return 0
    return int(connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {predicate}").fetchone()[0])
