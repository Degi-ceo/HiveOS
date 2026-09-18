"""Safe projection of failed specialist delegations into operator incidents.

Delegation inputs and worker output are deliberately not durable, so an
incident is a replan handoff rather than permission to replay work. This core
adapter accepts only the closed lifecycle metadata exposed by DelegationLedger.
"""
from __future__ import annotations

from typing import Any, Protocol


class FailedDelegation(Protocol):
    """The intentionally narrow record shape needed for incident projection."""

    id: str
    parent_run_id: str
    child_run_id: str
    role: str
    state: str
    attempts: int
    max_attempts: int
    safe_summary: str


_INTERRUPTED_SUMMARY = "worker interrupted; recovery requires replanning"


def _failure_code(record: FailedDelegation) -> str:
    """Map stored safe text to a closed code; never promote free text to evidence."""
    return "interrupted" if record.safe_summary == _INTERRUPTED_SUMMARY else "execution_failed"


def record_failed_delegation(ledger: Any, record: FailedDelegation) -> dict[str, Any]:
    """Record one redacted, idempotent replan-required delegation incident."""
    code = _failure_code(record)
    role = str(record.role)
    return ledger.record(
        "delegation",
        f"specialist {role} {code}",
        run_id=str(record.parent_run_id),
        evidence={
            "delegation_id": str(record.id),
            "parent_run_id": str(record.parent_run_id),
            "child_run_id": str(record.child_run_id),
            "role": role,
            "attempt": int(record.attempts),
            "max_attempts": int(record.max_attempts),
            "state": "failed",
            "recovery": "replanning_required",
        },
        occurrence_key=f"delegation:{record.id}:attempt:{int(record.attempts)}:failed",
    )


__all__ = ["record_failed_delegation"]
