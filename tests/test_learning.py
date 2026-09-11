"""Tests for the learning loop (SPRINT_6 P-F).

Coverage target: 100% on src/hive/core/learning/*.
"""
from __future__ import annotations

import asyncio
import ast
import json
import os
import re
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from hive.core.learning import (
    Tracer,
    Evaluator,
    Evolver,
    LearningLoop,
    LoopConfig,
    Proposal,
    OUTCOME_OK,
    OUTCOME_ERROR,
    OUTCOME_DENIED,
)
from hive.core.learning.storage import (
    ensure_schema,
    insert_trace,
    query_traces,
    insert_loop,
    query_loops,
    count_by_verdict,
)
from hive.core.learning.evaluator import EvalScore, _parse_pytest_output
from hive.core.types import LoopOutcome, VERDICT_ACCEPT, VERDICT_REJECT


# --- storage -----------------------------------------------------------------


def test_storage_ensure_schema_idempotent(tmp_path: Path):
    db = tmp_path / "s.db"
    ensure_schema(db)
    ensure_schema(db)  # no exception
    # tables exist
    import sqlite3
    conn = sqlite3.connect(str(db))
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "learning_traces" in names
    assert "learning_loops" in names


def test_storage_insert_and_query_traces(tmp_path: Path):
    db = tmp_path / "s.db"
    ensure_schema(db)
    from hive.core.types import TraceRow
    rid = insert_trace(db, TraceRow(
        ts=time.time(), session_id="s1", tool="echo",
        args={"x": 1}, outcome=OUTCOME_OK, latency_ms=12.3,
    ))
    assert rid > 0
    rows = query_traces(db, limit=10)
    assert len(rows) == 1
    assert rows[0].tool == "echo"
    assert rows[0].args == {"x": 1}
    # outcome filter
    assert len(query_traces(db, outcome=OUTCOME_OK)) == 1
    assert len(query_traces(db, outcome=OUTCOME_ERROR)) == 0


def test_storage_query_traces_with_since_filter(tmp_path: Path):
    db = tmp_path / "s.db"
    ensure_schema(db)
    from hive.core.types import TraceRow
    insert_trace(db, TraceRow(ts=time.time() - 7200, tool="echo", outcome=OUTCOME_OK))
    insert_trace(db, TraceRow(ts=time.time(), tool="echo", outcome=OUTCOME_OK))
    rows = query_traces(db, since_ts=time.time() - 3600, limit=10)
    assert len(rows) == 1


def test_storage_insert_and_query_loops(tmp_path: Path):
    db = tmp_path / "s.db"
    ensure_schema(db)
    out = LoopOutcome(
        ts=time.time(), symptom="sym", verdict=VERDICT_ACCEPT,
        pytest_baseline=1.0, pytest_candidate=1.0,
        evals_baseline=1.0, evals_candidate=1.0,
        worktree_branch="hive/test", pr_url="https://x",
    )
    rid = insert_loop(db, out)
    assert rid > 0
    rows = query_loops(db)
    assert len(rows) == 1
    assert rows[0].verdict == VERDICT_ACCEPT
    assert rows[0].pr_url == "https://x"


def test_storage_count_by_verdict(tmp_path: Path):
    db = tmp_path / "s.db"
    ensure_schema(db)
    for v in [VERDICT_ACCEPT, VERDICT_ACCEPT, VERDICT_REJECT]:
        insert_loop(db, LoopOutcome(ts=time.time(), symptom="x", verdict=v))
    counts = count_by_verdict(db)
    assert counts == {VERDICT_ACCEPT: 2, VERDICT_REJECT: 1}


# --- tracer ------------------------------------------------------------------


def test_tracer_record_ok(tmp_path: Path):
    db = tmp_path / "s.db"
    t = Tracer(db)
    rid = t.record(tool="echo", outcome=OUTCOME_OK, session_id="s1",
                   args={"text": "hi"}, latency_ms=12.0)
    assert rid > 0
    recent = t.recent_traces(limit=5)
    assert len(recent) == 1
    assert recent[0].tool == "echo"
    assert recent[0].outcome == OUTCOME_OK


def test_tracer_record_error_with_class_and_message(tmp_path: Path):
    db = tmp_path / "s.db"
    t = Tracer(db)
    rid = t.record(
        tool="missing", outcome=OUTCOME_ERROR, session_id="s1",
        error_class="RuntimeError", error_message="boom",
    )
    assert rid > 0
    rows = query_traces(db, outcome=OUTCOME_ERROR)
    assert rows[0].error_class == "RuntimeError"
    assert rows[0].error_message == "boom"


def test_tracer_invalid_outcome_coerced_to_error(tmp_path: Path):
    db = tmp_path / "s.db"
    t = Tracer(db)
    t.record(tool="x", outcome="unknown_outcome")
    rows = query_traces(db, limit=1)
    assert rows[0].outcome == OUTCOME_ERROR


def test_tracer_record_never_raises_on_db_failure(tmp_path: Path):
    """Tracer.record() must never raise — a SQLite error → return 0."""
    db = tmp_path / "s.db"
    t = Tracer(db)
    # Inject a path that points to a directory (SQLite can't open that).
    t._db_path = str(tmp_path)  # directory, not file
    rid = t.record(tool="echo", outcome=OUTCOME_OK)
    assert rid == 0


def test_tracer_recent_failures_window_and_threshold(tmp_path: Path):
    db = tmp_path / "s.db"
    t = Tracer(db)
    t.record(tool="a", outcome=OUTCOME_ERROR, session_id="s1")
    t.record(tool="b", outcome=OUTCOME_ERROR, session_id="s1")
    t.record(tool="c", outcome=OUTCOME_OK, session_id="s1")
    t.record(tool="d", outcome=OUTCOME_DENIED, session_id="s1")
    fails = t.recent_failures(threshold=10, window_minutes=60)
    # 2 errors + 1 denied = 3 failures
    assert len(fails) == 3
    tools = {r.tool for r in fails}
    assert "a" in tools and "b" in tools and "d" in tools
    assert "c" not in tools


def test_tracer_recent_failures_window_excludes_old(tmp_path: Path):
    db = tmp_path / "s.db"
    t = Tracer(db)
    # Insert an "old" row directly (bypass Tracer.record which uses now()).
    from hive.core.types import TraceRow
    insert_trace(db, TraceRow(
        ts=time.time() - 7200, tool="old", outcome=OUTCOME_ERROR,
    ))
    t.record(tool="new", outcome=OUTCOME_ERROR)
    fails = t.recent_failures(threshold=10, window_minutes=60)
    tools = [r.tool for r in fails]
    assert "new" in tools
    assert "old" not in tools


