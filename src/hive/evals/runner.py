"""
runner.py — execute an eval dataset and collect results.

Public API:
  run_async(items, target, ...) — async core; returns a list[EvalResult]
  run(items, target, ...)       — sync wrapper around run_async

`target` may be either:
  * an async callable: async def target(item: EvalItem) -> str
  * a sync callable:    def    target(item: EvalItem) -> str

The runner enforces a per-item timeout and concurrency cap. Failures and
timeouts become EvalResults with `error` populated; the grader is still
called so that the report always shows a row for every input.

This module is stdlib + asyncio only — no HiveOS imports. The CLI (cli.py)
wraps it and the /eval/run gateway endpoint reuses the same code path.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Sequence, cast

from hive.evals.graders import get_grader
from hive.evals.types import (
    EvalItem,
    EvalReport,
    EvalResult,
    GraderResult,
)

# A target is either an async function returning str, or a sync function
# returning str. The runner auto-detects via inspect.iscoroutinefunction.
Target = Callable[[EvalItem], Awaitable[str] | str]

ProgressCb = Callable[[EvalResult], None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _invoke_target(
    target: Target,
    item: EvalItem,
    is_coro_target: bool,
    per_item_timeout: float,
) -> str:
    """Single point that dispatches sync vs async targets. Returns the output
    string; raises asyncio.TimeoutError on timeout, or any other exception
    from the target."""
    if is_coro_target:
        coro_target = cast(Callable[[EvalItem], Awaitable[str]], target)
        return await asyncio.wait_for(coro_target(item), timeout=per_item_timeout)
    sync_target = cast(Callable[[EvalItem], str], target)
    return await asyncio.wait_for(
        asyncio.to_thread(sync_target, item),
        timeout=per_item_timeout,
    )


async def run_async(
    items: Sequence[EvalItem] | Iterable[EvalItem],
    target: Target,
    *,
    concurrency: int = 4,
    per_item_timeout: float = 30.0,
    progress: ProgressCb | None = None,
) -> list[EvalResult]:
    """Execute every item against `target` and return a list of EvalResults.

    Order of returned results matches the order of `items`. Progress callback
    (if provided) is invoked once per result, in completion order."""
    materialised = list(items)
    if not materialised:
        return []
    sem = asyncio.Semaphore(max(1, concurrency))
    is_coro_target = inspect.iscoroutinefunction(target)

    async def _run_one(item: EvalItem) -> EvalResult:
        async with sem:
            start = time.monotonic()
            output = ""
            error: str | None = None
            try:
                output = await _invoke_target(target, item, is_coro_target, per_item_timeout)
            except asyncio.TimeoutError:
                error = f"timeout after {per_item_timeout}s"
            except Exception as e:  # noqa: BLE001 — runner must swallow all target errors
                error = f"{type(e).__name__}: {e}"
            duration_ms = (time.monotonic() - start) * 1000.0
            if error is not None:
                grader_result = GraderResult(passed=False, score=0.0, message=error)
            else:
                # Grader lookup / invocation can fail (unknown name, buggy
                # implementation); surface those as errored results instead
                # of crashing the whole runner.
                try:
                    grader_result = get_grader(item.grader).grade(item, output)
                except Exception as e:  # noqa: BLE001
                    error = f"grader {item.grader!r} failed: {type(e).__name__}: {e}"
                    grader_result = GraderResult(passed=False, score=0.0, message=error)
            return EvalResult(
                item=item,
                output=output,
                grader_result=grader_result,
                duration_ms=duration_ms,
                error=error,
            )

    tasks = [asyncio.create_task(_run_one(item)) for item in materialised]
    # asyncio.gather preserves submission order, so result[i] corresponds to
    # materialised[i] without needing an index map (as_completed would have
    # returned wrapper futures we couldn't track back to the originals).
    results = await asyncio.gather(*tasks)
    if progress is not None:
        for r in results:
            progress(r)
    return list(results)


def run(
    items: Sequence[EvalItem] | Iterable[EvalItem],
    target: Target,
    *,
    concurrency: int = 4,
    per_item_timeout: float = 30.0,
    progress: ProgressCb | None = None,
) -> list[EvalResult]:
    """Synchronous entry point — runs an event loop to drive run_async.

    If called from inside a running loop, the caller should use run_async
    directly. We detect this case and re-raise a clear error rather than
    crash with `asyncio.run() cannot be called from a running event loop`."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # no running loop — safe to use asyncio.run
    else:
        raise RuntimeError(
            "hive.evals.runner.run() cannot be called from inside a running "
            "event loop — use `await run_async(...)` directly."
        )
    return asyncio.run(
        run_async(
            items,
            target,
            concurrency=concurrency,
            per_item_timeout=per_item_timeout,
            progress=progress,
        )
    )


def make_report(
    items: Sequence[EvalItem] | Iterable[EvalItem],
    results: Sequence[EvalResult],
    *,
    dataset_path: str | Path,
    started_at: str,
    finished_at: str | None = None,
) -> EvalReport:
    """Build an EvalReport from raw results. `items` is used only for a length
    cross-check (raises ValueError on mismatch) — the report's authoritative
    input is `results`. Useful when the caller ran the runner out-of-process
    (e.g. the /eval/run gateway endpoint) and wants to serialise the outcome."""
    items_list = list(items)
    if len(items_list) != len(results):
        raise ValueError(
            f"items/results length mismatch: {len(items_list)} items vs "
            f"{len(results)} results — refusing to build a report"
        )
    report = EvalReport(
        dataset_path=str(dataset_path),
        started_at=started_at,
        finished_at=finished_at or _now_iso(),
        results=list(results),
    )
    report.recompute_summary()
    return report
