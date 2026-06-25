"""Tests for hive.evals.reporters — console + junit_xml + html + registry.

The JUnit-XML assertions intentionally use **string-level** checks instead of
parsing the XML with stdlib's `xml.etree.ElementTree`. Stdlib parsers are
vulnerable to XXE / billion-laughs on untrusted input (per project security
guidance); here we generate the XML ourselves, but string-based assertions
sidestep the parser entirely so the test suite never instantiates a parser on
test-shaped input.
"""
from __future__ import annotations

import io

import pytest

from hive.evals.reporters import (
    REPORTERS,
    all_reporters,
    get_reporter,
    register_reporter,
)
from hive.evals.reporters.console import ConsoleReporter, render as console_render
from hive.evals.reporters.html import HTMLReporter, render as html_render
from hive.evals.reporters.junit_xml import JUnitXMLReporter, render as junit_render
from hive.evals.types import EvalItem, EvalReport, EvalResult, GraderResult


def _item(i: str) -> EvalItem:
    return EvalItem(id=i, input="in", expected="exp", grader="exact")


def _result(i: str, *, passed: bool, error: str | None = None,
            output: str = "out", message: str = "msg") -> EvalResult:
    return EvalResult(
        item=_item(i),
        output=output,
        grader_result=GraderResult(passed=passed, score=1.0 if passed else 0.0, message=message),
        duration_ms=12.5,
        error=error,
    )


def _report(*results: EvalResult) -> EvalReport:
    rep = EvalReport(
        dataset_path="ds.jsonl",
        started_at="2026-06-25T00:00:00Z",
        finished_at="2026-06-25T00:00:01Z",
        results=list(results),
    )
    rep.recompute_summary()
    return rep


# ---------- console -------------------------------------------------------------