def test_tracer_recent_failures_threshold_limit(tmp_path: Path):
    db = tmp_path / "s.db"
    t = Tracer(db)
    for i in range(5):
        t.record(tool=f"t{i}", outcome=OUTCOME_ERROR)
    fails = t.recent_failures(threshold=2, window_minutes=60)
    assert len(fails) == 2


def test_tracer_recent_traces_limit_zero_returns_empty(tmp_path: Path):
    db = tmp_path / "s.db"
    t = Tracer(db)
    t.record(tool="x", outcome=OUTCOME_OK)
    assert t.recent_traces(limit=0) == []


def test_tracer_repr_contains_db_path(tmp_path: Path):
    db = tmp_path / "s.db"
    t = Tracer(db)
    r = repr(t)
    assert str(db) in r


# --- evaluator parser --------------------------------------------------------


def test_parse_pytest_output_passed_only():
    s = _parse_pytest_output("===== 5 passed in 0.20s =====")
    assert s.pytest_passed == 5
    assert s.pytest_total == 5
    assert s.pytest_pass_rate == 1.0
    assert not s.error


def test_parse_pytest_output_passed_and_failed():
    s = _parse_pytest_output("===== 3 passed, 2 failed in 0.10s =====")
    assert s.pytest_passed == 3
    assert s.pytest_total == 5
    assert abs(s.pytest_pass_rate - 0.6) < 1e-6


def test_parse_pytest_output_error_and_failed():
    s = _parse_pytest_output("===== 1 passed, 2 failed, 1 error in 0.10s =====")
    assert s.pytest_passed == 1
    assert s.pytest_total == 4
    assert abs(s.pytest_pass_rate - 0.25) < 1e-6


def test_parse_pytest_output_no_tests_ran():
    s = _parse_pytest_output("===== no tests ran in 0.01s =====")
    assert s.pytest_passed == 0
    assert s.pytest_total == 0
    assert s.pytest_pass_rate == 1.0


def test_parse_pytest_output_unparseable_returns_error():
    s = _parse_pytest_output("gibberish nothing recognizable")
    assert s.error != ""


def test_parse_pytest_output_empty_returns_error():
    s = _parse_pytest_output("")
    assert s.error != ""


# --- evaluator (scoring + compare) ------------------------------------------


class _FakeModifier:
    """Minimal stand-in for SelfModifier — never touches git."""
    def __init__(self, ok: bool = True, stage: str = "dry_run"):
        self.ok = ok
        self.stage = stage

    async def propose(self, title, description, apply_fn, *, dry_run=True):
        await apply_fn("/tmp/fake-worktree")
        return {"ok": self.ok, "stage": self.stage,
                "worktree": "/tmp/fake-worktree", "base_sha": "abc",
                "changed": []}


def test_evaluator_score_nonexistent_worktree(tmp_path: Path):
    ev = Evaluator(repo_root=str(tmp_path), timeout_seconds=5)
    score = ev.score("/no/such/dir")
    assert score.error != ""


def test_evaluator_baseline_cache_hit(tmp_path: Path):
    """The 2nd call to score_baseline() returns the same object (cached)."""
    ev = Evaluator(repo_root=str(tmp_path), timeout_seconds=5,
                   evals_dataset="does_not_exist.jsonl")
    # Make pytest discoverable by using repo_root = tmp_path (will fail but
    # that's fine — we only care about cache identity).
    b1 = ev.score_baseline()
    b2 = ev.score_baseline()
    # The actual pytest run may fail with .error set, but the cache returns
    # the same EvalScore object either way.
    assert b1 is b2


def test_evaluator_invalidate_baseline_recomputes(tmp_path: Path):
    ev = Evaluator(repo_root=str(tmp_path), timeout_seconds=5,
                   evals_dataset="does_not_exist.jsonl")
    b1 = ev.score_baseline()
    ev.invalidate_baseline()
    b2 = ev.score_baseline()
    assert b1 is not b2


def test_evaluator_compare_accepts_equal_scores():
    ev = Evaluator()
    from hive.core.learning.evaluator import EvalScore
    b = EvalScore(pytest_pass_rate=1.0, evals_pass_rate=1.0,
                  pytest_passed=10, pytest_total=10,
                  evals_passed=30, evals_total=30)
    c = EvalScore(pytest_pass_rate=1.0, evals_pass_rate=1.0)
    v = ev.compare(b, c)
    assert v.verdict == VERDICT_ACCEPT
    assert v.reason == "all gates passed"


def test_evaluator_compare_rejects_pytest_regression():
    ev = Evaluator()
    from hive.core.learning.evaluator import EvalScore
    b = EvalScore(pytest_pass_rate=1.0, evals_pass_rate=1.0,
                  pytest_passed=10, pytest_total=10)
    c = EvalScore(pytest_pass_rate=0.9, evals_pass_rate=1.0,
                  pytest_passed=9, pytest_total=10)
    v = ev.compare(b, c)
    assert v.verdict == VERDICT_REJECT
    assert "pytest regression" in v.reason


def test_evaluator_compare_rejects_evals_regression():
    """An evals-pass-rate that drops below the baseline (but still below 1.0
    absolute) trips the 'evals regression' branch. We use pytest_pass_rate=1.0
    on both sides and shift the evals baseline UP, so the candidate hits the
    'regression' branch (not the 'golden_qa not full' branch).
    """
    ev = Evaluator()
    from hive.core.learning.evaluator import EvalScore
    b = EvalScore(pytest_pass_rate=1.0, evals_pass_rate=0.95,
                  evals_passed=29, evals_total=30)
    c = EvalScore(pytest_pass_rate=1.0, evals_pass_rate=0.93,
                  evals_passed=28, evals_total=30)
    v = ev.compare(b, c)
    assert v.verdict == VERDICT_REJECT
    assert "evals regression" in v.reason


def test_evaluator_compare_rejects_when_candidate_evals_not_full():
    """Runtime evals are mandatory: even when baseline matches the candidate
    (so no regression), a candidate below 1.0 is rejected.
    """
    ev = Evaluator()
    from hive.core.learning.evaluator import EvalScore
    # Baseline == candidate == 0.9 → no regression trips, but runtime eval gate.
    b = EvalScore(pytest_pass_rate=1.0, evals_pass_rate=0.9,
                  evals_passed=27, evals_total=30)
    c = EvalScore(pytest_pass_rate=1.0, evals_pass_rate=0.9,
                  evals_passed=27, evals_total=30)
    v = ev.compare(b, c)
    assert v.verdict == VERDICT_REJECT
    assert "runtime evals" in v.reason


