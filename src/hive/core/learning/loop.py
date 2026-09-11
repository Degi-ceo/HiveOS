"""
loop.py — orchestrator for the learning loop (SPRINT_6 P-F).

``LearningLoop.run(symptom)`` is the single entry point used by:
  - ``heartbeat.py`` (when ``config.learning_loop_enabled`` is true)
  - ``runtime.self_improve_from_symptom`` (with ``use_learning_loop=True``)
  - The future ``hive learning replay`` CLI (dry-run replay)

The loop composes the four lower layers in this fixed order:
    1. Tracer.collect_context   — gather recent failing traces
The production path is ``gate_candidate``. SelfModifier invokes it while the
candidate worktree still exists, after tests and before commit/push. The legacy
``run`` entry point rejects when no real applier is injected; it never records
an accepted no-op.

The loop NEVER raises to the caller — failures become LoopOutcome records
with verdict=reject + a reason. Heartbeat treats this as "not improved yet"
and moves on.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from hive.core.learning import storage
from hive.core.learning.evaluator import EvalScore, Evaluator
from hive.core.learning.evolver import Evolver, Proposal
from hive.core.learning.tracer import Tracer
from hive.core.types import VERDICT_ACCEPT, VERDICT_REJECT, LoopOutcome

log = logging.getLogger(__name__)

_EVALUATION_CONTROL_PREFIXES = (
    ".github/workflows/",
    "evals/",
    "src/hive/evals/",
    "src/hive/core/learning/",
)
_EVALUATION_CONTROL_EXACT = {
    "pyproject.toml",
    "pytest.ini",
    "setup.cfg",
    "tox.ini",
    "conftest.py",
    "sitecustomize.py",
    "usercustomize.py",
    "src/sitecustomize.py",
    "src/usercustomize.py",
    "src/hive/__init__.py",
}


def _requires_supervisor_tool_evidence(path: str) -> bool:
    """Return whether a candidate must be escalated until tool IPC is trusted.

    Candidate processes currently own their runtime ledger and tool executor.
    Their structured trace/run/outcome fields are therefore observations, not
    security attestations. Only documentation-only candidates can be accepted
    without relying on those fields; every other change fails closed to manual
    review until execution and evidence collection move behind supervisor IPC.
    """
    normalized = path.replace("\\", "/").lower()
    return not (normalized.startswith("docs/") and normalized.endswith(".md"))


@dataclass(slots=True)
class LoopConfig:
    """Runtime configuration for one ``LearningLoop.run`` call.

    Defaults are conservative — operators must opt in for any aggressive
    behaviour. ``enabled`` mirrors ``config.learning_loop_enabled`` so the
    heartbeat can decide whether to invoke the loop at all (when ``False``,
    ``run`` returns a no-op ``LoopOutcome`` without doing any work).
    """
    enabled: bool = False
    eval_timeout: float = 60.0
    repo_root: str = "."
    db_path: str = ""
    evals_dataset: str = "evals/datasets/runtime_smoke.jsonl"
    # The function the loop uses to materialise a candidate. In production
    # this is ``SelfModifier.propose`` (called a second time with
    # ``dry_run=False``). For tests it's a stub.
    apply_fn_factory: Callable[[Proposal], Awaitable[dict[str, Any]]] | None = None


class LearningLoop:
    """trace → evolve → eval → apply(guarded) orchestrator.

    Construct once per runtime. ``run`` is async (because Evolver +
    SelfModifier are async). Callers from sync code can use
    ``asyncio.run(loop.run(symptom))``.
    """

    def __init__(
        self,
        tracer: Tracer,
        evolver: Evolver,
        evaluator: Evaluator,
        config: LoopConfig,
    ) -> None:
        self._tracer = tracer
        self._evolver = evolver
        self._evaluator = evaluator
        self._config = config

    # --- public -------------------------------------------------------------

    async def run(self, symptom: str) -> LoopOutcome:
        """Run one learning-loop iteration for ``symptom``.

        Never raises — all errors are captured as ``verdict=reject`` with a
        ``reject_reason``. Returns a ``LoopOutcome`` (also persisted to
        ``learning_loops`` if ``config.db_path`` is set).
        """
        ts = time.time()
        if not self._config.enabled:
            return self._finalise(
                ts=ts,
                symptom=symptom,
                verdict=VERDICT_REJECT,
                reason="learning loop disabled (set HIVE_LEARNING_LOOP_ENABLED=true)",
            )
        if not symptom.strip():
            return self._finalise(
                ts=ts,
                symptom=symptom,
                verdict=VERDICT_REJECT,
                reason="empty symptom",
            )
        if self._config.apply_fn_factory is None:
            return self._finalise(
                ts=ts,
                symptom=symptom,
                verdict=VERDICT_REJECT,
                reason=("direct learning-loop execution has no candidate applier; "
                        "use SelfModifier's in-worktree candidate gate"),
            )

        # 1) collect context — best effort, never blocking
        try:
            recent_failures = self._tracer.recent_failures(
                threshold=20, window_minutes=60
            )
            log.debug(
                "learning_loop: collected %d recent failures for symptom=%s",
                len(recent_failures), symptom[:80],
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("learning_loop: tracer.collect failed: %s", exc)

        # 2) evolve — must produce a Proposal; if dry-run failed at
        # pytest/protected stage, we record a reject and return.
        try:
            proposal = await self._evolver.propose_for_symptom(
                symptom,
                apply_fn=self._build_apply_fn(),
            )
        except Exception as exc:
            return self._finalise(
                ts=ts,
                symptom=symptom,
                verdict=VERDICT_REJECT,
                reason=f"evolver raised: {exc}",
            )

        if not proposal.dry_run_result.get("ok"):
            return self._finalise(
                ts=ts,
                symptom=symptom,
                verdict=VERDICT_REJECT,
                reason=(
                    f"dry-run failed at stage="
                    f"{proposal.dry_run_result.get('stage', 'unknown')}"
                ),
                worktree_branch=proposal.branch,
            )

        # 3) + 4) score + compare
        try:
            baseline = self._evaluator.score_baseline()
            candidate = self._evaluator.score(proposal.worktree_path)
            verdict = self._evaluator.compare(baseline, candidate)
        except Exception as exc:
            return self._finalise(
                ts=ts,
                symptom=symptom,
                verdict=VERDICT_REJECT,
                reason=f"evaluator raised: {exc}",
                worktree_branch=proposal.branch,
                baseline=EvalScore(),
                candidate=EvalScore(),
            )

        # 5) apply on accept
        pr_url = None
        if verdict.verdict == VERDICT_ACCEPT:
            try:
                apply_result = await self._materialise(proposal)
                pr_url = apply_result.get("pr_url")
            except Exception as exc:
                return self._finalise(
                    ts=ts,
                    symptom=symptom,
                    verdict=VERDICT_REJECT,
                    reason=f"apply raised after accept: {exc}",
                    worktree_branch=proposal.branch,
                    baseline=verdict.baseline,
                    candidate=verdict.candidate,
                )

        return self._finalise(
            ts=ts,
            symptom=symptom,
            verdict=verdict.verdict,
            reason=verdict.reason,
            worktree_branch=proposal.branch,
            pr_url=pr_url,
            baseline=verdict.baseline,
            candidate=verdict.candidate,
        )

    # --- internal -----------------------------------------------------------

    async def gate_candidate(
        self,
        worktree: str,
        base_commit: str,
        changed: list[str],
        run_id: str,
        candidate_digest: str,
    ) -> dict[str, Any]:
        """Evaluate a live candidate worktree and persist a correlated verdict."""
        import asyncio
        try:
            normalized_changed = [path.replace("\\", "/").lower() for path in changed]
            control_changes = [
                path for path in normalized_changed
                if (
                    path in _EVALUATION_CONTROL_EXACT
                    or path.endswith(("/conftest.py", "/sitecustomize.py", "/usercustomize.py"))
                    or any(
                        path == prefix.rstrip("/") or path.startswith(prefix)
                        for prefix in _EVALUATION_CONTROL_PREFIXES
                    )
                )
            ]
            if control_changes:
                raise RuntimeError(
                    "candidate changes the evaluation control plane: "
                    + ", ".join(control_changes[:10])
                )
            manual_changes = [
                path for path in normalized_changed
                if _requires_supervisor_tool_evidence(path)
            ]
            if manual_changes:
                reason = (
                    "candidate runtime evidence is not externally attestable; "
                    "manual review required for: " + ", ".join(manual_changes[:10])
                )
                self._finalise(
                    ts=time.time(), symptom="candidate evaluation",
                    verdict=VERDICT_REJECT, reason=reason, run_id=run_id,
                    candidate_digest=candidate_digest,
                )
                return {
                    "ok": False,
                    "verdict": VERDICT_REJECT,
                    "reason": reason,
                    "run_id": run_id,
                    "candidate_digest": candidate_digest,
                    "required_tier": "manual",
                }
            if not re.fullmatch(r"[0-9a-f]{64}", candidate_digest):
                raise RuntimeError("candidate artifact digest is missing or malformed")
            baseline = await asyncio.to_thread(
                self._evaluator.score_baseline, expected_commit=base_commit,
            )
            candidate = await asyncio.to_thread(self._evaluator.score, worktree)
            verdict = self._evaluator.compare(
                baseline, candidate, run_id=run_id, candidate_digest=candidate_digest,
            )
        except Exception as exc:
            reason = f"candidate gate failed closed: {type(exc).__name__}: {exc}"
            self._finalise(
                ts=time.time(), symptom="candidate evaluation", verdict=VERDICT_REJECT,
                reason=reason, run_id=run_id,
            )
            return {"ok": False, "reason": reason, "run_id": run_id}

        self._finalise(
            ts=time.time(),
            symptom=f"candidate files: {', '.join(changed[:20])}",
            verdict=verdict.verdict,
            reason=verdict.reason,
            worktree_branch=None,
            baseline=baseline,
            candidate=candidate,
            run_id=run_id,
            candidate_digest=candidate_digest,
            pytest_delta=verdict.pytest_delta,
            evals_delta=verdict.evals_delta,
        )
        return {
            "ok": verdict.verdict == VERDICT_ACCEPT,
            "verdict": verdict.verdict,
            "reason": verdict.reason,
            "run_id": run_id,
            "base_commit": base_commit,
            "candidate_digest": candidate_digest,
            "pytest_delta": verdict.pytest_delta,
            "evals_delta": verdict.evals_delta,
        }

    def _build_apply_fn(self) -> Callable[..., Awaitable[list[str]]]:
        """Default apply_fn: a no-op that returns no changed files.

        Real callers (heartbeat) inject a custom ``apply_fn`` at the call
        site, since the actual edit depends on the symptom. The Evolver's
        dry-run path runs this apply_fn inside the candidate worktree.
        """
        async def _noop(_wt: str) -> list[str]:  # noqa: ARG001 - worktree unused
            return []
        return _noop

    async def _materialise(self, proposal: Proposal) -> dict[str, Any]:
        """Apply the candidate change. Default: delegate to
        ``config.apply_fn_factory`` if set, else return no-op."""
        if self._config.apply_fn_factory is not None:
            return await self._config.apply_fn_factory(proposal)
        # No custom applier — log and return without applying. The
        # trace still records a rejected-like outcome (verdict stays
        # "accept" per the comparator, but pr_url is None).
        log.info(
            "learning_loop: no apply_fn_factory configured; "
            "accept recorded but no materialisation for branch=%s",
            proposal.branch,
        )
        return {"ok": True, "pr_url": None}

    def _finalise(
        self,
        *,
        ts: float,
        symptom: str,
        verdict: str,
        reason: str = "",
        worktree_branch: str | None = None,
        pr_url: str | None = None,
        baseline: EvalScore | None = None,
        candidate: EvalScore | None = None,
        run_id: str = "",
        candidate_digest: str = "",
        pytest_delta: float = 0.0,
        evals_delta: float = 0.0,
    ) -> LoopOutcome:
        """Build the LoopOutcome, persist it, and return."""
        b = baseline or EvalScore()
        c = candidate or EvalScore()
        outcome = LoopOutcome(
            ts=ts,
            symptom=symptom,
            verdict=verdict,
            pytest_baseline=b.pytest_pass_rate,
            pytest_candidate=c.pytest_pass_rate,
            evals_baseline=b.evals_pass_rate,
            evals_candidate=c.evals_pass_rate,
            worktree_branch=worktree_branch,
            pr_url=pr_url,
            reject_reason=reason or None,
            run_id=run_id,
            candidate_digest=candidate_digest,
            pytest_delta=pytest_delta,
            evals_delta=evals_delta,
        )
        if self._config.db_path:
            try:
                storage.insert_loop(self._config.db_path, outcome)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("learning_loop: persist failed: %s", exc)
        return outcome

    @property
    def config(self) -> LoopConfig:
        return self._config

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"LearningLoop(enabled={self._config.enabled})"
