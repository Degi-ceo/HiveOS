"""M2 #si-1 — risk-tiered self-improvement loop tests (offline)."""
from __future__ import annotations

import asyncio

import pytest

from hive.core.self_mod import SelfModifier
from hive.core.spec_search import (
    Edit, EditOp, EditOutcome, RiskTier, SelfImprovement,
    assign_tier, diagnose_and_run, tiered, _TIER_TABLE,
)


# --- deterministic tiering -----------------------------------------------------

def test_every_op_has_a_tier():
    assert set(_TIER_TABLE) == set(EditOp)


def test_model_cannot_escalate_its_own_tier():
    # An edit arrives claiming AUTO for a code patch; the table forces REVIEW.
    async def _noop(_wt): return []
    e = Edit(op=EditOp.PATCH_CODE, summary="sneaky", apply=_noop, risk_tier=RiskTier.AUTO)
    [fixed] = tiered([e])
    assert fixed.risk_tier is RiskTier.REVIEW
    assert assign_tier(EditOp.DEPENDENCY_CHANGE) is RiskTier.MANUAL
    assert assign_tier(EditOp.ADD_TEST) is RiskTier.AUTO


# --- gate routing by tier ------------------------------------------------------

class _FakeModifier:
    """Duck-typed SelfModifier: records propose() calls, returns a canned result."""
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


def _edit(op, summary="s"):
    async def _apply(_wt): return ["src/hive/x.py"]
    return Edit(op=op, summary=summary, apply=_apply)


def test_auto_edit_drives_through_modifier():
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "hive/auto-1"})
    imp = SelfImprovement(mod, gate=_FakeGate())
    [out] = asyncio.run(imp.run([_edit(EditOp.ADD_TEST)]))
    assert out.status == "applied" and out.branch == "hive/auto-1"
    assert mod.calls and out.tier is RiskTier.AUTO


def test_review_edit_requests_approval_and_does_not_apply():
    mod = _FakeModifier({"ok": True})
    gate = _FakeGate()
    imp = SelfImprovement(mod, gate=gate)
    [out] = asyncio.run(imp.run([_edit(EditOp.PATCH_CODE)]))
    assert out.status == "pending_approval" and out.approval_id == "appr-1"
    assert gate.requests and not mod.calls   # gate hit, modifier NOT called


def test_manual_edit_is_recorded_not_applied():
    mod = _FakeModifier({"ok": True})
    imp = SelfImprovement(mod, gate=_FakeGate())
    [out] = asyncio.run(imp.run([_edit(EditOp.DEPENDENCY_CHANGE)]))
    assert out.status == "manual" and not mod.calls


def test_protected_block_surfaces():
    mod = _FakeModifier({"ok": False, "stage": "protected", "msg": "touches SOUL.md"})
    imp = SelfImprovement(mod, gate=_FakeGate())
    [out] = asyncio.run(imp.run([_edit(EditOp.ADD_TEST)]))
    assert out.status == "blocked_protected" and "SOUL" in out.detail


def test_failed_test_surfaces():
    mod = _FakeModifier({"ok": False, "stage": "test", "log": "1 failed"})
    imp = SelfImprovement(mod, gate=_FakeGate())
    [out] = asyncio.run(imp.run([_edit(EditOp.ADD_TEST)]))
    assert out.status == "failed" and "test" in out.detail


def test_apply_approved_runs_review_edit():
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "b"})
    imp = SelfImprovement(mod, gate=_FakeGate())
    out = asyncio.run(imp.apply_approved(_edit(EditOp.PATCH_CODE)))
    assert out.status == "applied" and mod.calls


# --- diagnose loop -------------------------------------------------------------

def test_diagnose_and_run_no_edits_is_noop():
    async def diagnoser(_ctx): return []
    imp = SelfImprovement(_FakeModifier({"ok": True}), gate=_FakeGate())
    assert asyncio.run(diagnose_and_run(diagnoser, "ctx", imp)) == []


def test_diagnose_and_run_mixed_tiers():
    async def diagnoser(_ctx):
        return [_edit(EditOp.ADD_TEST), _edit(EditOp.PATCH_CODE),
                _edit(EditOp.DEPENDENCY_CHANGE)]
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "b"})
    imp = SelfImprovement(mod, gate=_FakeGate())
    outs = asyncio.run(diagnose_and_run(diagnoser, "ctx", imp, dry_run=True))
    statuses = {o.op: o.status for o in outs}
    assert statuses[EditOp.ADD_TEST] == "applied"
    assert statuses[EditOp.PATCH_CODE] == "pending_approval"
    assert statuses[EditOp.DEPENDENCY_CHANGE] == "manual"


# --- integration with the REAL SelfModifier (fake git runner, no network) ------

def test_integration_auto_edit_with_real_selfmodifier(tmp_path):
    """AUTO edit flows through the real SelfModifier using a fake git runner so the
    worktree->test->push sequence is exercised without touching real git."""
    calls = []

    async def fake_run(cmd, cwd=None):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        calls.append(cmd_str)
        if cmd_str.startswith("git rev-parse"):
            return 0, "deadbeef\n"
        return 0, "ok"  # worktree add, test, add, commit, push, cleanup all succeed

    async def apply_fn(_wt):
        return ["src/hive/llm/pricing.py"]   # non-protected path

    mod = SelfModifier(repo_root=str(tmp_path), run=fake_run, test_cmd="pytest -q")
    imp = SelfImprovement(mod, gate=_FakeGate())
    [out] = asyncio.run(imp.run([_edit(EditOp.ADD_TEST)]))
    assert out.status == "applied" and out.branch and out.branch.startswith("hive/auto-")
    assert any(c.startswith("git worktree add") for c in calls)
    assert any("pytest" in c for c in calls)


def test_integration_protected_edit_is_blocked(tmp_path):
    """An edit that touches a PROTECTED file is refused by the real SelfModifier."""
    async def fake_run(cmd, cwd=None):
        if cmd.startswith("git rev-parse"):
            return 0, "deadbeef\n"
        return 0, "ok"

    async def apply_protected(_wt):
        return ["Config/SOUL.md"]   # PROTECTED

    mod = SelfModifier(repo_root=str(tmp_path), run=fake_run)
    imp = SelfImprovement(mod, gate=_FakeGate())
    [out] = asyncio.run(imp.run([_edit(EditOp.PATCH_CODE)]))  # REVIEW tier → gate, no apply
    # REVIEW never reaches the modifier, so use apply_approved to exercise the block:
    out2 = asyncio.run(imp.apply_approved(
        Edit(op=EditOp.PATCH_CODE, summary="x", apply=apply_protected)))
    assert out2.status == "blocked_protected"
