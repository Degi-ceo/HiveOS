"""Tests for hive.evals.runner — async + sync runner + make_report."""
from __future__ import annotations

import asyncio

import pytest

from hive.evals.graders import GRADERS
from hive.evals.runner import make_report, run, run_async
from hive.evals.types import EvalItem, EvalReport, EvalResult


def _item(i: str = "t", expected: str = "out") -> EvalItem:
    return EvalItem(id=i, input="in", expected=expected, grader="exact", extra={})


# ---------- run_async: sync target ---------------------------------------------

def test_run_async_empty_returns_empty():
    assert asyncio.run(run_async([], lambda i: "x")) == []


def test_run_async_sync_target_one_item():
    items = [_item("a", "hello")]
    results = asyncio.run(run_async(items, lambda i: "hello"))
    assert len(results) == 1
    r = results[0]
    assert r.passed is True
    assert r.output == "hello"
    assert r.error is None


def test_run_async_sync_target_failing_grader():
    items = [_item("a", "goodbye")]
    results = asyncio.run(run_async(items, lambda i: "hello"))
    assert results[0].passed is False
    assert results[0].error is None  # grader rejected, not target error


def test_run_async_sync_target_raises_caught():
    def boom(item):
        raise RuntimeError("kapow")
    results = asyncio.run(run_async([_item()], boom))
    assert results[0].error == "RuntimeError: kapow"
    assert results[0].passed is False


# ---------- run_async: async target --------------------------------------------

async def _async_target(item):
    return "async-hello"


def test_run_async_async_target():
    items = [_item("a", "async-hello")]
    results = asyncio.run(run_async(items, _async_target))
    assert results[0].passed is True
    assert results[0].output == "async-hello"


async def _async_target_raises(item):
    raise ValueError("async-boom")


def test_run_async_async_target_exception_caught():
    results = asyncio.run(run_async([_item()], _async_target_raises))
    assert results[0].error == "ValueError: async-boom"


# ---------- timeout ------------------------------------------------------------

def test_run_async_timeout_marks_error():
    def slow(item):
        import time
        time.sleep(0.5)
        return "x"
    results = asyncio.run(run_async([_item()], slow, per_item_timeout=0.05))
    assert results[0].error is not None
    assert "timeout" in results[0].error


# ---------- ordering + progress ------------------------------------------------

def test_run_async_results_preserve_input_order():
    items = [_item(f"id-{i}", f"out-{i}") for i in range(5)]
    def target(item):
        # Reverse order to confuse any non-deterministic ordering
        import time
        time.sleep(0.01 * (5 - int(item.id.split("-")[1])))
        return item.expected
    results = asyncio.run(run_async(items, target, concurrency=4))
    assert [r.item.id for r in results] == [it.id for it in items]


def test_run_async_progress_called_for_each_item():
    items = [_item(f"id-{i}", "x") for i in range(3)]
    seen: list[str] = []
    def progress(r):
        seen.append(r.item.id)
    asyncio.run(run_async(items, lambda i: "x", progress=progress))
    assert sorted(seen) == ["id-0", "id-1", "id-2"]


# ---------- concurrency cap ----------------------------------------------------

def test_run_async_concurrency_at_least_one():
    """concurrency=0 must not deadlock — the runner clamps to 1."""
    items = [_item("a", "x"), _item("b", "x")]
    results = asyncio.run(run_async(items, lambda i: "x", concurrency=0))
    assert len(results) == 2


def test_run_async_unknown_grader_becomes_error():
    # Temporarily inject an unknown grader via direct mutation of the item
    item = EvalItem(id="x", input="i", expected="x", grader="nonexistent_grader")
    results = asyncio.run(run_async([item], lambda i: "x"))
    assert results[0].error is not None
    # The runner catches grader errors through get_grader → KeyError → caught
    assert "KeyError" in results[0].error or "unknown grader" in results[0].error


# ---------- run() sync wrapper -------------------------------------------------

def test_run_from_sync_context():
    items = [_item("a", "hello")]
    results = run(items, lambda i: "hello")
    assert results[0].passed is True


def test_run_from_running_loop_raises_clear_error():
    async def inside():
        with pytest.raises(RuntimeError) as exc:
            run([_item()], lambda i: "x")
        assert "running event loop" in str(exc.value)
    asyncio.run(inside())


# ---------- make_report --------------------------------------------------------

def test_make_report_builds_with_summary():
    items = [_item("a", "hello"), _item("b", "x")]
    results = [
        EvalResult(item=items[0], output="hello",
                   grader_result=GRADERS["exact"].grade(items[0], "hello"),
                   duration_ms=1.0),
        EvalResult(item=items[1], output="y",
                   grader_result=GRADERS["exact"].grade(items[1], "y"),
                   duration_ms=1.0),
    ]
    rep = make_report(items, results, dataset_path="x.jsonl", started_at="t0")
    assert isinstance(rep, EvalReport)
    assert rep.summary.total == 2
    assert rep.summary.passed == 1
    assert rep.summary.failed == 1
    assert rep.summary.errored == 0
    assert rep.summary.all_passed is False


def test_make_report_length_mismatch_raises():
    items = [_item("a"), _item("b")]
    results = [EvalResult(item=items[0], output="x",
                          grader_result=GRADERS["exact"].grade(items[0], "x"),
                          duration_ms=1.0)]
    with pytest.raises(ValueError) as exc:
        make_report(items, results, dataset_path="x", started_at="t0")
    assert "length mismatch" in str(exc.value)


def test_make_report_uses_passed_finished_at():
    items = [_item("a", "x")]
    results = [EvalResult(item=items[0], output="x",
                          grader_result=GRADERS["exact"].grade(items[0], "x"),
                          duration_ms=1.0)]
    rep = make_report(items, results, dataset_path="p", started_at="s", finished_at="f")
    assert rep.finished_at == "f"
    assert rep.started_at == "s"


def test_make_report_no_finished_at_uses_now():
    items = [_item("a", "x")]
    results = [EvalResult(item=items[0], output="x",
                          grader_result=GRADERS["exact"].grade(items[0], "x"),
                          duration_ms=1.0)]
    rep = make_report(items, results, dataset_path="p", started_at="s")
    assert rep.finished_at != ""
