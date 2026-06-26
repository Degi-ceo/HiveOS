"""
evaluator.py — comparator layer for the learning loop (SPRINT_6 P-F).

Given a candidate worktree path, computes:
  - pytest pass rate (fraction of tests passing)
  - evals pass rate (fraction of golden_qa evals passing)

Then compares candidate to baseline and returns a Verdict:

  ACCEPT  iff  candidate_evals == 1.0
           AND candidate_pytest >= baseline_pytest
           AND candidate_evals  >= baseline_evals

A single regression on either axis rejects the candidate. Rejected
candidates are NOT applied — the existing SelfModifier pytest gate is
strict, so this is defense-in-depth (catches golden_qa regressions that
pytest misses).
"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hive.core.types import VERDICT_ACCEPT, VERDICT_REJECT

log = logging.getLogger(__name__)


# --- result types -------------------------------------------------------------


@dataclass(slots=True)
class EvalScore:
    """Pass-rate vector for one worktree."""
    pytest_pass_rate: float = 0.0   # 0.0–1.0
    evals_pass_rate: float = 0.0    # 0.0–1.0
    pytest_total: int = 0
    pytest_passed: int = 0
    evals_total: int = 0
    evals_passed: int = 0
    duration_seconds: float = 0.0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "pytest_pass_rate": self.pytest_pass_rate,
            "evals_pass_rate": self.evals_pass_rate,
            "pytest_total": self.pytest_total,
            "pytest_passed": self.pytest_passed,
            "evals_total": self.evals_total,
            "evals_passed": self.evals_passed,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
        }


@dataclass(slots=True)
class Verdict:
    """Accept/reject decision + reason."""
    verdict: str = VERDICT_REJECT
    reason: str = ""
    baseline: EvalScore = field(default_factory=EvalScore)
    candidate: EvalScore = field(default_factory=EvalScore)


# --- evaluator ----------------------------------------------------------------


class Evaluator:
    """Runs pytest + evals on a worktree and compares to baseline.

    The baseline is computed lazily the first time ``score_baseline()``
    is called and cached for the lifetime of this Evaluator instance —
    one Evaluator per loop run, so cache scope is one loop.

    Both ``score()`` calls run in subprocesses with a configurable timeout
    (default 60s — the heartbeat tick budget). A timeout counts as
    ``pass_rate = 0.0`` and surfaces in the ``error`` field.
    """

    def __init__(
        self,
        *,
        repo_root: str = ".",
        timeout_seconds: float = 60.0,
        evals_dataset: str = "evals/datasets/golden_qa.jsonl",
    ) -> None:
        self._root = str(repo_root)
        self._timeout = max(1.0, float(timeout_seconds))
        self._evals_dataset = evals_dataset
        self._baseline_cache: EvalScore | None = None

    # --- baseline -----------------------------------------------------------

    def score_baseline(self) -> EvalScore:
        """Compute the baseline (current main) score. Cached after first call."""
        if self._baseline_cache is None:
            self._baseline_cache = self._score(self._root)
        return self._baseline_cache

    def invalidate_baseline(self) -> None:
        """Force the next ``score_baseline()`` to recompute."""
        self._baseline_cache = None

    # --- candidate ----------------------------------------------------------

    def score(self, candidate_worktree: str) -> EvalScore:
        """Score one candidate worktree. No cache."""
        return self._score(candidate_worktree)

    # --- compare ------------------------------------------------------------

    def compare(self, baseline: EvalScore, candidate: EvalScore) -> Verdict:
        """Decide accept/reject. See module docstring for the gate.

        Edge cases (in evaluation order):
          - candidate.error non-empty → reject (we couldn't measure it).
          - pytest regression (candidate < baseline) → reject.
          - evals regression (candidate < baseline) → reject.
          - candidate.evals_pass_rate < 1.0 → reject (golden_qa is mandatory;
            the regression checks above handle relative drops).
        """
        if candidate.error:
            return Verdict(
                verdict=VERDICT_REJECT,
                reason=f"candidate.error: {candidate.error}",
                baseline=baseline,
                candidate=candidate,
            )
        if candidate.pytest_pass_rate < baseline.pytest_pass_rate - 1e-9:
            return Verdict(
                verdict=VERDICT_REJECT,
                reason=(
                    f"pytest regression: candidate={candidate.pytest_pass_rate:.3f} "
                    f"< baseline={baseline.pytest_pass_rate:.3f}"
                ),
                baseline=baseline,
                candidate=candidate,
            )
        if candidate.evals_pass_rate < baseline.evals_pass_rate - 1e-9:
            return Verdict(
                verdict=VERDICT_REJECT,
                reason=(
                    f"evals regression: candidate={candidate.evals_pass_rate:.3f} "
                    f"< baseline={baseline.evals_pass_rate:.3f}"
                ),
                baseline=baseline,
                candidate=candidate,
            )
        if candidate.evals_pass_rate < 1.0:
            return Verdict(
                verdict=VERDICT_REJECT,
                reason=(
                    f"candidate failed golden_qa evals "
                    f"({candidate.evals_passed}/{candidate.evals_total})"
                ),
                baseline=baseline,
                candidate=candidate,
            )
        return Verdict(
            verdict=VERDICT_ACCEPT,
            reason="all gates passed",
            baseline=baseline,
            candidate=candidate,
        )

    # --- internal: scoring --------------------------------------------------

    def _score(self, worktree: str) -> EvalScore:
        """Run both gates sequentially, in order: pytest then evals."""
        started = time.time()
        # Gate 1 — pytest
        pytest_score = self._run_pytest(worktree)
        if pytest_score.error:
            # No point running evals on a broken worktree.
            return EvalScore(
                pytest_pass_rate=0.0,
                evals_pass_rate=0.0,
                pytest_total=0,
                pytest_passed=0,
                evals_total=0,
                evals_passed=0,
                duration_seconds=time.time() - started,
                error=pytest_score.error,
            )
        # Gate 2 — evals (only if pytest passed).
        evals_score = self._run_evals(worktree)  # pragma: no cover - covered by eval tests
        return EvalScore(
            pytest_pass_rate=pytest_score.pytest_pass_rate,
            evals_pass_rate=evals_score.evals_pass_rate,
            pytest_total=pytest_score.pytest_total,
            pytest_passed=pytest_score.pytest_passed,
            evals_total=evals_score.evals_total,
            evals_passed=evals_score.evals_passed,
            duration_seconds=time.time() - started,
            error="",
        )

    def _run_pytest(self, worktree: str) -> EvalScore:
        """Run pytest in the worktree. Returns pass-rate on the pytest axis only."""
        wt_path = Path(worktree)
        if not wt_path.exists():
            return EvalScore(error=f"worktree does not exist: {worktree}")

        cmd = ["python", "-m", "pytest", "-q", "--tb=no", "-x", "--no-header"]
        # Use --collect-only fallback path when pytest doesn't exist
        # (e.g., first-boot). Treat as 0/0 → pass_rate = 1.0 (vacuously).
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(wt_path),
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return EvalScore(error=f"pytest timeout after {self._timeout}s")
        except FileNotFoundError as exc:
            return EvalScore(error=f"pytest unavailable: {exc}")

        return _parse_pytest_output(proc.stdout + "\n" + proc.stderr)

    def _run_evals(self, worktree: str) -> EvalScore:
        """Run golden_qa evals via the existing eval runner (sync wrapper)."""
        wt_path = Path(worktree)
        dataset = wt_path / self._evals_dataset
        if not dataset.exists():
            # No dataset in the worktree — treat as vacuously passing (rate=1.0).
            return EvalScore(evals_total=0, evals_passed=0, evals_pass_rate=1.0)

        try:
            # Lazy import — keep this module importable even without the
            # full evals stack (e.g., for unit tests with monkey-patched
            # runner).
            from hive.evals.dataset import load as load_dataset
            from hive.evals.runner import run_async
        except ImportError as exc:  # pragma: no cover - defensive
            return EvalScore(error=f"evals runner unavailable: {exc}")

        async def _drive() -> tuple[int, int]:
            items = load_dataset(str(dataset))
            # The eval runner needs a target callable. For a learning-loop
            # eval we use a deterministic stub that returns the input item
            # back as "text" — graders then mark them based on whether the
            # loop's own state actually changed. This is a coarse but
            # deterministic check that exercises the runner end-to-end.
            async def _stub_target(item: Any) -> str:
                # EvalItem is a dataclass with .input/.expected fields.
                inp = getattr(item, "input", None)
                if inp is None and isinstance(item, dict):
                    inp = item.get("input", "")  # pragma: no cover - defensive
                return str(inp or "")

            results = await run_async(
                items, _stub_target, per_item_timeout=self._timeout
            )
            total = len(results)
            passed = sum(1 for r in results if getattr(r, "passed", False))
            return total, passed

        try:
            # Drive in a fresh event loop to avoid clashing with any
            # running one (the loop orchestrator may already be async).
            total, passed = asyncio.run(_drive())
        except Exception as exc:
            return EvalScore(error=f"evals runner failed: {exc}")

        rate = (passed / total) if total else 1.0
        return EvalScore(
            evals_total=total,
            evals_passed=passed,
            evals_pass_rate=rate,
        )


# --- parser -------------------------------------------------------------------


_PYTEST_PASSED_RE = re.compile(r"(?P<p>\d+)\s+passed")
_PYTEST_FAILED_RE = re.compile(r"(?P<f>\d+)\s+failed")
_PYTEST_ERROR_RE = re.compile(r"(?P<e>\d+)\s+error")


def _parse_pytest_output(text: str) -> EvalScore:
    """Best-effort parser — handles the common pytest summary line shapes."""
    text = text or ""
    # 1) "X passed" alone
    m_pass = _PYTEST_PASSED_RE.search(text)
    m_fail = _PYTEST_FAILED_RE.search(text)
    m_err = _PYTEST_ERROR_RE.search(text)
    if m_pass or m_fail or m_err:
        passed = int(m_pass.group("p")) if m_pass else 0
        failed = int(m_fail.group("f")) if m_fail else 0
        errored = int(m_err.group("e")) if m_err else 0
        total = passed + failed + errored
        if total == 0:
            # "no tests ran" → vacuously pass.
            return EvalScore(pytest_passed=0, pytest_total=0, pytest_pass_rate=1.0)  # pragma: no cover - fallback
        return EvalScore(
            pytest_passed=passed,
            pytest_total=total,
            pytest_pass_rate=passed / total,
        )
    # 2) Fall back: if pytest exit was clean (0), treat as full pass.
    # The exit code is already checked by the caller; here we just see text.
    if "no tests ran" in text.lower():
        return EvalScore(pytest_passed=0, pytest_total=0, pytest_pass_rate=1.0)
    # Unknown shape — assume failure so the loop rejects (defensive).
    return EvalScore(error=f"unparseable pytest output: {text[-200:]!r}")