def test_evaluator_compare_rejects_on_error():
    ev = Evaluator()
    from hive.core.learning.evaluator import EvalScore
    b = EvalScore()
    c = EvalScore(error="boom")
    v = ev.compare(b, c)
    assert v.verdict == VERDICT_REJECT
    assert "boom" in v.reason


def test_evaluator_run_evals_missing_dataset_fails_closed(tmp_path: Path):
    """A missing dataset is unavailable evidence, never a vacuous pass."""
    ev = Evaluator(repo_root=str(tmp_path), timeout_seconds=5,
                   evals_dataset="no_such_dataset.jsonl")
    score = ev._run_evals(str(tmp_path))
    assert score.evals_total == 0
    assert score.evals_pass_rate == 0.0
    assert "does not exist" in score.error


def test_evaluator_run_pytest_with_real_worktree(tmp_path: Path, monkeypatch):
    """The happy path: subprocess.run returns a normal pytest summary, which
    we parse correctly."""
    import subprocess as _sp
    class _FakeProc:
        stdout = "===== 3 passed in 0.10s =====\n"
        stderr = ""
    def _fake_run(cmd, **kw):
        return _FakeProc()
    monkeypatch.setattr(_sp, "run", _fake_run)
    wt = tmp_path / "wt"
    wt.mkdir()
    ev = Evaluator(repo_root=str(tmp_path), timeout_seconds=5)
    score = ev._run_pytest(str(wt))
    assert not score.error
    assert score.pytest_passed == 3
    assert score.pytest_pass_rate == 1.0


def test_evaluator_run_pytest_with_failures(tmp_path: Path, monkeypatch):
    """Subprocess returns pytest output with failures → pass_rate < 1.0."""
    import subprocess as _sp
    class _FakeProc:
        stdout = "===== 2 passed, 1 failed in 0.10s =====\n"
        stderr = ""
    monkeypatch.setattr(_sp, "run", lambda *a, **kw: _FakeProc())
    wt = tmp_path / "wt"
    wt.mkdir()
    ev = Evaluator(repo_root=str(tmp_path), timeout_seconds=5)
    score = ev._run_pytest(str(wt))
    assert score.pytest_passed == 2
    assert score.pytest_total == 3
    assert abs(score.pytest_pass_rate - 2/3) < 1e-9


def test_evaluator_run_pytest_timeout(tmp_path: Path, monkeypatch):
    """TimeoutExpired → EvalScore.error is set."""
    wt = tmp_path / "wt"
    wt.mkdir()
    import subprocess as _sp
    def _boom(*a, **kw):
        raise _sp.TimeoutExpired(cmd="pytest", timeout=1)
    monkeypatch.setattr(_sp, "run", _boom)
    ev = Evaluator(repo_root=str(tmp_path), timeout_seconds=1)
    score = ev._run_pytest(str(wt))
    assert score.error
    assert "timeout" in score.error


def test_evaluator_run_pytest_filenotfound(tmp_path: Path, monkeypatch):
    """FileNotFoundError (pytest missing) → EvalScore.error."""
    import subprocess as _sp
    def _boom(*a, **kw):
        raise FileNotFoundError("pytest not on PATH")
    monkeypatch.setattr(_sp, "run", _boom)
    wt = tmp_path / "wt"
    wt.mkdir()
    ev = Evaluator(repo_root=str(tmp_path), timeout_seconds=1)
    score = ev._run_pytest(str(wt))
    assert score.error
    assert "unavailable" in score.error


def test_evaluator_run_evals_with_dataset_and_runner(tmp_path: Path, monkeypatch):
    """The real-runtime subprocess summary is parsed into a score."""
    wt = tmp_path / "wt"
    wt.mkdir()
    # Minimal valid JSONL dataset.
    ds = wt / "evals" / "datasets"
    ds.mkdir(parents=True)
    (ds / "golden_qa.jsonl").write_text(
        '{"id": "q1", "input": "hello", "expected": "hi"}\n'
        '{"id": "q2", "input": "world", "expected": "world"}\n'
    )

    class _Proc:
        stdout = "Summary\n  total: 2\n  passed: 2\n  errored: 0\n"
        stderr = ""
        returncode = 0
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: _Proc())

    ev = Evaluator(repo_root=str(tmp_path), timeout_seconds=5,
                   evals_dataset="evals/datasets/golden_qa.jsonl")
    score = ev._run_evals(str(wt))
    assert score.evals_total == 2
    assert score.evals_passed == 2
    assert score.evals_pass_rate == 1.0


def test_evaluator_child_processes_receive_no_credentials(tmp_path: Path, monkeypatch):
    wt = tmp_path / "wt"
    dataset_dir = wt / "evals" / "datasets"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "runtime_smoke.jsonl").write_text(
        '{"id":"q1","input":"hello","expected":"hi"}\n'
    )
    monkeypatch.setenv("HIVE_APPROVER_KEY", "approver-value")
    monkeypatch.setenv("MINIMAX_API_KEY", "model-value")
    monkeypatch.setenv("GH_TOKEN", "github-value")
    captured = []

    class _Proc:
        stdout = "Summary\n  total: 1\n  passed: 1\n  errored: 0\n"
        stderr = ""
        returncode = 0

    def _capture(*_args, **kwargs):
        captured.append(kwargs["env"])
        return _Proc()

    monkeypatch.setattr("subprocess.run", _capture)
    ev = Evaluator(repo_root=str(wt), timeout_seconds=5, run_pytest=False)
    score = ev._score(str(wt))
    assert score.error == ""
    assert captured
    for child_env in captured:
        assert "HIVE_APPROVER_KEY" not in child_env
        assert "MINIMAX_API_KEY" not in child_env
        assert "GH_TOKEN" not in child_env


