"""End-to-end integration test for the evals gate (SPRINT_6 P-B).

This is the test that proves "a failing eval blocks merge". It invokes the
**real** `hive-eval` CLI (no internal mocking) against the **real**
`evals/datasets/golden_qa.jsonl` shipped in the repo, and asserts that:

  * a passing target exits 0 (the CI green path),
  * a failing target exits 1 (the CI red path that blocks merge).

Keeping this separate from the unit tests means a regression in any one of
runner.py / dataset.py / cli.py / golden_qa.jsonl surfaces here — which is
exactly the contract a regression gate needs to enforce.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET = REPO_ROOT / "evals" / "datasets" / "golden_qa.jsonl"


def _run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run `python -m hive.evals.cli ...` in a subprocess and capture everything.

    A subprocess (rather than importing and calling main) is intentional: it
    exercises the real entry point the way CI invokes it, including argv
    parsing and exit-code propagation."""
    cmd = [sys.executable, "-m", "hive.evals.cli", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(cwd or REPO_ROOT),
        env={**os.environ, "NO_COLOR": "1", "PYTHONPATH": str(REPO_ROOT / "src")},
        timeout=60,
    )


def test_golden_qa_dataset_exists_with_30_items():
    """Sanity guard — the dataset must have exactly 30 items per the sprint
    doc acceptance criteria. If this fails, either the dataset was edited or
    the file got truncated; both need a manual decision."""
    assert DATASET.exists(), f"missing dataset: {DATASET}"
    lines = [ln for ln in DATASET.read_text().splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    assert len(lines) == 30, f"expected 30 eval items, got {len(lines)}"


def test_integration_pass_target_exits_0(tmp_path):
    """The built-in `mock` target returns `item.expected`, so every grader
    sees a perfect match — the suite must exit 0 (CI green path)."""
    rc = _run_cli("run", str(DATASET), "--target", "mock", "--quiet")
    assert rc.returncode == 0, (
        f"expected exit 0 on the passing mock target, got {rc.returncode}\n"
        f"stderr: {rc.stderr}\nstdout: {rc.stdout}"
    )


def test_integration_fail_target_exits_1(tmp_path):
    """A user-supplied target that always returns the wrong string must
    cause the gate to exit 1 — this is the "blocks merge" contract.
    Dynamic module targets require the --allow-dynamic-target opt-in."""
    bad_target = tmp_path / "always_bad.py"
    bad_target.write_text("def target(item):\n    return 'WRONG_ANSWER'\n")
    rc = _run_cli(
        "run", str(DATASET),
        "--target", f"{bad_target.stem}:target",
        "--allow-dynamic-target",
        "--quiet",
        cwd=tmp_path,  # so the dotted import resolves the temp module
    )
    assert rc.returncode == 1, (
        f"expected exit 1 on a failing target, got {rc.returncode}\n"
        f"stderr: {rc.stderr}\nstdout: {rc.stdout}"
    )


def test_integration_emit_junit_xml_artifact(tmp_path):
    """The JUnit XML output must be a well-formed file with one testsuite —
    CI systems (GitHub Actions, Jenkins, GitLab) consume this format to
    surface failures inline. Asserting the file structure keeps the contract
    honest across reporter refactors."""
    out_xml = tmp_path / "report.xml"
    rc = _run_cli(
        "run", str(DATASET),
        "--target", "mock",
        "--quiet",
        "--report", "junit_xml=report.xml",
        cwd=tmp_path,  # chdir so the relative report path resolves here
    )
    assert rc.returncode == 0
    assert out_xml.exists()
    content = out_xml.read_text()
    assert content.startswith("<?xml version=")
    assert "<testsuites>" in content
    assert "<testsuite" in content
    # 30 cases from golden_qa
    assert content.count("<testcase ") == 30


def test_integration_emit_html_artifact(tmp_path):
    """HTML report must be self-contained and include per-case detail — CI
    uploads it as a workflow artifact so reviewers can inspect failures
    without running the suite locally."""
    out_html = tmp_path / "report.html"
    rc = _run_cli(
        "run", str(DATASET),
        "--target", "mock",
        "--quiet",
        "--report", "html=report.html",
        cwd=tmp_path,
    )
    assert rc.returncode == 0
    assert out_html.exists()
    body = out_html.read_text()
    assert body.startswith("<!doctype html>")
    assert body.count("<tr") >= 30  # at least 30 rows in the results table
    assert "ALL PASSED" in body
