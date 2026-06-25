"""Tests for hive.evals.graders — base + all four graders + registry."""
from __future__ import annotations

import re

import pytest

from hive.evals.graders import (
    GRADERS,
    all_graders,
    get_grader,
    register_grader,
)
from hive.evals.graders.base import Grader, fail, normalize, pass_
from hive.evals.graders.exact import ExactGrader
from hive.evals.graders.llm_judge import LLMJudgeGrader
from hive.evals.graders.regex import RegexGrader
from hive.evals.graders.tool_trace import ToolTraceGrader
from hive.evals.types import EvalItem


def _item(grader: str, expected: str = "", extra: dict | None = None) -> EvalItem:
    return EvalItem(id="t", input="in", expected=expected, grader=grader, extra=extra or {})


# ---------- base -----------------------------------------------------------------

def test_normalize_collapses_whitespace():
    assert normalize("  hello\n\tworld  ") == "hello world"


def test_normalize_empty():
    assert normalize("") == ""


def test_pass_default_score():
    r = pass_()
    assert r.passed is True and r.score == 1.0 and r.message == ""


def test_pass_with_message_and_score():
    r = pass_("ok", 0.5)
    assert r.passed is True and r.score == 0.5 and r.message == "ok"


def test_fail_default_score():
    r = fail("nope")
    assert r.passed is False and r.score == 0.0 and r.message == "nope"


def test_fail_with_score():
    r = fail("partial", 0.3)
    assert r.passed is False and r.score == 0.3


def test_grader_protocol_runtime_checkable():
    """The Protocol is runtime_checkable so duck-typed graders work."""
    class Fake:
        name = "fake"
        def grade(self, _item, _output):
            return pass_()
    assert isinstance(Fake(), Grader)


# ---------- exact ----------------------------------------------------------------

def test_exact_passes_on_equal_strings():
    r = ExactGrader().grade(_item("exact", "hello"), "hello")
    assert r.passed is True and r.score == 1.0


def test_exact_passes_after_normalize():
    r = ExactGrader().grade(_item("exact", "hello world"), "  hello\nworld  ")
    assert r.passed is True


def test_exact_fails_on_mismatch():
    r = ExactGrader().grade(_item("exact", "hello"), "goodbye")
    assert r.passed is False and "got" in r.message


def test_exact_case_insensitive_flag():
    r = ExactGrader().grade(_item("exact", "HELLO", {"case_insensitive": True}), "hello")
    assert r.passed is True


def test_exact_long_strings_truncated_in_message():
    long_out = "x" * 100
    long_exp = "y" * 100
    r = ExactGrader().grade(_item("exact", long_exp), long_out)
    assert "…" in r.message  # preview truncation marker


# ---------- regex ----------------------------------------------------------------

def test_regex_substring_match_passes():
    r = RegexGrader().grade(_item("regex", "world"), "hello world!")
    assert r.passed is True and "substring" in r.message


def test_regex_full_match_passes():
    r = RegexGrader().grade(_item("regex", "^hello$"), "hello")
    assert r.passed is True and "full match" in r.message


def test_regex_no_match_fails():
    r = RegexGrader().grade(_item("regex", "xyz"), "abc")
    assert r.passed is False and "did not match" in r.message


def test_regex_invalid_pattern_returns_fail():
    r = RegexGrader().grade(_item("regex", "[unclosed"), "anything")
    assert r.passed is False and "invalid regex" in r.message


def test_regex_extra_pattern_overrides_expected():
    r = RegexGrader().grade(_item("regex", "ignored", {"pattern": "world"}), "hello world")
    assert r.passed is True


def test_regex_flags_as_int():
    r = RegexGrader().grade(_item("regex", "HELLO", {"flags": re.IGNORECASE}), "hello")
    assert r.passed is True


def test_regex_flags_as_csv_string():
    r = RegexGrader().grade(_item("regex", "HELLO", {"flags": "i"}), "hello")
    assert r.passed is True


def test_regex_flags_csv_with_unknown_names_ignores_them():
    r = RegexGrader().grade(_item("regex", "HELLO", {"flags": "i,xyz"}), "hello")
    assert r.passed is True


def test_regex_flags_unsupported_type_defaults_to_zero():
    r = RegexGrader().grade(_item("regex", "HELLO", {"flags": ["i"]}), "HELLO")
    assert r.passed is True  # no flag applied → case-sensitive still matches


# ---------- llm_judge -----------------------------------------------------------

def test_llm_judge_passes_when_expected_substring_present():
    item = _item("llm_judge", "interface", {"rubric": "mentions interface", "threshold": 0.5})
    r = LLMJudgeGrader().grade(item, "An API is an interface between systems.")
    assert r.passed is True and r.score == 1.0
    assert "rubric=" in r.message


def test_llm_judge_fails_when_expected_missing():
    item = _item("llm_judge", "database", {"threshold": 0.5})
    r = LLMJudgeGrader().grade(item, "this is about something else entirely")
    assert r.passed is False and r.score == 0.0


def test_llm_judge_threshold_default_is_07():
    item = _item("llm_judge", "hello")  # expected in output → score 1.0 ≥ 0.7
    r = LLMJudgeGrader().grade(item, "hello world")
    assert r.passed is True


