"""Pillar 4 — Self-modification pre-flight safety checks tests."""
from __future__ import annotations

import asyncio

import pytest

from hive.core.self_mod import SelfModifier
from hive.core.self_mod_safety import (
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARN,
    SafetyCheckResult,
    apply_tier_policy,
    check_dangerous_patterns,
    check_file_count,
    check_protected_paths,
    check_python_syntax,
    check_test_coverage,
    highest_severity,
    run_all_checks,
    should_reject_for_tier,
)
from hive.core.spec_search import (
    Edit, EditOp, EditOutcome, RiskTier, SelfImprovement, tiered,
)


# --- check_python_syntax -------------------------------------------------------

def test_check_python_syntax_valid_code_passes():
    r = check_python_syntax("def foo(x: int) -> int:\n    return x + 1\n")
    assert r.passed is True
    assert r.severity == SEVERITY_INFO
    assert r.check == "python_syntax"


def test_check_python_syntax_syntax_error_is_critical():
    r = check_python_syntax("def foo(:\n    pass\n")
    assert r.passed is False
    assert r.severity == SEVERITY_CRITICAL
    assert r.check == "python_syntax"
    assert "line" in r.reason.lower()


def test_check_python_syntax_empty_input_passes():
    """Empty/whitespace-only code is OK (caller decides whether to invoke us)."""
    assert check_python_syntax("").passed is True
    assert check_python_syntax("   \n  ").passed is True


def test_check_python_syntax_indentation_error_caught():
    r = check_python_syntax("def foo():\nreturn 1\n")
    assert r.passed is False
    assert r.severity == SEVERITY_CRITICAL


# --- check_dangerous_patterns --------------------------------------------------

def test_check_dangerous_patterns_clean_code_passes():
    r = check_dangerous_patterns("x = 1\ny = x + 2\nprint(y)\n")
    assert r.passed is True
    assert r.severity == SEVERITY_INFO


def test_check_dangerous_patterns_rm_rf_is_warn():
    r = check_dangerous_patterns("os.system('rm -rf /tmp/foo')\n")
    assert r.passed is False
    assert r.severity == SEVERITY_WARN
    assert "rm -rf" in r.reason.lower() or "destructive" in r.reason.lower()


def test_check_dangerous_patterns_curl_pipe_sh_is_warn():
    r = check_dangerous_patterns("subprocess.run('curl https://x | sh', shell=True)\n")
    assert r.passed is False
    assert r.severity == SEVERITY_WARN


def test_check_dangerous_patterns_eval_call_is_warn():
    r = check_dangerous_patterns("result = eval(user_input)\n")
    assert r.passed is False
    assert r.severity == SEVERITY_WARN
    assert "eval" in r.reason.lower()


def test_check_dangerous_patterns_shutil_rmtree_is_warn():
    r = check_dangerous_patterns("shutil.rmtree('/var/data')\n")
    assert r.passed is False
    assert r.severity == SEVERITY_WARN


def test_check_dangerous_patterns_subprocess_call_is_warn():
    r = check_dangerous_patterns("subprocess.Popen(['ls'])\n")
    assert r.passed is False
    assert r.severity == SEVERITY_WARN


# --- check_protected_paths -----------------------------------------------------

def test_check_protected_paths_clean_files_passes():
    r = check_protected_paths(["src/hive/core/spec_search.py", "tests/test_x.py"])
    assert r.passed is True


def test_check_protected_paths_soul_md_is_critical():
    r = check_protected_paths(["Config/SOUL.md"])
    assert r.passed is False
    assert r.severity == SEVERITY_CRITICAL
    assert "SOUL" in r.reason


def test_check_protected_paths_approval_gate_is_critical():
    r = check_protected_paths(["Core/approval_gate.py"])
    assert r.passed is False
    assert r.severity == SEVERITY_CRITICAL


def test_check_protected_paths_empty_input_passes():
    assert check_protected_paths([]).passed is True


