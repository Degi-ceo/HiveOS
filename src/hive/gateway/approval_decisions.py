"""Shared, fail-closed execution path for durable approval decisions."""
from __future__ import annotations

from typing import Any

from hive.core.approval_enhancements import enhance
from hive.core.approval_store import PENDING
from hive.tools.executor import DispatchStatus


class ApprovalDecisionError(Exception):
    """Safe domain error exposed by authenticated approval surfaces."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def decide_approval(
    hive: Any, approval_id: str, *, approved: bool, decided_by: str,
) -> dict[str, Any]:
    """Record and execute an approval through the sole durable decision path."""
    stored = hive.approval_store.get(approval_id)
    if stored is not None:
        expired = set(enhance.sweep_expired())
        if approval_id in expired:
            hive.approval_store.expire(approval_id)
            hive.edit_pending.pop(approval_id, None)
            hive.task_board.cancel_approval(approval_id)
            raise ApprovalDecisionError(409, "approval expired; action was not executed")
        if enhance.is_killed():
            hive.approval_store.kill(approval_id)
            hive.edit_pending.pop(approval_id, None)
            hive.task_board.cancel_approval(approval_id)
            raise ApprovalDecisionError(409, "emergency stop active; action was not executed")
        if stored.state != PENDING:
            raise ApprovalDecisionError(409, "approval was already decided; action was not executed")
        if not hive.approval_store.decide(
            approval_id, approved=approved, decided_by=decided_by,
        ):
            raise ApprovalDecisionError(409, "approval decision race; action was not executed")

    item = enhance.resolve_with_history(approval_id, approved, decided_by=decided_by)
    if item is None:
        if stored is not None:
            raise ApprovalDecisionError(
                409, "decision recorded but live approval is unavailable; action was not executed",
            )
        raise ApprovalDecisionError(404, "unknown approval")
    def _record(state: str, *, branch: str | None = None, pr_url: str | None = None, lesson: str = "") -> None:
        store = getattr(hive, "selfdev_runs", None)
        if store is not None:
            try:
                store.record_evidence_for_approval(approval_id, state=state, branch=branch, pr_url=pr_url, lesson=lesson)
            except Exception:
                pass

    if not bool(item.get("approved", False)):
        hive.edit_pending.pop(approval_id, None)
        hive.task_board.cancel_approval(approval_id)
        return {"executed": False, "status": "rejected"}

    if stored is None:
        raise ApprovalDecisionError(
            409, "approval was not durably recorded; action was not executed",
        )
    if str(item.get("tool", "")).startswith("self_mod:"):
        edit = hive.edit_pending.pop(approval_id, None)
        if edit is None:
            return {"executed": False, "error": "edit not found (process may have restarted)"}
        outcome = await hive.improver.apply_approved(edit)
        return {
            "executed": True, "status": outcome.status,
            "branch": outcome.branch, "detail": outcome.detail,
        }

    if not hive.approval_store.begin_execution(approval_id):
        raise ApprovalDecisionError(
            409, "execution was already started; action was not executed",
        )
    dispatch = await hive.tool_executor.execute_approved(item["tool"], item["args"])
    confirmed = bool(
        dispatch.status is DispatchStatus.OK and dispatch.result and dispatch.result.success,
    )
    hive.approval_store.finish_execution(
        approval_id, succeeded=confirmed,
        error=dispatch.error or (
            None if confirmed else "approved tool returned no confirmed result"
        ),
    )
    if confirmed:
        hive.task_board.complete_approval(approval_id)
    else:
        hive.task_board.review_approval(
            approval_id, dispatch.error or "approved tool returned no confirmed result",
        )
    return {
        "executed": True, "status": dispatch.status.value,
        "result": dispatch.result.content if dispatch.result else None,
        "error": dispatch.error,
    }