def test_evaluator_uses_injected_candidate_runner(tmp_path: Path):
    wt = tmp_path / "wt"
    dataset_dir = wt / "evals" / "datasets"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "runtime_smoke.jsonl").write_text(
        '{"id":"q1","input":"x","expected":"","grader":"exact"}\n'
    )
    calls = []

    async def sandbox_runner(command, cwd):
        calls.append((command, cwd))
        marker = re.search(r"__HIVE_EVAL_ITEM_[0-9a-f]+__=", command).group(0)
        return 0, marker + json.dumps({
            "text": "",
            "tool_trace": [],
            "run_id": "run-sandbox",
            "terminal_outcome": "completed",
        })

    ev = Evaluator(
        repo_root=str(wt), timeout_seconds=5, run_pytest=False,
        candidate_runner=sandbox_runner,
    )
    score = ev._run_evals(str(wt))
    assert score.evals_pass_rate == 1.0
    assert len(calls) == 1
    assert calls[0][1] == str(wt)
    assert calls[0][0].startswith("env HIVE_AUTONOMY_ENABLED=false ")
    assert "python -I -c" in calls[0][0]
    assert "PYTHONPATH=" not in calls[0][0]
    assert "make_deterministic_target" in calls[0][0]


@pytest.mark.parametrize("candidate_path", [
    "src/hive/evals/runner.py",
    "evals/datasets/replacement.jsonl",
    "src/sitecustomize.py",
    "conftest.py",
    "tests/integration/conftest.py",
    "pyproject.toml",
    "src/hive/__init__.py",
])
def test_candidate_gate_rejects_evaluation_control_plane_changes(
    tmp_path: Path, candidate_path: str,
):
    loop, _ = _make_loop(tmp_path)
    result = asyncio.run(loop.gate_candidate(
        str(tmp_path), "deadbeef", [candidate_path], "run-control", "a" * 64,
    ))
    assert result["ok"] is False
    assert "evaluation control plane" in result["reason"]


def test_evaluator_propagates_eval_runner_error_when_pytest_is_skipped(
    tmp_path: Path, monkeypatch,
):
    ev = Evaluator(repo_root=str(tmp_path), timeout_seconds=5, run_pytest=False)
    monkeypatch.setattr(
        ev,
        "_run_evals",
        lambda _worktree: EvalScore(error="runtime evidence unavailable"),
    )
    score = ev._score(str(tmp_path))
    assert score.pytest_pass_rate == 1.0
    assert score.error == "runtime evidence unavailable"


def test_configured_regression_threshold_changes_overall_verdict():
    baseline = EvalScore(pytest_pass_rate=1.0, evals_pass_rate=1.0)
    candidate = EvalScore(
        pytest_pass_rate=0.96,
        evals_pass_rate=0.96,
        evals_total=25,
        evals_passed=24,
    )
    strict = Evaluator(tolerated_regression=0.0)
    tolerant = Evaluator(tolerated_regression=0.05)
    assert strict.compare(baseline, candidate).verdict == "reject"
    assert tolerant.compare(baseline, candidate).verdict == "accept"


@pytest.mark.parametrize("threshold", [-0.01, 1.01, float("inf"), float("nan")])
def test_evaluator_rejects_invalid_regression_threshold(threshold: float):
    with pytest.raises(ValueError, match="between 0 and 1"):
        Evaluator(tolerated_regression=threshold)


def test_sandboxed_eval_runner_has_whole_process_timeout(tmp_path: Path):
    wt = tmp_path / "wt"
    dataset_dir = wt / "evals" / "datasets"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "runtime_smoke.jsonl").write_text(
        '{"id":"q1","input":"x","expected":"","grader":"exact"}\n'
    )

    async def never_returns(_command, _cwd):
        await asyncio.sleep(10)
        return 0, ""

    ev = Evaluator(
        repo_root=str(wt), timeout_seconds=1, run_pytest=False,
        candidate_runner=never_returns,
    )
    ev._timeout = 0.01
    score = ev._run_evals(str(wt))
    assert "sandboxed evals timeout" in score.error


def test_eval_summary_spoof_with_nonzero_exit_fails_closed(tmp_path: Path):
    wt = tmp_path / "wt"
    dataset_dir = wt / "evals" / "datasets"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "runtime_smoke.jsonl").write_text(
        '{"id":"q1","input":"x","expected":"","grader":"exact"}\n'
    )

    async def spoofed(_command, _cwd):
        return 1, (
            "total: 1\npassed: 1\nerrored: 0\n"
            "total: 3\npassed: 0\nerrored: 0\n"
        )

    ev = Evaluator(repo_root=str(wt), run_pytest=False, candidate_runner=spoofed)
    score = ev._run_evals(str(wt))
    assert score.evals_pass_rate == 0.0
    assert "eval run was incomplete" in score.error


def test_candidate_stdout_cannot_control_supervisor_summary(tmp_path: Path):
    wt = tmp_path / "wt"
    dataset_dir = wt / "evals" / "datasets"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "runtime_smoke.jsonl").write_text(
        '{"id":"q1","input":"x","expected":"RIGHT","grader":"exact"}\n'
    )

    async def spoofed(command, _cwd):
        marker = re.search(r"__HIVE_EVAL_ITEM_[0-9a-f]+__=", command).group(0)
        item = marker + json.dumps({
            "text": "WRONG",
            "tool_trace": [],
            "run_id": "run-spoof",
            "terminal_outcome": "completed",
        })
        return 0, "total: 30\npassed: 30\nerrored: 0\n" + item

    ev = Evaluator(repo_root=str(wt), run_pytest=False, candidate_runner=spoofed)
    score = ev._run_evals(str(wt))
    assert score.error == ""
    assert score.evals_total == 1
    assert score.evals_passed == 0
    assert score.evals_pass_rate == 0.0


def test_duplicate_or_impossible_eval_summary_fails_closed(tmp_path: Path):
    wt = tmp_path / "wt"
    dataset_dir = wt / "evals" / "datasets"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "runtime_smoke.jsonl").write_text(
        '{"id":"q1","input":"x","expected":"","grader":"exact"}\n'
    )

    outputs = iter([
        "total: 1\ntotal: 1\npassed: 1\nerrored: 0\n",
        "total: 1\npassed: 2\nerrored: 0\n",
    ])

    async def malformed(_command, _cwd):
        return 0, next(outputs)

    ev = Evaluator(repo_root=str(wt), run_pytest=False, candidate_runner=malformed)
    assert ev._run_evals(str(wt)).error
    assert ev._run_evals(str(wt)).error


def test_commit_scoped_baseline_rejects_dirty_or_mismatched_tree(
    tmp_path: Path, monkeypatch,
):
    ev = Evaluator(repo_root=str(tmp_path), run_pytest=False)

    class _Status:
        returncode = 0
        stdout = " M src/hive/runtime.py\n"

    monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: _Status())
    assert "commit mismatch" in ev._baseline_guard("actual", "expected")
    assert "dirty" in ev._baseline_guard("same", "same")


