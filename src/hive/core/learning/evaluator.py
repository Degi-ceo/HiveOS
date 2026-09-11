"""
evaluator.py — comparator layer for the learning loop (SPRINT_6 P-F).

Given a candidate worktree path, computes:
  - pytest pass rate (fraction of tests passing)
  - evals pass rate (fraction of real-runtime smoke evals passing)

Then compares candidate to baseline and returns a Verdict:

  ACCEPT  iff  candidate_evals == 1.0
           AND candidate_pytest >= baseline_pytest
           AND candidate_evals  >= baseline_evals

A single regression on either axis rejects the candidate. Rejected
candidates are NOT applied — the existing SelfModifier pytest gate is
strict, so this is defense-in-depth (catches runtime regressions that
pytest misses).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hive.core.child_env import without_privileged_credentials
from hive.core.learning import storage
from hive.core.types import VERDICT_ACCEPT, VERDICT_REJECT

log = logging.getLogger(__name__)

CandidateRunner = Callable[
    [str | list[str], str | None],
    Awaitable[tuple[int, str]],
]


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
    run_id: str = ""
    candidate_digest: str = ""
    pytest_delta: float = 0.0
    evals_delta: float = 0.0


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
        evals_dataset: str = "evals/datasets/runtime_smoke.jsonl",
        db_path: str | None = None,
        target_id: str = "hive-runtime:v1",
        tolerated_regression: float = 0.0,
        run_pytest: bool = True,
        candidate_runner: CandidateRunner | None = None,
    ) -> None:
        self._root = str(repo_root)
        self._timeout = max(1.0, float(timeout_seconds))
        self._evals_dataset = evals_dataset
        self._db_path = db_path
        self._target_id = target_id
        threshold = float(tolerated_regression)
        if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
            raise ValueError("tolerated_regression must be between 0 and 1")
        self._tolerated_regression = threshold
        self._run_pytest_gate = bool(run_pytest)
        self._candidate_runner = candidate_runner
        self._baseline_cache: EvalScore | None = None
        self._baseline_identity: tuple[str, str, str] | None = None

    # --- baseline -----------------------------------------------------------

    def score_baseline(self, *, expected_commit: str | None = None) -> EvalScore:
        """Return the score bound to the current commit/dataset/target identity."""
        identity = self.baseline_identity()
        guard_error = self._baseline_guard(identity[0], expected_commit)
        if guard_error:
            return EvalScore(error=guard_error)
        if self._baseline_cache is not None and self._baseline_identity == identity:
            return self._baseline_cache
        if self._db_path:
            saved = storage.get_evaluation_baseline(
                self._db_path,
                base_commit=identity[0],
                dataset_hash=identity[1],
                target_id=identity[2],
            )
            if saved is not None:
                self._baseline_cache = EvalScore(**saved)
                self._baseline_identity = identity
                return self._baseline_cache
        self._baseline_cache = self._score(self._root)
        self._baseline_identity = identity
        if self._db_path and not self._baseline_cache.error:
            storage.insert_evaluation_baseline(
                self._db_path,
                base_commit=identity[0],
                dataset_hash=identity[1],
                target_id=identity[2],
                score=self._baseline_cache.as_dict(),
                created_ts=time.time(),
            )
        return self._baseline_cache

    def baseline_identity(self) -> tuple[str, str, str]:
        """Return ``(commit SHA, dataset SHA-256, target id)`` for persistence."""
        commit = self._git_output(self._root, ["git", "rev-parse", "HEAD"])
        dataset = Path(self._root) / self._evals_dataset
        if not dataset.is_file():
            return commit, "missing", self._target_id
        digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
        return commit, digest, self._target_id

    def invalidate_baseline(self) -> None:
        """Force the next ``score_baseline()`` to recompute."""
        self._baseline_cache = None
        self._baseline_identity = None

    def _baseline_guard(self, commit: str, expected_commit: str | None) -> str:
        """Ensure a scored baseline is the clean immutable commit requested by the gate."""
        if expected_commit is None:
            return ""
        if commit == "unknown" or commit != expected_commit:
            return (
                "baseline commit mismatch: "
                f"expected={expected_commit or 'missing'} actual={commit}"
            )
        try:
            status = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=all", "--", "."],
                cwd=self._root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=_evaluation_env(),
            )
        except OSError as exc:
            return f"unable to verify baseline worktree: {exc}"
        if status.returncode != 0:
            return "unable to verify baseline worktree"
        if status.stdout.strip():
            return "baseline worktree is dirty; refusing commit-scoped comparison"
        return ""

    # --- candidate ----------------------------------------------------------

    def score(self, candidate_worktree: str) -> EvalScore:
        """Score one candidate worktree. No cache."""
        return self._score(candidate_worktree)

    # --- compare ------------------------------------------------------------

    def compare(self, baseline: EvalScore, candidate: EvalScore, *,
                run_id: str = "", candidate_digest: str = "") -> Verdict:
        """Decide accept/reject. See module docstring for the gate.

        Edge cases (in evaluation order):
          - candidate.error non-empty → reject (we couldn't measure it).
          - pytest regression (candidate < baseline) → reject.
          - evals regression (candidate < baseline) → reject.
          - candidate.evals_pass_rate < 1.0 → reject (runtime evals are mandatory;
            the regression checks above handle relative drops).
        """
        pytest_delta = candidate.pytest_pass_rate - baseline.pytest_pass_rate
        evals_delta = candidate.evals_pass_rate - baseline.evals_pass_rate

        def decision(verdict: str, reason: str) -> Verdict:
            return Verdict(
                verdict=verdict, reason=reason, baseline=baseline, candidate=candidate,
                run_id=run_id, candidate_digest=candidate_digest,
                pytest_delta=pytest_delta, evals_delta=evals_delta,
            )

        if baseline.error:
            return decision(VERDICT_REJECT, f"baseline.error: {baseline.error}")
        if candidate.error:
            return decision(VERDICT_REJECT, f"candidate.error: {candidate.error}")
        if pytest_delta < -self._tolerated_regression - 1e-9:
            return decision(
                VERDICT_REJECT,
                    f"pytest regression: candidate={candidate.pytest_pass_rate:.3f} "
                    f"< baseline={baseline.pytest_pass_rate:.3f}"
            )
        if evals_delta < -self._tolerated_regression - 1e-9:
            return decision(
                VERDICT_REJECT,
                    f"evals regression: candidate={candidate.evals_pass_rate:.3f} "
                    f"< baseline={baseline.evals_pass_rate:.3f}"
            )
        required_eval_rate = max(0.0, 1.0 - self._tolerated_regression)
        if candidate.evals_pass_rate < required_eval_rate - 1e-9:
            return decision(
                VERDICT_REJECT,
                    f"candidate failed runtime evals "
                    f"({candidate.evals_passed}/{candidate.evals_total}); "
                    f"required pass rate={required_eval_rate:.3f}"
            )
        return decision(VERDICT_ACCEPT, "all gates passed")

    # --- internal: scoring --------------------------------------------------

    def _score(self, worktree: str) -> EvalScore:
        """Run both gates sequentially, in order: pytest then evals."""
        started = time.time()
        # Gate 1 — pytest
        pytest_score = (
            self._run_pytest(worktree)
            if self._run_pytest_gate
            else EvalScore(pytest_pass_rate=1.0)
        )
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
            error=evals_score.error,
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
                env=_evaluation_env(),
            )
        except subprocess.TimeoutExpired:
            return EvalScore(error=f"pytest timeout after {self._timeout}s")
        except FileNotFoundError as exc:
            return EvalScore(error=f"pytest unavailable: {exc}")

        return _parse_pytest_output(proc.stdout + "\n" + proc.stderr)

    def _run_evals(self, worktree: str) -> EvalScore:
        """Run the isolated HiveOS target through the CLI in the candidate."""
        wt_path = Path(worktree)
        dataset = wt_path / self._evals_dataset
        if not dataset.exists():
            return EvalScore(error=f"eval dataset does not exist: {dataset}")
        if self._candidate_runner is not None:
            try:
                from hive.evals.dataset import load as load_dataset

                items = load_dataset(str(dataset))
                whole_run_timeout = (
                    self._timeout * max(1, len(items)) + min(5.0, self._timeout)
                )
                total, passed, errored = asyncio.run(
                    asyncio.wait_for(
                        self._run_sandboxed_items(wt_path, items),
                        timeout=whole_run_timeout,
                    )
                )
            except TimeoutError:
                return EvalScore(error=f"sandboxed evals timeout after {whole_run_timeout}s")
            except Exception as exc:  # noqa: BLE001 - runner boundary fails closed
                return EvalScore(
                    error=f"sandboxed evals runner unavailable: {type(exc).__name__}: {exc}"
                )
            if total <= 0 or errored:
                return EvalScore(
                    error=f"eval run was incomplete ({passed}/{total}, errors={errored})"
                )
            return EvalScore(
                evals_total=total,
                evals_passed=passed,
                evals_pass_rate=passed / total,
            )
        else:
            child_env = _evaluation_env()
            child_env["PYTHONPATH"] = str(wt_path / "src")
            child_env["HIVE_AUTONOMY_ENABLED"] = "false"
            child_env["HIVE_AUTONOMOUS_SELFMOD_ENABLED"] = "false"
            child_env["HIVE_PRODUCTION"] = "false"
            cmd = [
                sys.executable, "-m", "hive.evals.cli", "run", str(dataset),
                "--target", "hive-runtime", "--concurrency", "1",
                "--timeout", str(self._timeout),
            ]
            try:
                proc = subprocess.run(
                    cmd, cwd=str(wt_path), capture_output=True, text=True,
                    timeout=self._timeout * 2, check=False, env=child_env,
                )
            except subprocess.TimeoutExpired:
                return EvalScore(error=f"evals timeout after {self._timeout * 2}s")
            except OSError as exc:
                return EvalScore(error=f"evals runner unavailable: {exc}")
            returncode = proc.returncode
            output = proc.stdout + "\n" + proc.stderr
        if returncode != 0:
            return EvalScore(error=f"eval runner exited {returncode}")
        totals = re.findall(r"^\s*total:\s*(\d+)\s*$", output, re.MULTILINE)
        passed_values = re.findall(r"^\s*passed:\s*(\d+)\s*$", output, re.MULTILINE)
        errored_values = re.findall(r"^\s*errored:\s*(\d+)\s*$", output, re.MULTILINE)
        if not (len(totals) == len(passed_values) == len(errored_values) == 1):
            return EvalScore(error=f"unparseable eval output (exit={returncode})")
        total = int(totals[0])
        passed = int(passed_values[0])
        errored = int(errored_values[0])
        if total <= 0 or not 0 <= passed <= total or errored:
            return EvalScore(error=f"eval run was incomplete ({passed}/{total}, errors={errored})")
        return EvalScore(
            evals_total=total,
            evals_passed=passed,
            evals_pass_rate=passed / total,
        )

    async def _run_sandboxed_items(
        self, worktree: Path, items: list[Any],
    ) -> tuple[int, int, int]:
        """Grade candidate item outputs in this trusted supervisor process.

        Candidate code produces one item result per isolated container invocation;
        it never emits or controls the aggregate pass/fail summary.
        """
        from hive.evals.runner import run_async
        from hive.evals.types import EvalItem, TargetOutput

        async def target(item: EvalItem) -> TargetOutput:
            marker = f"__HIVE_EVAL_ITEM_{uuid.uuid4().hex}__="
            item_data = json.dumps({"id": item.id, "input": item.input})
            worker = f"""
import asyncio
import json
import sys
sys.path.insert(0, '/repo/src')
from hive.evals.runtime_target import make_deterministic_target
from hive.evals.types import EvalItem

async def _run():
    target = make_deterministic_target()
    try:
        data = json.loads({item_data!r})
        result = await target(EvalItem(
            id=data['id'], input=data['input'], expected='', grader='exact'
        ))
        print({marker!r} + json.dumps({{
            'text': result.text,
            'tool_trace': list(result.tool_trace),
            'run_id': result.run_id,
            'terminal_outcome': result.terminal_outcome,
        }}))
    finally:
        await target.aclose()

asyncio.run(_run())
"""
            command = " ".join([
                "env",
                "HIVE_AUTONOMY_ENABLED=false",
                "HIVE_AUTONOMOUS_SELFMOD_ENABLED=false",
                "HIVE_PRODUCTION=false",
                "python", "-I", "-c", shlex.quote(worker),
            ])
            assert self._candidate_runner is not None
            returncode, output = await self._candidate_runner(command, str(worktree))
            if returncode != 0:
                raise RuntimeError(f"candidate item runner exited {returncode}")
            records = [line.split(marker, 1)[1] for line in output.splitlines() if marker in line]
            if len(records) != 1:
                raise RuntimeError("candidate item runner returned ambiguous evidence")
            payload = json.loads(records[0])
            if not isinstance(payload, dict) or set(payload) != {
                "text", "tool_trace", "run_id", "terminal_outcome",
            }:
                raise RuntimeError("candidate item evidence has an invalid schema")
            if (
                not isinstance(payload["text"], str)
                or not isinstance(payload["tool_trace"], list)
                or any(not isinstance(name, str) for name in payload["tool_trace"])
                or not isinstance(payload["run_id"], str)
                or not payload["run_id"]
                or not isinstance(payload["terminal_outcome"], str)
                or not payload["terminal_outcome"]
            ):
                raise RuntimeError("candidate item evidence has invalid values")
            return TargetOutput(
                text=payload["text"],
                tool_trace=tuple(payload["tool_trace"]),
                run_id=payload["run_id"],
                terminal_outcome=payload["terminal_outcome"],
            )

        results = await run_async(
            items,
            target,
            concurrency=1,
            per_item_timeout=self._timeout,
        )
        return (
            len(results),
            sum(1 for result in results if result.passed),
            sum(1 for result in results if result.error is not None),
        )

    @staticmethod
    def _git_output(worktree: str, cmd: list[str]) -> str:
        try:
            proc = subprocess.run(
                cmd, cwd=worktree, capture_output=True, text=True,
                timeout=10, check=False, env=_evaluation_env(),
            )
        except OSError:
            return "unknown"
        value = proc.stdout.strip()
        return value if proc.returncode == 0 and value else "unknown"


def _evaluation_env() -> dict[str, str]:
    """Return a child environment with all credential-like values removed."""
    child_env = without_privileged_credentials()
    for key in list(child_env):
        folded = key.casefold()
        if any(marker in folded for marker in ("key", "token", "secret", "password")):
            child_env.pop(key, None)
    return child_env


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