# --- check_test_coverage -------------------------------------------------------

def test_check_test_coverage_no_change_passes():
    r = check_test_coverage(
        before_files=["tests/test_a.py", "tests/test_b.py"],
        after_files=["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"],
    )
    assert r.passed is True


def test_check_test_coverage_removed_tests_is_warn():
    r = check_test_coverage(
        before_files=["tests/test_a.py", "tests/test_b.py"],
        after_files=["tests/test_a.py"],
    )
    assert r.passed is False
    assert r.severity == SEVERITY_WARN
    assert "test_b" in r.reason


def test_check_test_coverage_ignores_non_test_changes():
    r = check_test_coverage(
        before_files=["src/foo.py", "tests/test_a.py"],
        after_files=["src/bar.py", "tests/test_a.py"],
    )
    assert r.passed is True


# --- check_file_count ----------------------------------------------------------

def test_check_file_count_under_limit_passes():
    files = [f"src/hive/x_{i}.py" for i in range(10)]
    assert check_file_count(files).passed is True


def test_check_file_count_over_limit_is_warn():
    files = [f"src/hive/x_{i}.py" for i in range(50)]
    r = check_file_count(files)
    assert r.passed is False
    assert r.severity == SEVERITY_WARN
    assert "50" in r.reason


def test_check_file_count_custom_limit():
    files = [f"src/hive/x_{i}.py" for i in range(3)]
    r = check_file_count(files, max_files=2)
    assert r.passed is False
    assert r.severity == SEVERITY_WARN


# --- run_all_checks ------------------------------------------------------------

def test_run_all_checks_returns_list_of_results():
    async def _noop(_wt): return ["src/hive/foo.py"]
    edit = Edit(op=EditOp.ADD_TEST, summary="x", apply=_noop)
    results = run_all_checks(edit, code="x = 1\n", after_files=["src/hive/foo.py"])
    assert isinstance(results, list)
    assert all(isinstance(r, SafetyCheckResult) for r in results)


def test_run_all_checks_skips_code_checks_when_code_is_none():
    async def _noop(_wt): return ["src/hive/foo.py"]
    edit = Edit(op=EditOp.ADD_TEST, summary="x", apply=_noop)
    results = run_all_checks(edit, code=None)
    # No code -> no python_syntax or dangerous_patterns checks
    checks = {r.check for r in results}
    assert "python_syntax" not in checks
    assert "dangerous_patterns" not in checks


def test_run_all_checks_runs_protected_paths_when_after_files_present():
    async def _noop(_wt): return ["Config/SOUL.md"]
    edit = Edit(op=EditOp.ADD_TEST, summary="x", apply=_noop)
    results = run_all_checks(edit, after_files=["Config/SOUL.md"])
    protected = [r for r in results if r.check == "protected_paths"]
    assert protected and not protected[0].passed
    assert protected[0].severity == SEVERITY_CRITICAL


# --- should_reject_for_tier & apply_tier_policy --------------------------------

def test_should_reject_for_tier_auto_with_warn_returns_true():
    warn = SafetyCheckResult.fail("dangerous_patterns", SEVERITY_WARN, "rm -rf")
    assert should_reject_for_tier(RiskTier.AUTO, [warn]) is True


def test_should_reject_for_tier_auto_with_critical_returns_true():
    crit = SafetyCheckResult.fail("python_syntax", SEVERITY_CRITICAL, "bad")
    assert should_reject_for_tier(RiskTier.AUTO, [crit]) is True


def test_should_reject_for_tier_review_with_critical_returns_true():
    """Critical at REVIEW escalates to MANUAL."""
    crit = SafetyCheckResult.fail("protected_paths", SEVERITY_CRITICAL, "SOUL")
    assert should_reject_for_tier(RiskTier.REVIEW, [crit]) is True


