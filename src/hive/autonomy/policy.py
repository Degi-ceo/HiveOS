"""Deterministic, evidence-only autonomy policy for self-development actions.

This module classifies actions; it never executes, approves, resolves, or retries
them. The protected Approval Gate and ToolExecutor remain final enforcement points.
"""
from __future__ import annotations

import enum
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from hive.core.approval import PROTECTED_PATHS
from hive.core.spec_search import EditOp, RiskTier, assign_tier

POLICY_VERSION = "1"


class PolicyAction(str, enum.Enum):
    DENY = "deny"
    AUTOMATIC = "automatic"
    NOTIFY_ONLY = "notify_only"
    OWNER_APPROVAL = "owner_approval"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    op: str
    action: PolicyAction
    reason: str
    policy_version: str = POLICY_VERSION


def _is_protected(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./").lower()
    return any(
        normalized == str(protected).replace("\\", "/").lstrip("./").lower()
        or normalized.endswith("/" + str(protected).replace("\\", "/").lstrip("./").lower())
        for protected in PROTECTED_PATHS
    )


def evaluate_edit(op: EditOp | str, *, target_files: tuple[str, ...] = ()) -> PolicyDecision:
    """Classify from immutable code rules; unknown input fails closed.

    Owner approvals and past successes are intentionally not accepted as inputs.
    They are audit evidence only and can never escalate a future action.
    """
    if any(_is_protected(path) for path in target_files):
        return PolicyDecision(str(getattr(op, "value", op)), PolicyAction.DENY,
                              "protected target is never policy-authorized")
    try:
        resolved = op if isinstance(op, EditOp) else EditOp(str(op))
    except ValueError:
        return PolicyDecision(str(op), PolicyAction.DENY, "unknown action defaults to deny")
    tier = assign_tier(resolved)
    if tier is RiskTier.AUTO:
        return PolicyDecision(resolved.value, PolicyAction.AUTOMATIC,
                              "deterministic AUTO tier; worktree, tests, and no-merge guard remain required")
    if tier is RiskTier.REVIEW:
        return PolicyDecision(resolved.value, PolicyAction.OWNER_APPROVAL,
                              "deterministic REVIEW tier requires a per-action owner approval")
    return PolicyDecision(resolved.value, PolicyAction.NOTIFY_ONLY,
                          "deterministic MANUAL tier is recorded only and never auto-executed")


def policy_catalog() -> dict[str, str]:
    """Safe static mapping used by authenticated owner-pull surfaces."""
    return {op.value: evaluate_edit(op).action.value for op in EditOp}


class AutonomyPolicyStore:
    """Append-only, non-authoritative policy-decision evidence."""

    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._clock = clock
        self._db.execute("""CREATE TABLE IF NOT EXISTS autonomy_policy_decisions(
            idempotency_key TEXT PRIMARY KEY, op TEXT NOT NULL, action TEXT NOT NULL,
            policy_version TEXT NOT NULL, created_ts REAL NOT NULL)""")
        self._db.commit()

    def record(self, idempotency_key: str, decision: PolicyDecision) -> bool:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        with self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO autonomy_policy_decisions VALUES(?,?,?,?,?)",
                (idempotency_key, decision.op, decision.action.value,
                 decision.policy_version, self._clock()),
            )
        return cur.rowcount == 1

    def summary(self) -> dict:
        counts = {action.value: 0 for action in PolicyAction}
        for row in self._db.execute(
            "SELECT action,COUNT(*) AS count FROM autonomy_policy_decisions GROUP BY action"
        ):
            action = str(row["action"])
            counts[action if action in counts else PolicyAction.DENY.value] += int(row["count"])
        return {
            "policy_version": POLICY_VERSION,
            "learning_mode": "evidence_only_never_escalates",
            "decision_counts": counts,
            "catalog": policy_catalog(),
        }