def test_llm_judge_no_rubric_in_message():
    item = _item("llm_judge", "hello")
    r = LLMJudgeGrader().grade(item, "say hello there")
    assert "rubric=" not in r.message


def test_llm_judge_below_threshold_message():
    item = _item("llm_judge", "hello", {"threshold": 0.9})
    r = LLMJudgeGrader().grade(item, "totally unrelated text")
    assert r.passed is False
    assert "below threshold" in r.message


# ---------- tool_trace ----------------------------------------------------------

def test_tool_trace_passes_when_required_present_no_forbidden():
    item = _item("tool_trace", "", {"required_tools": ["web_get"]})
    r = ToolTraceGrader().grade(item, "tools called: web_get, summarize")
    assert r.passed is True


def test_tool_trace_fails_when_required_missing():
    item = _item("tool_trace", "", {"required_tools": ["web_get"]})
    r = ToolTraceGrader().grade(item, "tools called: summarize")
    assert r.passed is False and "missing required" in r.message


def test_tool_trace_fails_when_forbidden_called():
    item = _item("tool_trace", "", {"forbidden_tools": ["bash"]})
    r = ToolTraceGrader().grade(item, "tools called: bash, web_get")
    assert r.passed is False and "called forbidden" in r.message


def test_tool_trace_both_missing_and_forbidden_message_has_both():
    item = _item("tool_trace", "", {"required": ["x"], "required_tools": ["x"], "forbidden_tools": ["y"]})
    r = ToolTraceGrader().grade(item, "tools called: y, z")
    msg = r.message
    assert "missing required" in msg and "called forbidden" in msg


def test_tool_trace_extra_trace_overrides_inline():
    item = _item("tool_trace", "", {"_trace": ["web_get"], "required_tools": ["web_get"]})
    r = ToolTraceGrader().grade(item, "tools called: bash")  # inline ignored
    assert r.passed is True


def test_tool_trace_no_marker_no_extra_fails():
    item = _item("tool_trace", "", {"required_tools": ["web_get"]})
    r = ToolTraceGrader().grade(item, "no marker here")
    assert r.passed is False


def test_tool_trace_empty_required_and_forbidden_passes_with_score_zero():
    """When nothing is required and nothing is forbidden, the grader trivially
    passes (nothing to violate). The score formula reduces to 0/1 = 0.0 because
    of the denominator guard — that's the documented behaviour; correctness
    here is "passed", not a particular score."""
    item = _item("tool_trace", "")
    r = ToolTraceGrader().grade(item, "anything")
    assert r.passed is True
    assert r.score == 0.0


def test_tool_trace_score_clamped_between_0_and_1():
    # Required+forbidden = 2, missing+called = 0 → score = 2/2 = 1.0
    item = _item("tool_trace", "", {"required_tools": ["a", "b"], "forbidden_tools": ["c", "d"]})
    r = ToolTraceGrader().grade(item, "tools called: a, b")
    assert r.passed is True
    assert 0.0 <= r.score <= 1.0


# ---------- registry ------------------------------------------------------------

def test_get_grader_known_names():
    assert get_grader("exact").name == "exact"
    assert get_grader("regex").name == "regex"
    assert get_grader("llm_judge").name == "llm_judge"
    assert get_grader("tool_trace").name == "tool_trace"


def test_get_grader_unknown_raises_with_helpful_message():
    with pytest.raises(KeyError) as exc:
        get_grader("nope")
    assert "unknown grader" in str(exc.value)
    assert "exact" in str(exc.value)  # available list


def test_register_grader_returns_self():
    class MyGrader:
        name = "tmp_unique"
        def grade(self, item, output):  # noqa: ARG002 — protocol signature
            return pass_("custom")
    g = MyGrader()
    assert register_grader(g) is g
    assert get_grader("tmp_unique") is g
    # cleanup
    GRADERS.pop("tmp_unique", None)


def test_register_grader_empty_name_raises():
    class BadGrader:
        name = ""
        def grade(self, item, output):  # noqa: ARG002 — protocol signature
            return pass_()
    with pytest.raises(ValueError):
        register_grader(BadGrader())


def test_all_graders_iterates_registry():
    names = {g.name for g in all_graders()}
    assert {"exact", "regex", "llm_judge", "tool_trace"} <= names


# ---------- factories (exhaustive coverage of the `make()` helper) ------------

def test_exact_make_returns_instance():
    from hive.evals.graders.exact import make
    g = make()
    assert isinstance(g, ExactGrader) and g.name == "exact"


def test_regex_make_returns_instance():
    from hive.evals.graders.regex import make
    g = make()
    assert isinstance(g, RegexGrader) and g.name == "regex"


def test_llm_judge_make_returns_instance():
    from hive.evals.graders.llm_judge import make
    g = make()
    assert isinstance(g, LLMJudgeGrader) and g.name == "llm_judge"


def test_tool_trace_make_returns_instance():
    from hive.evals.graders.tool_trace import make
    g = make()
    assert isinstance(g, ToolTraceGrader) and g.name == "tool_trace"