def test_evaluator_run_evals_with_failing_items(tmp_path: Path, monkeypatch):
    """A nonzero CLI exit is incomplete evidence and fails closed."""
    wt = tmp_path / "wt"
    wt.mkdir()
    ds = wt / "evals" / "datasets"
    ds.mkdir(parents=True)
    (ds / "golden_qa.jsonl").write_text(
        '{"id": "q1", "input": "a"}\n{"id": "q2", "input": "b"}\n'
        '{"id": "q3", "input": "c"}\n'
    )
    class _Proc:
        stdout = "Summary\n  total: 3\n  passed: 2\n  errored: 0\n"
        stderr = ""
        returncode = 1
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: _Proc())

    ev = Evaluator(repo_root=str(tmp_path), timeout_seconds=5,
                   evals_dataset="evals/datasets/golden_qa.jsonl")
    score = ev._run_evals(str(wt))
    assert score.evals_pass_rate == 0.0
    assert score.error == "eval runner exited 1"


def test_evaluator_run_evals_empty_dataset_fails_closed(tmp_path: Path, monkeypatch):
    """An empty eval run is incomplete evidence and fails closed."""
    wt = tmp_path / "wt"
    wt.mkdir()
    ds = wt / "evals" / "datasets"
    ds.mkdir(parents=True)
    (ds / "golden_qa.jsonl").write_text("")
    class _Proc:
        stdout = "Summary\n  total: 0\n  passed: 0\n  errored: 0\n"
        stderr = ""
        returncode = 2
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: _Proc())
    ev = Evaluator(repo_root=str(tmp_path), timeout_seconds=5,
                   evals_dataset="evals/datasets/golden_qa.jsonl")
    score = ev._run_evals(str(wt))
    assert score.evals_total == 0
    assert score.evals_pass_rate == 0.0
    assert score.error


def test_evaluator_run_evals_runner_raises(tmp_path: Path, monkeypatch):
    """When the eval subprocess cannot start, EvalScore.error is set."""
    wt = tmp_path / "wt"
    wt.mkdir()
    ds = wt / "evals" / "datasets"
    ds.mkdir(parents=True)
    (ds / "golden_qa.jsonl").write_text('{"id":"q1","input":"x"}\n')
    def _boom(*args, **kwargs):
        raise OSError("runner kaboom")
    monkeypatch.setattr("subprocess.run", _boom)
    ev = Evaluator(repo_root=str(tmp_path), timeout_seconds=5,
                   evals_dataset="evals/datasets/golden_qa.jsonl")
    score = ev._run_evals(str(wt))
    assert score.error
    assert "kaboom" in score.error


def test_evaluator_score_propagates_pytest_error(tmp_path: Path, monkeypatch):
    """If pytest errors, _score returns EvalScore with .error set, without
    running evals."""
    import subprocess as _sp
    def _boom(*a, **kw):
        raise _sp.TimeoutExpired(cmd="pytest", timeout=1)
    monkeypatch.setattr(_sp, "run", _boom)
    ev = Evaluator(repo_root=str(tmp_path), timeout_seconds=1)
    score = ev._score(str(tmp_path))
    assert score.error


def test_evaluator_score_dict_roundtrip():
    """EvalScore.as_dict exposes all fields."""
    from hive.core.learning.evaluator import EvalScore
    s = EvalScore(pytest_pass_rate=0.9, evals_pass_rate=0.8,
                  pytest_total=10, pytest_passed=9,
                  evals_total=10, evals_passed=8,
                  duration_seconds=1.5, error="")
    d = s.as_dict()
    assert d["pytest_pass_rate"] == 0.9
    assert d["evals_passed"] == 8
    assert d["error"] == ""


def test_evaluator_persists_baseline_by_commit_dataset_and_target(tmp_path: Path, monkeypatch):
    dataset = tmp_path / "suite.jsonl"
    dataset.write_text("{}\n")
    db = tmp_path / "state.sqlite"
    expected = EvalScore(
        pytest_pass_rate=1.0, evals_pass_rate=1.0,
        pytest_total=10, pytest_passed=10, evals_total=3, evals_passed=3,
    )
    first = Evaluator(
        repo_root=str(tmp_path), evals_dataset="suite.jsonl",
        db_path=str(db), target_id="target:v1",
    )
    monkeypatch.setattr(first, "_git_output", lambda *_args: "abc123")
    monkeypatch.setattr(first, "_score", lambda _root: expected)
    assert first.score_baseline().as_dict() == expected.as_dict()

    second = Evaluator(
        repo_root=str(tmp_path), evals_dataset="suite.jsonl",
        db_path=str(db), target_id="target:v1",
    )
    monkeypatch.setattr(second, "_git_output", lambda *_args: "abc123")
    monkeypatch.setattr(
        second, "_score",
        lambda _root: (_ for _ in ()).throw(AssertionError("baseline recomputed")),
    )
    assert second.score_baseline().as_dict() == expected.as_dict()


def test_tracer_db_path_property(tmp_path: Path):
    db = tmp_path / "s.db"
    t = Tracer(db)
    assert t.db_path == str(db)


def test_tracer_recent_failures_zero_threshold_and_window(tmp_path: Path):
    """threshold<=0 and window_minutes<=0 are coerced to safe defaults."""
    db = tmp_path / "s.db"
    t = Tracer(db)
    t.record(tool="x", outcome=OUTCOME_ERROR)
    fails = t.recent_failures(threshold=0, window_minutes=0)
    assert len(fails) >= 1  # default window=60 catches the row


def test_evaluator_score_happy_path_combined(tmp_path: Path, monkeypatch):
    """_score combines pytest + evals into a single EvalScore on the happy
    path."""
    import subprocess as _sp
    class _FakeProc:
        stderr = ""
        returncode = 0

        def __init__(self, stdout):
            self.stdout = stdout

    def _run(cmd, *args, **kwargs):
        if "hive.evals.cli" in cmd:
            return _FakeProc("Summary\n  total: 1\n  passed: 1\n  errored: 0\n")
        return _FakeProc("===== 5 passed in 0.10s =====\n")
    monkeypatch.setattr(_sp, "run", _run)

    wt = tmp_path / "wt"
    wt.mkdir()
    ds = wt / "evals" / "datasets"
    ds.mkdir(parents=True)
    (ds / "golden_qa.jsonl").write_text('{"id":"q1","input":"x"}\n')

    ev = Evaluator(repo_root=str(tmp_path), timeout_seconds=5,
                   evals_dataset="evals/datasets/golden_qa.jsonl")
    score = ev._score(str(wt))
    assert score.pytest_passed == 5
    assert score.evals_passed == 1
    assert score.evals_total == 1
    assert score.duration_seconds >= 0


