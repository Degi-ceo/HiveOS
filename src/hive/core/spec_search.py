"""
spec_search.py — risk-tiered self-improvement loop (M2 #si-1).

Adapts OpenJarvis's spec_search pattern (learning/spec_search/{models,plan/risk_tier,
orchestrator}.py) onto HiveOS's existing safety spine. The single most important
idea ported here: **the model cannot choose how risky its own change is.** A typed
edit is assigned a risk tier from a deterministic table; the tier — not the model —
decides the gate:

  AUTO   -> drive through SelfModifier.propose() (isolated worktree -> test -> PR);
            no human needed, but it still NEVER merges and NEVER touches PROTECTED files.
  REVIEW -> request human approval through the PROTECTED Core/approval_gate.py and stop;
            the edit is applied only after a human approves.
  MANUAL -> recorded for a human; never auto-applied.

Checkpoint/rollback is already provided by SelfModifier (snapshot last-good +
discard-worktree-on-failure), so no separate CheckpointStore is needed.

DAG: core leaf. Depends only on core.self_mod + core.approval (both core). The
diagnose step that proposes edits is INJECTED (a callable), so this module never
imports llm — same discipline as memory.keeper's injected Summarizer.
"""
from __future__ import annotations

import enum
import logging
import uuid
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable, Protocol

from hive.core import approval
from hive.core.self_mod import ApplyFn, SelfModifier

log = logging.getLogger("hive.spec_search")


class EditOp(enum.Enum):
    """Kinds of self-modification Hive can propose. Every op MUST appear in
    _TIER_TABLE — a missing entry is a programming error (asserted at import)."""
    # Model routing / config — reversible, low blast radius
    SET_MODEL_FOR_TASK = "set_model_for_task"
    SET_MODEL_PARAM = "set_model_param"
    EDIT_TOOL_DESCRIPTION = "edit_tool_description"
    REMOVE_TOOL = "remove_tool"
    ADD_TEST = "add_test"
    EDIT_DOCS = "edit_docs"
    # Safe file creation (new test/doc files only; never overwrites existing code)
    CREATE_FILE = "create_file"
    # Behaviour-changing — needs human eyes
    PATCH_SYSTEM_PROMPT = "patch_system_prompt"
    PATCH_CODE = "patch_code"
    ADD_TOOL = "add_tool"
    # High blast radius — always human-only
    DEPENDENCY_CHANGE = "dependency_change"
    INFRA_DEPLOY = "infra_deploy"


class RiskTier(enum.Enum):
    AUTO = "auto"
    REVIEW = "review"
    MANUAL = "manual"


# The canonical op -> tier mapping. The teacher/model cannot override this; the
# planner overwrites whatever tier an edit arrives with (OpenJarvis spec §4.1).
_TIER_TABLE: dict[EditOp, RiskTier] = {
    EditOp.SET_MODEL_FOR_TASK: RiskTier.AUTO,
    EditOp.SET_MODEL_PARAM: RiskTier.AUTO,
    EditOp.EDIT_TOOL_DESCRIPTION: RiskTier.AUTO,
    EditOp.REMOVE_TOOL: RiskTier.AUTO,
    EditOp.ADD_TEST: RiskTier.AUTO,
    EditOp.EDIT_DOCS: RiskTier.AUTO,
    EditOp.CREATE_FILE: RiskTier.AUTO,   # safe: creates only; never overwrites existing code
    EditOp.PATCH_SYSTEM_PROMPT: RiskTier.REVIEW,
    EditOp.PATCH_CODE: RiskTier.REVIEW,
    EditOp.ADD_TOOL: RiskTier.REVIEW,
    EditOp.DEPENDENCY_CHANGE: RiskTier.MANUAL,
    EditOp.INFRA_DEPLOY: RiskTier.MANUAL,
}
# Fail loudly if a new EditOp is added without a tier — never default to AUTO.
assert set(_TIER_TABLE) == set(EditOp), "every EditOp needs an explicit risk tier"


def assign_tier(op: EditOp) -> RiskTier:
    return _TIER_TABLE[op]


