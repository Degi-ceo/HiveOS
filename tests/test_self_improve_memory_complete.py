"""Memory hook completion tests (Batch A — Pillar 1 gap).

Exercises the closed memory hook that mirrors outcomes (success / failure /
blocked) from BOTH paths:
  - AUTO path: ``SelfImprovement.run`` -> ``_apply_one``
  - REVIEW-approved path: ``SelfImprovement.apply_approved`` (called by the
    gateway /approvals/decide flow after a human approves).

Previously, only the AUTO path recorded outcomes — human-approved fixes were
invisible to the learning loop. Also covers the new ``--dry-run`` mode for
MANUAL-tier edits (returns the proposed preview without enqueueing).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from hive.core.spec_search import (
    Edit, EditOp, EditOutcome, RiskTier, SelfImprovement,
)


# --- Fakes ---------------------------------------------------------------------

class _FakeGate:
    """Minimal duck-typed approval gate."""
    def __init__(self):
        self.requests: list[tuple[str, dict, str]] = []
        self._counter = 0

    def request(self, name, args, reason):
        self.requests.append((name, args, reason))
        self._counter += 1
        return f"appr-{self._counter}"


class _FakeModifier:
    """SelfModifier stand-in with a programmable result."""
    def __init__(self, result):
        self.result = result
        self.calls: list[tuple[str, bool]] = []

    async def propose(self, title, description, apply_fn, *, dry_run=False):
        self.calls.append((title, dry_run))
        return self.result


class _RecordingMemory:
    """Stands in for any MemoryProvider that has a ``learn(kind, topic, content, source)`` method."""
    def __init__(self):
        self.records: list[tuple[str, str, str, str]] = []
        self.raise_on_learn = False

    def learn(self, kind: str, topic: str, content: str, source: str = "") -> None:
        if self.raise_on_learn:
            raise RuntimeError("memory layer is down")
        self.records.append((kind, topic, content, source))

    # Other methods used by the runtime provider (safe defaults).
    def prefetch(self, *_a, **_kw):
        return ""

    def system_prompt_block(self):
        return ""


def _edit(op: EditOp, summary: str = "s", *, rationale: str = "r") -> Edit:
    async def _apply(_wt):
        return ["src/hive/x.py"]
    return Edit(op=op, summary=summary, apply=_apply, rationale=rationale)


def _success_outcome(op: EditOp = EditOp.ADD_TEST) -> EditOutcome:
    return EditOutcome(
        edit_id="eid-1", op=op, tier=RiskTier.AUTO,
        status="applied", branch="hive/test-1", detail="pushed",
    )


def _failed_outcome(op: EditOp = EditOp.ADD_TEST, stage: str = "test",
                   log: str = "1 failed") -> EditOutcome:
    return EditOutcome(
        edit_id="eid-2", op=op, tier=RiskTier.AUTO, status="failed",
        detail=f"{stage}: {log}",
    )


def _blocked_outcome(op: EditOp = EditOp.PATCH_CODE) -> EditOutcome:
    return EditOutcome(
        edit_id="eid-3", op=op, tier=RiskTier.REVIEW,
        status="blocked_protected", detail="touches SOUL.md",
    )


# --- A1.4 #1: apply_approved success recorded to memory ------------------------

def test_apply_approved_records_success_to_memory():
    """Pillar 1 gap: human-approved REVIEW-tier edits that apply successfully must
    be recorded as ``success:<op>`` so the learning loop picks them up."""
    mem = _RecordingMemory()
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "hive/appr-42"})
    imp = SelfImprovement(mod, gate=_FakeGate(), memory_provider=mem)

    edit = _edit(EditOp.PATCH_CODE, summary="patch bug")
    out = asyncio.run(imp.apply_approved(edit))
    assert out.status == "applied"
    assert out.branch == "hive/appr-42"

    success = [r for r in mem.records if r[1].startswith("success:")]
    assert success, "apply_approved success was NOT recorded to memory"
    assert success[0][1] == "success:patch_code"
    assert "hive/appr-42" in success[0][2]


# --- A1.4 #2: apply_approved failure recorded with stage ------------------------

def test_apply_approved_records_failure_to_memory():
    """Pillar 1 gap: human-approved edits that fail at the modifier stage must
    be bucketed as ``failure:<stage>`` (same format as the AUTO path)."""
    mem = _RecordingMemory()
    mod = _FakeModifier({
        "ok": False, "stage": "test", "log": "AssertionError 1 != 2",
    })
    imp = SelfImprovement(mod, gate=_FakeGate(), memory_provider=mem)

    edit = _edit(EditOp.PATCH_CODE, summary="patch bug")
    out = asyncio.run(imp.apply_approved(edit))
    assert out.status == "failed"
    assert "test:" in out.detail

    failure = [r for r in mem.records if r[1].startswith("failure:")]
    assert failure, "apply_approved failure was NOT recorded"
    assert failure[0][1] == "failure:test", (
        f"expected 'failure:test', got {failure[0][1]!r}"
    )


# --- A1.4 #3: protected block recorded -----------------------------------------

def test_apply_approved_records_protected_block_to_memory():
    """A protected-file block surfaced by ``apply_approved`` must be bucketed
    as ``failure:protected`` for the learning loop."""
    mem = _RecordingMemory()
    mod = _FakeModifier({
        "ok": False, "stage": "protected", "msg": "touches SOUL.md",
    })
    imp = SelfImprovement(mod, gate=_FakeGate(), memory_provider=mem)

    edit = _edit(EditOp.PATCH_CODE, summary="patch soul")
    out = asyncio.run(imp.apply_approved(edit))
    assert out.status == "blocked_protected"

    assert any(r[1] == "failure:protected" for r in mem.records), (
        "expected 'failure:protected' topic, got: "
        + ", ".join(r[1] for r in mem.records)
    )


# --- A1.4 #4: memory failure must not break the approval loop -------------------

def test_apply_approved_memory_write_failure_does_not_break():
    """If the injected memory provider raises during ``learn()``, the approval
    must still complete (the human-approved edit must not be reverted because
    the learning layer is down). Regression for the runtime's silent try/except."""
    mem = _RecordingMemory()
    mem.raise_on_learn = True
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "hive/x"})
    imp = SelfImprovement(mod, gate=_FakeGate(), memory_provider=mem)

    edit = _edit(EditOp.PATCH_CODE, summary="patch")
    # Must NOT raise, even though the memory sink raises on every learn().
    out = asyncio.run(imp.apply_approved(edit))
    assert out.status == "applied"
    assert out.branch == "hive/x"