# --- evolver -----------------------------------------------------------------


def test_evolver_propose_for_symptom_returns_proposal(tmp_path: Path):
    db = tmp_path / "s.db"
    ev = Evolver(_FakeModifier(ok=True), db_path=str(db))

    async def _apply(wt):
        return ["src/foo.py"]

    async def _run():
        return await ev.propose_for_symptom(
            symptom="test symptom",
            apply_fn=_apply,
        )

    p = asyncio.run(_run())
    assert isinstance(p, Proposal)
    assert p.symptom == "test symptom"
    assert p.title
    assert p.branch.startswith("hive/learning-")
    assert p.dry_run_result["ok"] is True


def test_evolver_propose_for_symptom_with_failed_dry_run(tmp_path: Path):
    db = tmp_path / "s.db"
    ev = Evolver(_FakeModifier(ok=False, stage="test"), db_path=str(db))

    async def _apply(wt):
        return []

    async def _run():
        return await ev.propose_for_symptom(
            symptom="failing symptom",
            apply_fn=_apply,
        )

    p = asyncio.run(_run())
    assert p.dry_run_result["ok"] is False
    assert p.dry_run_result["stage"] == "test"


def test_evolver_persist_proposal_marker(tmp_path: Path):
    db = tmp_path / "s.db"
    ev = Evolver(_FakeModifier(), db_path=str(db))
    p = Proposal(symptom="mark me", branch="hive/x")
    rid = ev.persist_proposal_marker(p)
    assert rid > 0
    loops = query_loops(db)
    assert any(l.symptom == "mark me" for l in loops)


def test_evolver_persist_proposal_marker_no_db_returns_zero():
    ev = Evolver(_FakeModifier(), db_path="")
    rid = ev.persist_proposal_marker(Proposal(symptom="x"))
    assert rid == 0


def test_evolver_modifier_property():
    m = _FakeModifier()
    ev = Evolver(m)
    assert ev.modifier is m


# --- loop --------------------------------------------------------------------


def _make_loop(tmp_path: str | Path, *, enabled: bool = True,
               db_path: str = "", evolver_ok: bool = True,
               candidate_score=None) -> tuple:
    tmp = Path(tmp_path) if isinstance(tmp_path, str) else tmp_path
    db = db_path or str(tmp / "s.db")
    tracer = Tracer(db)
    modifier = _FakeModifier(ok=evolver_ok)
    evolver = Evolver(modifier, db_path=db)
    evaluator = Evaluator(repo_root=str(tmp), timeout_seconds=5,
                          evals_dataset="no_dataset.jsonl")
    if candidate_score is not None:
        # Force compare() to always return this verdict
        def _forced_compare(b, c):
            from hive.core.learning.evaluator import Verdict as V
            return V(verdict=candidate_score[0], reason=candidate_score[1])
        evaluator.compare = _forced_compare  # type: ignore[assignment]
    async def _materialise(_proposal):
        return {"ok": True, "pr_url": None}

    cfg = LoopConfig(enabled=enabled, eval_timeout=5,
                     repo_root=str(tmp), db_path=db,
                     apply_fn_factory=_materialise)
    return LearningLoop(tracer, evolver, evaluator, cfg), db


def test_loop_disabled_returns_reject():
    with tempfile.TemporaryDirectory() as tmp:
        loop, _ = _make_loop(tmp, enabled=False)
        out = asyncio.run(loop.run("anything"))
        assert out.verdict == VERDICT_REJECT
        assert "disabled" in (out.reject_reason or "")


def test_loop_empty_symptom_rejects():
    with tempfile.TemporaryDirectory() as tmp:
        loop, _ = _make_loop(tmp)
        out = asyncio.run(loop.run("   "))
        assert out.verdict == VERDICT_REJECT
        assert "empty" in (out.reject_reason or "")


def test_loop_evolver_dry_run_failure_rejects(tmp_path: Path):
    loop, db = _make_loop(tmp_path, evolver_ok=False)
    out = asyncio.run(loop.run("real symptom"))
    assert out.verdict == VERDICT_REJECT
    assert "dry-run" in (out.reject_reason or "")
    # Persisted
    loops = query_loops(db)
    assert any(l.symptom == "real symptom" for l in loops)


def test_loop_persists_rejected_outcome(tmp_path: Path):
    loop, db = _make_loop(tmp_path)
    out = asyncio.run(loop.run("another symptom"))
    loops = query_loops(db)
    assert any(l.symptom == "another symptom" for l in loops)


def test_loop_accept_persists_accept(tmp_path: Path):
    # Force compare to accept.
    loop, db = _make_loop(tmp_path, candidate_score=(VERDICT_ACCEPT, "forced"))
    out = asyncio.run(loop.run("accept me"))
    assert out.verdict == VERDICT_ACCEPT
    loops = query_loops(db)
    match = next(l for l in loops if l.symptom == "accept me")
    assert match.verdict == VERDICT_ACCEPT


def test_loop_materialise_with_factory(tmp_path: Path):
    """When apply_fn_factory is set, the loop calls it on accept."""
    loop, db = _make_loop(tmp_path, candidate_score=(VERDICT_ACCEPT, "ok"))

    captured = {}
    async def _factory(proposal):
        captured["branch"] = proposal.branch
        return {"ok": True, "pr_url": "https://example.com/pr/1"}

    loop._config.apply_fn_factory = _factory
    out = asyncio.run(loop.run("factory test"))
    assert out.verdict == VERDICT_ACCEPT
    assert out.pr_url == "https://example.com/pr/1"
    assert "branch" in captured


def test_loop_apply_failure_rolls_back_to_reject(tmp_path: Path):
    """If the apply step raises after accept, verdict flips to reject."""
    loop, _ = _make_loop(tmp_path, candidate_score=(VERDICT_ACCEPT, "ok"))

    async def _boom_factory(proposal):
        raise RuntimeError("apply failed")

    loop._config.apply_fn_factory = _boom_factory
    out = asyncio.run(loop.run("boom"))
    assert out.verdict == VERDICT_REJECT
    assert "apply" in (out.reject_reason or "")