def test_console_writes_all_pass_marker(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    rep = _report(_result("a", passed=True))
    buf = io.StringIO()
    console_render(rep, buf)
    out = buf.getvalue()
    assert "ALL PASSED" in out
    assert "Eval report" in out


def test_console_writes_fail_marker(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    rep = _report(_result("a", passed=False))
    buf = io.StringIO()
    console_render(rep, buf)
    out = buf.getvalue()
    assert "EVALS FAILED" in out
    assert "FAIL" in out


def test_console_writes_error_marker(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    rep = _report(_result("a", passed=False, error="boom"))
    buf = io.StringIO()
    console_render(rep, buf)
    out = buf.getvalue()
    assert "ERROR" in out
    assert "boom" in out


def test_console_no_color_env_disables_ansi(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    rep = _report(_result("a", passed=True))
    buf = io.StringIO()
    console_render(rep, buf)
    assert "\033[" not in buf.getvalue()


def test_console_summary_counts(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    rep = _report(
        _result("a", passed=True),
        _result("b", passed=False),
        _result("c", passed=False, error="x"),
    )
    buf = io.StringIO()
    console_render(rep, buf)
    out = buf.getvalue()
    assert "passed:    1" in out
    assert "failed:    1" in out
    assert "errored:   1" in out
    assert "total:     3" in out


def test_console_reporter_class_matches_function():
    rep_inst = ConsoleReporter()
    assert rep_inst.name == "console"
    buf = io.StringIO()
    rep_inst.write(_report(_result("a", passed=True)), buf)
    assert "ALL PASSED" in buf.getvalue()


def test_console_default_stream_is_stdout(monkeypatch):
    """When stream=None the writer should target sys.stdout (covered line)."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    captured = io.StringIO()
    monkeypatch.setattr("sys.stdout", captured)
    # call without stream
    console_render(_report(_result("a", passed=True)))
    assert "ALL PASSED" in captured.getvalue()


def test_console_no_failures_uses_neutral_text(monkeypatch):
    """When failed==0 the console writes the count without colouring the zero."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    rep = _report(_result("a", passed=True))
    buf = io.StringIO()
    console_render(rep, buf)
    out = buf.getvalue()
    assert "failed:    0" in out
    assert "errored:   0" in out


# ---------- factories ----------------------------------------------------------

def test_console_make_returns_instance():
    from hive.evals.reporters.console import make
    r = make()
    assert isinstance(r, ConsoleReporter) and r.name == "console"


def test_junit_xml_make_returns_instance():
    from hive.evals.reporters.junit_xml import make
    r = make()
    assert isinstance(r, JUnitXMLReporter) and r.name == "junit_xml"


def test_html_make_returns_instance():
    from hive.evals.reporters.html import make
    r = make()
    assert isinstance(r, HTMLReporter) and r.name == "html"


# ---------- junit_xml -----------------------------------------------------------

def test_junit_xml_well_formed_with_passed_case(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    rep = _report(_result("a", passed=True))
    buf = io.StringIO()
    junit_render(rep, buf)
    out = buf.getvalue()
    # Structural assertions via string search (avoids stdlib XML parser — see
    # module docstring).
    assert "<testsuites>" in out
    assert 'name="ds.jsonl"' in out
    assert 'tests="1"' in out
    assert 'failures="0"' in out
    assert 'errors="0"' in out
    assert '<testcase classname="exact" name="a"' in out
    assert "<failure" not in out
    assert "<error" not in out


def test_junit_xml_failure_case_has_failure_child(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    rep = _report(_result("a", passed=False))
    buf = io.StringIO()
    junit_render(rep, buf)
    out = buf.getvalue()
    assert 'failures="1"' in out
    assert '<failure' in out
    assert 'type="AssertionError"' in out
    assert "expected:" in out


def test_junit_xml_error_case_has_error_child(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    rep = _report(_result("a", passed=False, error="boom"))
    buf = io.StringIO()
    junit_render(rep, buf)
    out = buf.getvalue()
    assert 'errors="1"' in out
    assert "<error" in out
    assert 'type="RuntimeError"' in out
    assert ">boom<" in out


def test_junit_xml_failure_with_no_message(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    rep = _report(_result("a", passed=False, message=""))
    buf = io.StringIO()
    junit_render(rep, buf)
    out = buf.getvalue()
    assert 'message="grader rejected"' in out


def test_junit_xml_includes_xml_declaration(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    rep = _report(_result("a", passed=True))
    buf = io.StringIO()
    junit_render(rep, buf)
    assert buf.getvalue().startswith('<?xml version="1.0" encoding="utf-8"?>')


def test_junit_xml_class_wrapper_matches_registry():
    r = JUnitXMLReporter()
    assert r.name == "junit_xml"


# ---------- html ----------------------------------------------------------------

def test_html_writes_self_contained_doc(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    rep = _report(_result("a", passed=True), _result("b", passed=False, error="boom"))
    buf = io.StringIO()
    html_render(rep, buf)
    out = buf.getvalue()
    assert out.lstrip().startswith("<!doctype html>")
    assert "Eval report" in out
    assert "<table>" in out
    assert "ALL PASSED" not in out  # because one failed
    assert "EVALS FAILED" in out


def test_html_escapes_user_input(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    item = EvalItem(id="<x>", input="<in>", expected="<exp>", grader="exact")
    res = EvalResult(item=item, output="<out>",
                     grader_result=GraderResult(passed=True, score=1.0),
                     duration_ms=1.0)
    rep = _report(res)
    buf = io.StringIO()
    html_render(rep, buf)
    out = buf.getvalue()
    assert "&lt;x&gt;" in out
    assert "&lt;in&gt;" in out
    assert "&lt;out&gt;" in out


def test_html_all_passed_verdict(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    rep = _report(_result("a", passed=True))
    buf = io.StringIO()
    html_render(rep, buf)
    assert "ALL PASSED" in buf.getvalue()
    assert "all-pass" in buf.getvalue()


def test_html_fail_row_renders_fail_class(monkeypatch):
    """A failing result (no error) should land in the `fail` CSS branch."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    rep = _report(_result("a", passed=False, message="bad"))
    buf = io.StringIO()
    html_render(rep, buf)
    out = buf.getvalue()
    assert 'class="fail"' in out
    assert ">FAIL<" in out
    assert "bad" in out


def test_html_class_wrapper():
    r = HTMLReporter()
    assert r.name == "html"


# ---------- registry ------------------------------------------------------------

def test_get_reporter_known_names():
    assert get_reporter("console").name == "console"
    assert get_reporter("junit_xml").name == "junit_xml"
    assert get_reporter("html").name == "html"


def test_get_reporter_unknown_raises_with_helpful_message():
    with pytest.raises(KeyError) as exc:
        get_reporter("nope")
    assert "unknown reporter" in str(exc.value)


def test_register_reporter_returns_self():
    class MyReporter:
        name = "tmp_unique_rep"
        def write(self, report, stream):
            stream.write("custom")
    r = MyReporter()
    assert register_reporter(r) is r
    assert get_reporter("tmp_unique_rep") is r
    REPORTERS.pop("tmp_unique_rep", None)


def test_register_reporter_empty_name_raises():
    class BadReporter:
        name = ""
        def write(self, report, stream):
            pass
    with pytest.raises(ValueError):
        register_reporter(BadReporter())


def test_all_reporters_iterates_registry():
    names = {r.name for r in all_reporters()}
    assert {"console", "junit_xml", "html"} <= names


def test_reporter_protocol_satisfied_by_builtins():
    from hive.evals.reporters import Reporter
    for name in ("console", "junit_xml", "html"):
        assert isinstance(get_reporter(name), Reporter)