@dataclass(slots=True)
class Edit:
    """One proposed self-modification.

    `apply` is the worktree mutator handed to SelfModifier.propose (it edits files
    inside the candidate worktree and returns the changed repo-relative paths).
    `risk_tier` is advisory on input — it is always overwritten deterministically.
    """
    op: EditOp
    summary: str
    apply: ApplyFn
    rationale: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    risk_tier: RiskTier = RiskTier.MANUAL


@dataclass(slots=True)
class EditOutcome:
    edit_id: str
    op: EditOp
    tier: RiskTier
    status: str           # applied | pending_approval | manual | failed | blocked_protected
    detail: str = ""
    branch: str | None = None
    approval_id: str | None = None


# A diagnose step proposes edits from a context blob (recent failures, gaps, goals).
# Injected so this module never imports the llm layer (DAG leaf).
Diagnoser = Callable[[str], Awaitable[list[Edit]]]


class _GateLike(Protocol):
    def request(self, name: str, args: dict, reason: str) -> object: ...


def tiered(edits: list[Edit]) -> list[Edit]:
    """Return copies with risk_tier overwritten from the canonical table.

    A model-supplied tier that disagrees is logged and discarded — the model can
    never escalate its own change to a lower-friction tier."""
    out: list[Edit] = []
    for e in edits:
        correct = assign_tier(e.op)
        if e.risk_tier is not correct:
            log.info("edit %s: tier %s -> %s (op=%s)", e.id, e.risk_tier.value,
                     correct.value, e.op.value)
        out.append(replace(e, risk_tier=correct))
    return out