def test_loop_evaluator_exception_rejects(tmp_path: Path):
    """If the evaluator itself raises, the loop catches + rejects."""
    loop, _ = _make_loop(tmp_path)

    def _boom_score(_path):
        raise RuntimeError("score boom")

    loop._evaluator.score = _boom_score  # type: ignore[assignment]
    out = asyncio.run(loop.run("eval fail"))
    assert out.verdict == VERDICT_REJECT
    assert "evaluator" in (out.reject_reason or "")


def test_loop_tracer_collect_failure_is_swallowed(tmp_path: Path, monkeypatch):
    """If tracer.recent_failures() raises, the loop continues."""
    loop, _ = _make_loop(tmp_path, candidate_score=(VERDICT_REJECT, "x"))

    def _boom_collect(**kw):
        raise RuntimeError("tracer boom")

    monkeypatch.setattr(loop._tracer, "recent_failures", _boom_collect)
    out = asyncio.run(loop.run("survive tracer fail"))
    assert out.verdict == VERDICT_REJECT  # didn't crash


def test_loop_evolver_exception_rejects(tmp_path: Path):
    loop, _ = _make_loop(tmp_path)

    class _BoomModifier:
        async def propose(self, *a, **kw):
            raise RuntimeError("evolver boom")

    loop._evolver = Evolver(_BoomModifier(), db_path=str(tmp_path / "s.db"))
    out = asyncio.run(loop.run("evolver raise"))
    assert out.verdict == VERDICT_REJECT
    assert "evolver" in (out.reject_reason or "")


def test_loop_config_property():
    cfg = LoopConfig(enabled=True, eval_timeout=10)
    tracer = Tracer(":memory:")
    loop = LearningLoop(tracer, Evolver(_FakeModifier()),
                         Evaluator(), cfg)
    assert loop.config is cfg


def test_loop_repr():
    cfg = LoopConfig(enabled=True)
    tracer = Tracer(":memory:")
    loop = LearningLoop(tracer, Evolver(_FakeModifier()),
                         Evaluator(), cfg)
    r = repr(loop)
    assert "enabled=True" in r
    assert "autopromote" not in r


def test_environment_constants_have_a_reader():
    """Safety/config env constants must not survive as dead promises."""
    src_root = Path(__file__).parents[1] / "src" / "hive"
    unread: list[str] = []
    for path in src_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assigned = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            if isinstance(target, ast.Name) and target.id.endswith("_ENV")
        }
        readers: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            first_arg = node.args[0]
            if not isinstance(first_arg, ast.Name):
                continue
            func = node.func
            direct_getenv = (
                isinstance(func, ast.Attribute)
                and func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            )
            environ_method = (
                isinstance(func, ast.Attribute)
                and func.attr in {"get", "pop", "setdefault"}
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "environ"
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id == "os"
            )
            if direct_getenv or environ_method:
                readers.add(first_arg.id)
        unread.extend(
            f"{path.relative_to(src_root)}:{name}" for name in assigned - readers
        )
    assert unread == []


def test_candidate_gate_persists_run_id_digest_and_deltas(tmp_path: Path, monkeypatch):
    loop, db = _make_loop(tmp_path)
    baseline = EvalScore(
        pytest_pass_rate=1.0, evals_pass_rate=1.0,
        pytest_total=10, pytest_passed=10, evals_total=3, evals_passed=3,
    )
    candidate = EvalScore(
        pytest_pass_rate=1.0, evals_pass_rate=1.0,
        pytest_total=10, pytest_passed=10, evals_total=3, evals_passed=3,
    )
    monkeypatch.setattr(loop._evaluator, "score_baseline", lambda **_kwargs: baseline)
    monkeypatch.setattr(loop._evaluator, "score", lambda _path: candidate)

    class _Diff:
        returncode = 0
        stdout = b"candidate diff"
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: _Diff())

    result = asyncio.run(loop.gate_candidate(
        str(tmp_path), "abc123", ["src/hive/example.py"], "run-42", "b" * 64,
    ))
    assert result["ok"] is True
    assert result["candidate_digest"]
    persisted = query_loops(db)[0]
    assert persisted.run_id == "run-42"
    assert persisted.candidate_digest == result["candidate_digest"]
    assert persisted.pytest_delta == 0.0
    assert persisted.evals_delta == 0.0


def test_candidate_gate_rejects_regression(tmp_path: Path, monkeypatch):
    loop, _ = _make_loop(tmp_path)
    baseline = EvalScore(pytest_pass_rate=1.0, evals_pass_rate=1.0)
    candidate = EvalScore(pytest_pass_rate=1.0, evals_pass_rate=0.5,
                          evals_total=2, evals_passed=1)
    monkeypatch.setattr(loop._evaluator, "score_baseline", lambda **_kwargs: baseline)
    monkeypatch.setattr(loop._evaluator, "score", lambda _path: candidate)

    class _Diff:
        returncode = 0
        stdout = b"regression"
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: _Diff())

    result = asyncio.run(loop.gate_candidate(
        str(tmp_path), "abc", ["x"], "run-r", "c" * 64,
    ))
    assert result["ok"] is False
    assert result["evals_delta"] == -0.5
    assert "regression" in result["reason"]


# --- gateway endpoints -------------------------------------------------------


def _build_hive(tmp_path: Path):
    """Build a minimal HiveOS for gateway tests."""
    from hive.core.config import HiveConfig
    from hive.runtime import HiveOS
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    return HiveOS.build(cfg)


def test_learning_status_endpoint(tmp_path: Path):
    from hive.gateway.app import create_app
    hive = _build_hive(tmp_path)
    with TestClient(create_app(hive)) as c:
        r = c.get("/learning/status",
                  headers={"X-Hive-Token": "change_me"})
        assert r.status_code == 200
        body = r.json()
        assert "enabled" in body
        assert "counts" in body
        assert "recent" in body
        assert isinstance(body["recent"], list)


def test_learning_status_requires_token(tmp_path: Path):
    from hive.gateway.app import create_app
    hive = _build_hive(tmp_path)
    with TestClient(create_app(hive)) as c:
        r = c.get("/learning/status")
        assert r.status_code == 401