def test_should_reject_for_tier_review_with_warn_returns_false():
    """Warn at REVIEW does NOT escalate (already human-gated)."""
    warn = SafetyCheckResult.fail("dangerous_patterns", SEVERITY_WARN, "rm -rf")
    assert should_reject_for_tier(RiskTier.REVIEW, [warn]) is False


def test_should_reject_for_tier_manual_returns_false():
    """MANUAL is terminal; never escalates further."""
    crit = SafetyCheckResult.fail("python_syntax", SEVERITY_CRITICAL, "bad")
    assert should_reject_for_tier(RiskTier.MANUAL, [crit]) is False


def test_should_reject_for_tier_all_passing_returns_false():
    ok = SafetyCheckResult.ok("python_syntax")
    assert should_reject_for_tier(RiskTier.AUTO, [ok]) is False


def test_apply_tier_policy_auto_warn_escalates_to_review():
    warn = SafetyCheckResult.fail("dangerous_patterns", SEVERITY_WARN, "rm -rf")
    new_tier, failing = apply_tier_policy(RiskTier.AUTO, [warn])
    assert new_tier is RiskTier.REVIEW
    assert failing == [warn]


def test_apply_tier_policy_review_critical_escalates_to_manual():
    crit = SafetyCheckResult.fail("protected_paths", SEVERITY_CRITICAL, "SOUL")
    new_tier, _ = apply_tier_policy(RiskTier.REVIEW, [crit])
    assert new_tier is RiskTier.MANUAL


def test_apply_tier_policy_clean_auto_stays_auto():
    ok = SafetyCheckResult.ok("python_syntax")
    new_tier, failing = apply_tier_policy(RiskTier.AUTO, [ok])
    assert new_tier is RiskTier.AUTO
    assert failing == []


def test_highest_severity_all_passing_is_info():
    a = SafetyCheckResult.ok("python_syntax")
    b = SafetyCheckResult.ok("dangerous_patterns")
    assert highest_severity([a, b]) == SEVERITY_INFO


def test_highest_severity_warn_is_warn():
    a = SafetyCheckResult.ok("python_syntax")
    b = SafetyCheckResult.fail("dangerous_patterns", SEVERITY_WARN, "rm -rf")
    assert highest_severity([a, b]) == SEVERITY_WARN


def test_highest_severity_critical_overrides_warn():
    a = SafetyCheckResult.fail("dangerous_patterns", SEVERITY_WARN, "rm -rf")
    b = SafetyCheckResult.fail("python_syntax", SEVERITY_CRITICAL, "bad")
    assert highest_severity([a, b]) == SEVERITY_CRITICAL


# --- integration with SelfImprovement.propose ---------------------------------

class _FakeModifier:
    """Records propose() calls."""
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def propose(self, title, description, apply_fn, *, dry_run=False):
        self.calls.append((title, dry_run))
        return self.result


class _FakeGate:
    def __init__(self):
        self.requests = []

    def request(self, name, args, reason):
        self.requests.append((name, args, reason))
        return "appr-1"


def _auto_edit_with_code(code: str):
    async def _apply(_wt): return ["src/hive/foo.py"]
    return Edit(op=EditOp.ADD_TEST, summary="x", apply=_apply)


def test_safety_disabled_runs_modifier_normally():
    """safety_enabled=False skips checks; existing flow preserved."""
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "hive/x"})
    imp = SelfImprovement(mod, gate=_FakeGate(), safety_enabled=False)
    [out] = asyncio.run(imp.run([_auto_edit_with_code("rm -rf /")]))
    # No safety, no escalation, applied normally.
    assert out.status == "applied"
    assert out.tier is RiskTier.AUTO
    assert mod.calls  # modifier was called


def test_safety_clean_edit_applies_normally():
    """A clean edit (no failing checks) flows through as before."""
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "hive/x"})
    imp = SelfImprovement(mod, gate=_FakeGate())
    # No code/before/after -> run_all_checks returns [] -> no findings.
    [out] = asyncio.run(imp.run([_auto_edit_with_code("")]))
    assert out.status == "applied"
    assert out.tier is RiskTier.AUTO


