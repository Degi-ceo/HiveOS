"""
tracer.py — observation layer for the learning loop (SPRINT_6 P-F).

The Tracer records tool-call outcomes to ``learning_traces`` (SQLite) so the
learning loop has a structured failure history to learn from. It does NOT
re-implement audit logging — it piggybacks on ``core/audit.py`` events so
secrets are redacted once, at the source.

Design notes:
- ``Tracer`` is a thin class — all DB work delegates to ``storage.py``.
- ``record()`` is the hot path; it must never raise (audit emit must not
  fail user-visible tool calls). Errors are swallowed and logged.
- ``recent_failures()`` is the cold read path used by the loop + heartbeat.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from hive.core.learning import storage
from hive.core.types import TraceRow

log = logging.getLogger(__name__)


# Outcome tags — kept narrow so the schema tests stay deterministic.
OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
OUTCOME_DENIED = "denied"

_VALID_OUTCOMES = {OUTCOME_OK, OUTCOME_ERROR, OUTCOME_DENIED}


class Tracer:
    """Records tool-call outcomes to ``learning_traces``.

    Lifecycle: construct once with a DB path, share across the runtime. The
    Tracer is **safe to call from sync or async** code (the underlying
    ``storage.insert_trace`` opens a fresh connection per call).
    """

    def __init__(self, db_path: str | str) -> None:  # type: ignore[override]
        # The annotation above is intentional duplication — accepts both str
        # and ``pathlib.Path`` without forcing an import. Storage layer
        # already accepts Path.
        self._db_path = str(db_path)
        # Idempotent — cheap CREATE TABLE IF NOT EXISTS.
        storage.ensure_schema(self._db_path)

    # --- write path ---------------------------------------------------------

    def record(
        self,
        *,
        tool: str,
        outcome: str,
        session_id: str = "",
        args: dict[str, Any] | None = None,
        latency_ms: float = 0.0,
        error_class: str | None = None,
        error_message: str | None = None,
    ) -> int:
        """Persist a trace row. Never raises — errors are logged only.

        Returns the inserted row id, or 0 on failure.
        """
        if outcome not in _VALID_OUTCOMES:
            # Coerce unknown outcomes to "error" so callers don't have to
            # know the taxonomy. Stays defensive.
            outcome = OUTCOME_ERROR
        row = TraceRow(
            ts=time.time(),
            session_id=session_id or "unknown",
            tool=tool or "unknown",
            args=dict(args or {}),
            outcome=outcome,
            latency_ms=max(0.0, float(latency_ms)),
            error_class=error_class,
            error_message=error_message,
        )
        try:
            return storage.insert_trace(self._db_path, row)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("tracer.record failed for %s: %s", tool, exc)
            return 0

    # --- read path ----------------------------------------------------------

    def recent_failures(
        self,
        *,
        threshold: int = 1,
        window_minutes: float = 60.0,
    ) -> list[TraceRow]:
        """Return recent rows where ``outcome != 'ok'`` within the window.

        ``threshold`` is an upper bound on how many rows to scan; the
        caller decides what "enough failures" means. Default ``threshold=1``
        returns every failure in the window (heartbeat / loop can apply
        its own minimum-count check on the returned length).
        """
        if threshold <= 0:
            threshold = 1
        if window_minutes <= 0:
            window_minutes = 60.0
        since_ts = time.time() - (window_minutes * 60.0)
        rows = storage.query_traces(
            self._db_path,
            outcome=OUTCOME_ERROR,
            since_ts=since_ts,
            limit=threshold,
        )
        # Also include denied rows — they're failures from the user's POV.
        denied = storage.query_traces(
            self._db_path,
            outcome=OUTCOME_DENIED,
            since_ts=since_ts,
            limit=threshold,
        )
        merged = sorted(rows + denied, key=lambda r: r.ts, reverse=True)
        return merged[:threshold]

    def recent_traces(
        self,
        *,
        outcome: str | None = None,
        limit: int = 50,
    ) -> list[TraceRow]:
        """Convenience read: last N traces (any outcome by default)."""
        if limit <= 0:
            return []
        return storage.query_traces(
            self._db_path,
            outcome=outcome,
            limit=limit,
        )

    # --- introspection ------------------------------------------------------

    @property
    def db_path(self) -> str:
        """Exposed so the gateway endpoint and CLI can show where state lives."""
        return self._db_path

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"Tracer(db_path={self._db_path})"