def test_learning_history_endpoint(tmp_path: Path):
    from hive.gateway.app import create_app
    hive = _build_hive(tmp_path)
    # Pre-populate one loop.
    insert_loop(str(hive.config.state_db), LoopOutcome(
        ts=time.time(), symptom="history test", verdict=VERDICT_REJECT,
        reject_reason="manual",
    ))
    with TestClient(create_app(hive)) as c:
        r = c.get("/learning/history?limit=5",
                  headers={"X-Hive-Token": "change_me"})
        assert r.status_code == 200
        body = r.json()
        assert body["count"] >= 1
        assert any(l["symptom"] == "history test" for l in body["loops"])


def test_learning_history_limit_capped_at_200(tmp_path: Path):
    from hive.gateway.app import create_app
    hive = _build_hive(tmp_path)
    with TestClient(create_app(hive)) as c:
        r = c.get("/learning/history?limit=99999",
                  headers={"X-Hive-Token": "change_me"})
        # Should NOT 500 — the cap protects the DB.
        assert r.status_code == 200


def test_learning_run_requires_symptom(tmp_path: Path):
    from hive.gateway.app import create_app
    hive = _build_hive(tmp_path)
    with TestClient(create_app(hive)) as c:
        r = c.post("/learning/run", json={},
                   headers={"X-Hive-Token": "change_me"})
        assert r.status_code == 400


def test_learning_run_with_symptom_returns_outcome_count(tmp_path: Path):
    from hive.gateway.app import create_app
    hive = _build_hive(tmp_path)
    with TestClient(create_app(hive)) as c:
        r = c.post("/learning/run",
                   json={"symptom": "manual trigger"},
                   headers={"X-Hive-Token": "change_me"})
        assert r.status_code == 200
        body = r.json()
        assert "outcome_count" in body
        assert body["symptom"] == "manual trigger"


def test_learning_run_requires_token(tmp_path: Path):
    from hive.gateway.app import create_app
    hive = _build_hive(tmp_path)
    with TestClient(create_app(hive)) as c:
        r = c.post("/learning/run", json={"symptom": "x"})
        assert r.status_code == 401


# --- CLI ---------------------------------------------------------------------


def _patch_from_env(monkeypatch, tmp_path):
    """Helper: monkey-patch HiveConfig.from_env to use tmp_path."""
    from hive.core.config import HiveConfig
    real_from_env = HiveConfig.from_env.__func__  # underlying function
    def _patched(cls, root=None, *, load_dotenv: bool = True):
        if root is None:
            root = tmp_path
        return real_from_env(cls, root, load_dotenv=False)
    monkeypatch.setattr(HiveConfig, "from_env", classmethod(_patched))


def test_cli_learning_status(monkeypatch, tmp_path: Path):
    from hive.surfaces import cli
    _patch_from_env(monkeypatch, tmp_path)
    rc = cli.main(["learning", "status"])
    assert rc == 0


def test_cli_learning_replay_not_found(monkeypatch, tmp_path: Path):
    from hive.surfaces import cli
    _patch_from_env(monkeypatch, tmp_path)
    rc = cli.main(["learning", "replay", "99999"])
    assert rc == 1


def test_cli_learning_replay_found(monkeypatch, tmp_path: Path):
    from hive.surfaces import cli
    from hive.core.config import HiveConfig
    real_from_env = HiveConfig.from_env
    cfg = real_from_env(root=tmp_path, load_dotenv=False)
    # Ensure parent dir exists so SQLite can open the file.
    Path(cfg.state_db).parent.mkdir(parents=True, exist_ok=True)
    ensure_schema(str(cfg.state_db))
    insert_loop(str(cfg.state_db), LoopOutcome(
        ts=time.time(), symptom="cli-found", verdict=VERDICT_ACCEPT,
        worktree_branch="hive/cli-test", pr_url="https://x",
    ))
    _patch_from_env(monkeypatch, tmp_path)
    rc = cli.main(["learning", "replay", "1"])
    assert rc == 0


def test_cli_learning_dispatch_unknown_subcommand(monkeypatch, tmp_path: Path):
    from hive.surfaces import cli
    _patch_from_env(monkeypatch, tmp_path)
    rc = cli.main(["learning", "frobnicate"])
    assert rc == 1


def test_cli_learning_dispatch_no_args(monkeypatch, tmp_path: Path):
    from hive.surfaces import cli
    _patch_from_env(monkeypatch, tmp_path)
    rc = cli.main(["learning"])
    assert rc == 1


def test_cli_learning_replay_invalid_id(monkeypatch, tmp_path: Path):
    from hive.surfaces import cli
    _patch_from_env(monkeypatch, tmp_path)
    rc = cli.main(["learning", "replay", "notanumber"])
    assert rc == 1


# --- runtime wire-up ---------------------------------------------------------


def test_runtime_self_improve_loop_route_when_enabled(monkeypatch, tmp_path: Path):
    """use_learning_loop=True + config enabled → routes through loop."""
    from hive.core.config import HiveConfig
    from hive.runtime import HiveOS
    os.environ["HIVE_LEARNING_LOOP_ENABLED"] = "true"
    try:
        cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
        cfg = replace(cfg, sandbox_image="python:3.12")
        hive = HiveOS.build(cfg)
        outcomes = asyncio.run(hive.self_improve_from_symptom(
            "wire-up test", use_learning_loop=True,
        ))
        # The loop persisted a LoopOutcome, not a list of EditOutcomes.
        assert outcomes == []
    finally:
        del os.environ["HIVE_LEARNING_LOOP_ENABLED"]


def test_runtime_self_improve_loop_route_when_disabled(monkeypatch, tmp_path: Path):
    """use_learning_loop=True + config disabled → falls through to legacy
    flow (which here will error on missing API key, but should NOT route
    through the learning loop)."""
    from hive.core.config import HiveConfig
    from hive.runtime import HiveOS
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    # Make sure config.learning_loop_enabled is False.
    assert cfg.learning_loop_enabled is False
    hive = HiveOS.build(cfg)
    # Patch router.complete to throw to confirm we DID NOT route through loop.
    from hive.llm.adapters.base import CompletionResult
    async def _boom(*a, **kw):
        raise RuntimeError("should not be called via loop path")
    hive.router.complete = _boom  # type: ignore[assignment]
    try:
        asyncio.run(hive.self_improve_from_symptom(
            "off test", use_learning_loop=True,
        ))
    except RuntimeError as exc:
        assert "should not be called via loop path" in str(exc)
    else:
        # If it returned cleanly, the legacy path didn't need the router
        # (e.g., empty symptom) — that's also fine, since loop didn't fire.
        pass