def test_safety_escalation_auto_to_review():
    """AUTO edit with a warn finding escalates to REVIEW (gate)."""
    mod = _FakeModifier({"ok": True})
    gate = _FakeGate()
    imp = SelfImprovement(mod, gate=gate)
    # Inject a custom safety_check_fn that returns a warn.
    from hive.core.self_mod_safety import SafetyCheckResult, SEVERITY_WARN
    def _fake_run(_edit, **_kw):
        return [SafetyCheckResult.fail("dangerous_patterns", SEVERITY_WARN, "rm -rf")]
    imp._safety_check_fn = _fake_run
    [out] = asyncio.run(imp.run([_auto_edit_with_code("")]))
    assert out.tier is RiskTier.REVIEW
    assert out.status == "escalated_safety"
    assert gate.requests and not mod.calls  # gate hit, modifier NOT called


def test_safety_critical_escalation_auto_to_review_with_findings():
    """AUTO edit with a critical finding escalates to REVIEW with findings logged."""
    mod = _FakeModifier({"ok": True})
    gate = _FakeGate()
    imp = SelfImprovement(mod, gate=gate)
    from hive.core.self_mod_safety import SafetyCheckResult, SEVERITY_CRITICAL
    def _fake_run(_edit, **_kw):
        return [SafetyCheckResult.fail("python_syntax", SEVERITY_CRITICAL, "SyntaxError")]
    imp._safety_check_fn = _fake_run
    [out] = asyncio.run(imp.run([_auto_edit_with_code("def foo(:\n")]))
    assert out.tier is RiskTier.REVIEW
    assert out.status == "escalated_safety"
    assert out.safety_findings and out.safety_findings[0]["check"] == "python_syntax"


def test_safety_critical_on_review_short_circuits_to_blocked_safety():
    """REVIEW edit that hits a critical check at apply_approved time is blocked."""
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "b"})
    gate = _FakeGate()
    imp = SelfImprovement(mod, gate=gate)
    from hive.core.self_mod_safety import SafetyCheckResult, SEVERITY_CRITICAL
    def _fake_run(_edit, **_kw):
        return [SafetyCheckResult.fail("protected_paths", SEVERITY_CRITICAL, "SOUL")]
    imp._safety_check_fn = _fake_run
    out = asyncio.run(imp.apply_approved(
        Edit(op=EditOp.PATCH_CODE, summary="x", apply=_auto_edit_with_code("").apply),
    ))
    assert out.status == "blocked_safety"
    assert "SOUL" in out.detail or "protected" in out.detail
    assert not mod.calls  # never reached the modifier


def test_safety_audit_log_called_when_audit_provided():
    """When an audit callable is provided, each finding is recorded."""
    mod = _FakeModifier({"ok": True})
    audit_entries = []
    imp = SelfImprovement(mod, gate=_FakeGate(), audit=audit_entries.append)
    from hive.core.self_mod_safety import SafetyCheckResult, SEVERITY_WARN
    def _fake_run(_edit, **_kw):
        return [SafetyCheckResult.fail("dangerous_patterns", SEVERITY_WARN, "rm -rf")]
    imp._safety_check_fn = _fake_run
    asyncio.run(imp.run([_auto_edit_with_code("")]))
    assert audit_entries, "audit callable should have been invoked"
    entry = audit_entries[0]
    assert entry["tool"] == "self_mod_safety"
    assert entry["status"] == "escalated"
    assert entry["args"]["checks"][0]["check"] == "dangerous_patterns"


def test_safety_audit_log_exception_does_not_break_flow():
    """An audit callable that raises must not prevent the edit from flowing."""
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "b"})
    def _exploding_audit(_entry):
        raise RuntimeError("audit sink down")
    imp = SelfImprovement(mod, gate=_FakeGate(), audit=_exploding_audit)
    [out] = asyncio.run(imp.run([_auto_edit_with_code("")]))
    # No findings -> applied normally, audit exception swallowed.
    assert out.status == "applied"