class SelfImprovement:
    """Drives proposed edits through the risk gate. Reuses SelfModifier (worktree +
    test + PR + PROTECTED-file refusal) and the canonical approval gate."""

    def __init__(self, modifier: SelfModifier, *, gate: _GateLike | None = None,
                 pending_store: dict[str, Edit] | None = None) -> None:
        self._mod = modifier
        self._gate: _GateLike = gate or approval.gate
        self._pending_store: dict[str, Edit] = pending_store if pending_store is not None else {}

    async def run(self, edits: list[Edit], *, dry_run: bool = False) -> list[EditOutcome]:
        return [await self._apply_one(e, dry_run=dry_run) for e in tiered(edits)]

    async def _apply_one(self, edit: Edit, *, dry_run: bool) -> EditOutcome:
        if edit.risk_tier is RiskTier.MANUAL:
            return EditOutcome(
                edit_id=edit.id, op=edit.op, tier=edit.risk_tier, status="manual",
                detail="manual tier — human-only, not auto-applied",
            )

        if edit.risk_tier is RiskTier.REVIEW:
            # Route to the PROTECTED gate; apply only after a human approves (the
            # gateway /approvals flow resolves it, then apply_approved runs the edit).
            # Honor the global kill-switch: refuse new REVIEW-tier requests when on.
            try:
                from hive.core.approval_enhancements import enhance as _enhance
                if _enhance.is_request_blocked():
                    return EditOutcome(
                        edit_id=edit.id, op=edit.op, tier=edit.risk_tier,
                        status="blocked_kill_switch",
                        detail="kill-switch engaged; no new self-mod requests",
                    )
            except Exception:  # noqa: BLE001
                pass
            approval_id = str(self._gate.request(
                f"self_mod:{edit.op.value}", {"summary": edit.summary}, edit.rationale))
            try:
                from hive.core.approval_enhancements import enhance as _enhance
                _enhance.audit_request(approval_id)
            except Exception:  # noqa: BLE001
                pass
            self._pending_store[approval_id] = edit  # retrieved by gateway on approval
            return EditOutcome(
                edit_id=edit.id, op=edit.op, tier=edit.risk_tier,
                status="pending_approval", approval_id=approval_id,
                detail="awaiting human approval",
            )

        # AUTO: still isolated, still tested, still never merged, still PROTECTED-safe.
        result = await self._mod.propose(edit.summary, edit.rationale, edit.apply,
                                         dry_run=dry_run)
        if not result.get("ok"):
            stage = result.get("stage")
            if stage == "protected":
                return EditOutcome(
                    edit_id=edit.id, op=edit.op, tier=edit.risk_tier,
                    status="blocked_protected",
                    detail=str(result.get("msg", "touches a PROTECTED file")),
                )
            return EditOutcome(
                edit_id=edit.id, op=edit.op, tier=edit.risk_tier, status="failed",
                detail=f"{stage}: {str(result.get('log', ''))[:200]}",
            )
        return EditOutcome(
            edit_id=edit.id, op=edit.op, tier=edit.risk_tier, status="applied",
            branch=str(result.get("branch")) if result.get("branch") else None,
            detail=str(result.get("stage", "")),
        )

    def get_pending(self, approval_id: str) -> "Edit | None":
        """Return the pending REVIEW edit for an approval_id, or None if not found."""
        return self._pending_store.get(approval_id)

    def cancel_review(self, approval_id: str) -> bool:
        """Remove a REVIEW-tier edit from the pending store. Returns False if not found."""
        if approval_id in self._pending_store:
            self._pending_store.pop(approval_id)
            return True
        return False

    def pending_count(self) -> int:
        """Number of REVIEW-tier edits awaiting human approval."""
        return len(self._pending_store)

    def get_all_pending(self) -> dict[str, "Edit"]:
        """Return a copy of all pending REVIEW-tier edits keyed by approval_id."""
        return dict(self._pending_store)

    def cancel_all_pending(self) -> int:
        """Cancel all pending REVIEW-tier edits. Returns the count cancelled."""
        count = len(self._pending_store)
        self._pending_store.clear()
        return count

    def describe_pending(self) -> list[dict]:
        """Return a list of metadata dicts for all pending REVIEW-tier edits."""
        result = []
        for approval_id, edit in self._pending_store.items():
            result.append({
                "approval_id": approval_id,
                "edit_id": edit.id,
                "op": edit.op.value,
                "summary": edit.summary,
                "rationale": edit.rationale,
                "risk_tier": edit.risk_tier.value,
            })
        return result

    def oldest_pending_id(self) -> "str | None":
        """Return the approval_id of the oldest pending REVIEW edit (first inserted), or None."""
        return next(iter(self._pending_store), None)

    def tier_summary(self) -> dict:
        """Return a summary of pending edits by op category.

        Returns counts of pending REVIEW edits grouped by EditOp value."""
        counts: dict[str, int] = {}
        for edit in self._pending_store.values():
            key = edit.op.value
            counts[key] = counts.get(key, 0) + 1
        return {"pending_review": len(self._pending_store), "by_op": counts}

    async def apply_approved(self, edit: Edit, *, dry_run: bool = False) -> EditOutcome:
        """Run a REVIEW-tier edit after a human approved it (called by the gateway
        approvals flow). Goes through the same SelfModifier safety path as AUTO."""
        result = await self._mod.propose(edit.summary, edit.rationale, edit.apply,
                                         dry_run=dry_run)
        if not result.get("ok"):
            stage = result.get("stage")
            status = "blocked_protected" if stage == "protected" else "failed"
            return EditOutcome(
                edit_id=edit.id, op=edit.op, tier=edit.risk_tier, status=status,
                detail=str(result.get("msg") or stage),
            )
        return EditOutcome(
            edit_id=edit.id, op=edit.op, tier=edit.risk_tier, status="applied",
            branch=str(result.get("branch")) if result.get("branch") else None,
        )


async def diagnose_and_run(diagnoser: Diagnoser, context: str,
                           improver: SelfImprovement, *, dry_run: bool = False,
                           ) -> list[EditOutcome]:
    """Full loop: diagnose (injected LLM) -> typed edits -> tier -> gate -> apply.
    Returns one outcome per proposed edit. A diagnoser that returns [] is a no-op."""
    edits = await diagnoser(context)
    if not edits:
        return []
    return await improver.run(edits, dry_run=dry_run)