# --- A1.4 #5: dry-run on MANUAL returns the proposed edit (no enqueue) ---------

def test_dry_run_returns_edits_without_enqueuing():
    """A MANUAL-tier edit run with ``dry_run=True`` returns a preview (an
    EditOutcome plus the proposed Edit) without invoking the modifier and
    without touching git / pushing / running tests."""
    mem = _RecordingMemory()
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "hive/x"})
    gate = _FakeGate()
    imp = SelfImprovement(mod, gate=gate, memory_provider=mem)

    edit = _edit(EditOp.DEPENDENCY_CHANGE, summary="bump requests")
    out = asyncio.run(imp.run([edit], dry_run=True)[0]) if False else \
         asyncio.run(_dry_run(imp, [edit]))

    assert out.status == "manual"
    assert "dry-run" in out.detail
    # Modifier was NOT called — dry-run on MANUAL does not even build a worktree.
    assert mod.calls == [], (
        f"dry_run on MANUAL must NOT call the modifier (got calls: {mod.calls!r})"
    )
    # Gate was NOT asked (no approval_id surfaced, pending_store empty).
    assert gate.requests == [], "dry_run on MANUAL must NOT request approval"
    # mem.learn may record (the 'manual' outcome) — that's fine, it's just learning.
    # The HARD guarantee is: no modifier call, no git side effects.


async def _dry_run(imp: SelfImprovement, edits: list[Edit]) -> EditOutcome:
    return (await imp.run(edits, dry_run=True))[0]


# --- A1.4 #6: dry-run still surfaces safety / tier information -----------------

def test_dry_run_runs_safety_checks_and_tier_classification():
    """``dry_run=True`` must still classify the edit by its canonical risk tier
    (deterministic, not the model's). A REVIEW-tier manual-flagged edit must
    remain REVIEW — dry-run is a preview, not a tier override."""
    mem = _RecordingMemory()
    mod = _FakeModifier({"ok": True, "stage": "pushed"})
    imp = SelfImprovement(mod, gate=_FakeGate(), memory_provider=mem)

    # A REVIEW-tier edit (PATCH_CODE) under dry_run=True → still REVIEW,
    # still routed through the gate, still surfaces a pending_approval preview.
    edit = _edit(EditOp.PATCH_CODE, summary="patch")
    out = asyncio.run(imp.run([edit], dry_run=True))[0]
    assert out.tier == RiskTier.REVIEW, out.tier
    # The dry-run path on REVIEW still goes through the gate (no real apply),
    # so it returns status="pending_approval".
    assert out.status == "pending_approval"


# --- Bonus A1.4 #7: outcome detail format consistency ---------------------------

def test_apply_approved_outcome_has_stage_in_detail():
    """``apply_approved`` failure detail must follow the canonical
    ``<stage>: <log[:200]>`` format so callers and dashboards see consistent
    context whether the edit was applied via AUTO or via human approval."""
    long_log = "FAILED test_x - AssertionError 1 != 2\n" * 10
    mem = _RecordingMemory()
    mod = _FakeModifier({"ok": False, "stage": "push", "log": long_log})
    imp = SelfImprovement(mod, gate=_FakeGate(), memory_provider=mem)

    out = asyncio.run(imp.apply_approved(_edit(EditOp.PATCH_CODE)))
    assert out.status == "failed"
    assert out.detail.startswith("push:") or out.detail.startswith("push"), \
        f"detail must start with the stage label, got {out.detail!r}"
    # And it must contain a slice of the log (not just the bare stage).
    assert len(out.detail) > len("push") + 2


# --- A1.1 regression: helper integrates with AUTO path too -----------------------

def test_auto_path_uses_helper_for_memory_recording():
    """The AUTO path must also flow through the helper. After the refactor,
    the helper records ``success:<op>`` for AUTO success automatically — the
    runtime does NOT need to do it inline anymore."""
    mem = _RecordingMemory()
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "hive/a"})
    imp = SelfImprovement(mod, gate=_FakeGate(), memory_provider=mem)

    [out] = asyncio.run(imp.run([_edit(EditOp.ADD_TEST)]))
    assert out.status == "applied"
    assert any(r[1] == "success:add_test" for r in mem.records), (
        "AUTO success must still be recorded after the helper refactor"
    )


def test_no_memory_provider_does_not_break_loop():
    """When no memory_provider is injected (e.g. a downstream caller constructs
    SelfImprovement without one), the loop still works — recording is a no-op
    but never an error."""
    mod = _FakeModifier({"ok": True, "stage": "pushed", "branch": "hive/x"})
    imp = SelfImprovement(mod, gate=_FakeGate())  # NO memory_provider
    [out] = asyncio.run(imp.run([_edit(EditOp.ADD_TEST)]))
    assert out.status == "applied"

    # And apply_approved works too.
    edit = _edit(EditOp.PATCH_CODE)
    out2 = asyncio.run(imp.apply_approved(edit))
    assert out2.status == "applied"