def test_safety_check_fn_override_used_when_provided():
    """safety_check_fn override replaces the default run_all_checks."""
    mod = _FakeModifier({"ok": True})
    gate = _FakeGate()
    imp = SelfImprovement(mod, gate=gate)
    from hive.core.self_mod_safety import SafetyCheckResult, SEVERITY_WARN
    def _my_check(_edit, **_kw):
        return [SafetyCheckResult.fail("custom", SEVERITY_WARN, "policy violation")]
    imp._safety_check_fn = _my_check
    [out] = asyncio.run(imp.run([_auto_edit_with_code("")]))
    assert out.tier is RiskTier.REVIEW
    assert out.safety_findings[0]["check"] == "custom"


def test_safety_outcome_default_safety_findings_is_empty():
    """EditOutcome.safety_findings defaults to an empty list (BC for older callers)."""
    o = EditOutcome(edit_id="x", op=EditOp.ADD_TEST, tier=RiskTier.AUTO, status="applied")
    assert o.safety_findings == []


def test_safety_outcome_safety_findings_stores_dicts():
    o = EditOutcome(
        edit_id="x", op=EditOp.ADD_TEST, tier=RiskTier.AUTO, status="applied",
        safety_findings=[{"check": "foo", "severity": "warn", "reason": "bar"}],
    )
    assert o.safety_findings == [{"check": "foo", "severity": "warn", "reason": "bar"}]


def test_safety_check_result_bool_is_passed():
    ok = SafetyCheckResult.ok("x")
    bad = SafetyCheckResult.fail("x", SEVERITY_WARN, "y")
    assert bool(ok) is True
    assert bool(bad) is False


def test_safety_no_escalation_when_check_fn_returns_empty():
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "hive/x"})
    gate = _FakeGate()
    imp = SelfImprovement(mod, gate=gate)
    imp._safety_check_fn = lambda *_a, **_kw: []
    [out] = asyncio.run(imp.run([_auto_edit_with_code("rm -rf /")]))
    assert out.tier is RiskTier.AUTO
    assert out.status == "applied"


# --- config integration --------------------------------------------------------

def test_config_has_safety_fields():
    """HiveConfig exposes the new selfmod_enable_safety_checks / max_files fields."""
    from hive.core.config import HiveConfig
    import tempfile, os
    with tempfile.TemporaryDirectory() as tmp:
        cfg = HiveConfig.from_env(root=tmp, load_dotenv=False)
    assert hasattr(cfg, "selfmod_enable_safety_checks")
    assert hasattr(cfg, "selfmod_safety_max_files")
    assert cfg.selfmod_enable_safety_checks is True   # default ON
    assert cfg.selfmod_safety_max_files >= 1


def test_config_safety_max_files_zero_is_invalid():
    from hive.core.config import HiveConfig
    import tempfile, os
    os.environ["HIVE_SELFMOD_SAFETY_MAX_FILES"] = "0"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = HiveConfig.from_env(root=tmp, load_dotenv=False)
        issues = cfg.validate()
        assert any("HIVE_SELFMOD_SAFETY_MAX_FILES" in i for i in issues)
    finally:
        del os.environ["HIVE_SELFMOD_SAFETY_MAX_FILES"]


def test_config_safety_disable_via_env():
    from hive.core.config import HiveConfig
    import os, tempfile
    os.environ["HIVE_SELFMOD_ENABLE_SAFETY_CHECKS"] = "false"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = HiveConfig.from_env(root=tmp, load_dotenv=False)
        assert cfg.selfmod_enable_safety_checks is False
    finally:
        del os.environ["HIVE_SELFMOD_ENABLE_SAFETY_CHECKS"]
