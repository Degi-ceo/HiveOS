"""
evolver.py — proposal layer for the learning loop (SPRINT_6 P-F).

Wraps ``core/self_mod.py:SelfModifier.propose()``. Adds an eval gate on top
of the existing pytest gate so the loop can REJECT a candidate that passes
pytest but regresses on golden_qa evals (or vice-versa).

Design contract:
- Pure proposal: returns a ``Proposal`` dataclass. Does NOT apply anything
  — that's the loop's job (so the loop can attach an eval verdict first).
- Reuses existing SelfModifier — does NOT re-implement worktree creation.
  The SelfModifier handles: branch, worktree, protected-file check, pytest,
  and PR open. The loop orchestrates eval BEFORE ``SelfModifier.propose``
  finishes by intercepting via a custom ``apply_fn`` shim.
- Backward compatible: if ``SelfModifier.propose`` was enough yesterday, it
  still is today. ``Evolver`` is a new entry point.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from hive.core.learning import storage

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Proposal:
    """One self-mod proposal produced by the Evolver.

    ``worktree_path`` points at the candidate branch worktree. The loop
    runs the Evaluator there before deciding accept/reject.
    """
    title: str = ""
    description: str = ""
    symptom: str = ""
    branch: str = ""
    worktree_path: str = ""
    base_sha: str = ""
    changed_files: list[str] = field(default_factory=list)
    # The ``ApplyFn`` shim the loop will hand to SelfModifier.propose().
    # Stored on the proposal so the loop can apply the change only after
    # the eval gate accepts. Type matches SelfModifier's ApplyFn.
    apply_fn: Callable[..., Awaitable[list[str]]] | None = None
    # The original dry_run SelfModifier call result — kept for diagnostics
    # when the loop wants to log what pytest said about the candidate.
    dry_run_result: dict[str, Any] = field(default_factory=dict)


class Evolver:
    """Builds Proposal objects from symptom text.

    The constructor takes a SelfModifier instance (so the same shared
    modifier — with its history, event bus, runner — is reused). The
    Evolver does NOT mutate the modifier; it only calls ``propose(..., dry_run=True)``
    so no PR is opened and no permanent branch is created during the eval
    phase. If the loop accepts, it then calls ``propose(..., dry_run=False)``
    on the same modifier to materialise the change.

    Args:
        modifier: shared SelfModifier.
        db_path: SQLite path (for the optional persist-helper; the loop
            usually persists the LoopOutcome itself).
    """

    def __init__(self, modifier: Any, db_path: str = "") -> None:
        self._modifier = modifier
        self._db_path = db_path

    @property
    def modifier(self) -> Any:
        return self._modifier

    async def propose_for_symptom(
        self,
        symptom: str,
        *,
        apply_fn: Callable[..., Awaitable[list[str]]],
        title: str = "",
        description: str = "",
    ) -> Proposal:
        """Produce a candidate worktree via SelfModifier (dry-run) and
        return a Proposal. The apply_fn must NOT have run yet — the loop
        hands the SAME apply_fn to a follow-up ``propose(dry_run=False)``
        call after the eval gate accepts."""
        ts = int(time.time())
        branch = f"hive/learning-{ts}"
        title = title or f"learning-loop: {symptom[:60]}"
        description = description or (
            f"Auto-proposed by learning loop for symptom:\n\n> {symptom}"
        )

        # Run the modifier in dry-run mode. SelfModifier still creates a
        # worktree, runs apply_fn there, runs pytest, and reports back.
        # We capture the worktree path from the result so the evaluator
        # can score it directly.
        result = await self._modifier.propose(
            title, description, apply_fn, dry_run=True
        )

        proposal = Proposal(
            title=title,
            description=description,
            symptom=symptom,
            branch=branch,
            worktree_path=str(result.get("worktree", "")),
            base_sha=str(result.get("base_sha", "")),
            changed_files=list(result.get("changed", [])),
            apply_fn=apply_fn,
            dry_run_result=result,
        )

        if not result.get("ok"):
            # Dry-run failed at an early stage (worktree / pytest /
            # protected). Surface this to the loop — no point running
            # evals on a candidate that doesn't even pass pytest.
            log.info(
                "evolver.dry_run failed for symptom=%s stage=%s",
                symptom[:80], result.get("stage"),
            )

        return proposal

    def persist_proposal_marker(self, proposal: Proposal) -> int:
        """Optional helper: log the proposal attempt to learning_loops so the
        operator can see it via ``hive learning status`` even before the loop
        finishes. Returns the row id (0 on DB failure — defensive)."""
        if not self._db_path:
            return 0
        try:
            # Ensure the table exists — the helper may be called before
            # the Tracer has touched the DB.
            storage.ensure_schema(self._db_path)
            return storage.insert_loop(
                self._db_path,
                _marker_outcome(proposal),
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("evolver.persist_proposal_marker failed: %s", exc)
            return 0


def _marker_outcome(proposal: Proposal) -> Any:
    """Build a minimal LoopOutcome for the in-progress marker row."""
    from hive.core.types import VERDICT_REJECT, LoopOutcome  # local to dodge cycles
    return LoopOutcome(
        ts=time.time(),
        symptom=proposal.symptom,
        verdict=VERDICT_REJECT,  # placeholder until the loop finalises
        worktree_branch=proposal.branch,
        reject_reason=f"marker: dry_run stage={proposal.dry_run_result.get('stage', 'unknown')}",
    )
