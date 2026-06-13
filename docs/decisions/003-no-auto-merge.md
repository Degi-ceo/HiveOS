# ADR 003 — Hive never self-merges (the central safety invariant)

**Status:** Accepted  
**Date:** 2026-06-13  
**Deciders:** Kamil (owner), Hive (architect)

---

## Context

HiveOS's self-modification loop (`core/spec_search.py` + `core/self_mod.py`) can propose and apply code changes, run tests, push branches, and open draft PRs. The question is: should the system be allowed to merge its own PRs after tests pass?

The theoretical argument for auto-merge: if tests pass, the change is correct; human review is a bottleneck; continuous self-improvement speed is maximized by removing the bottleneck.

The arguments against are stronger.

---

## Decision

**Hive never merges to `main`. All changes go through branch → tests → draft PR → Kamil review → merge.** This is enforced at multiple levels:

1. **`SelfModifier.propose`** opens a *draft* PR, never calls the GitHub merge API.
2. **`core/self_mod.py::_touches_protected`** refuses any edit touching `Config/SOUL.md` or `Core/approval_gate.py` before tests even run.
3. **`Core/approval_gate.py`** (PROTECTED, uneditable by Hive) routes all dangerous tool calls through the human approval queue.
4. **`CLAUDE.md` + `AGENTS.md`** state the invariant explicitly so every Claude Code session and Codex session gets it from context.
5. **`docs/CONTRIBUTING.md`** lists "never merge to main without human review" as rule #2.

The self-improvement loop is designed as an **amplifier**, not an **autonomous actor**: Hive finds the gap, proposes the fix, tests it, and surfaces it for human judgment. The human provides the merge authorization.

---

## Consequences

**Good:**
- A self-modifying agent with a merge gate is **auditable**: every change to `main` has a human decision point. Hive cannot silently alter its own safety rules.
- The gate prevents the classic AI safety failure mode: a system that modifies its objective function or removes its own constraints.
- PROTECTED files (`SOUL.md`, `approval_gate.py`) cannot be modified by any code path Hive controls — the check is in `self_mod.py`, upstream of the approval gate itself.
- Draft PRs are visible: Kamil can see every self-mod attempt in the GitHub PR list, including ones that failed or were rejected.
- The model cannot self-escalate its risk tier — the `RiskTier` table is code, not LLM output (see ADR 005).

**Bad / trade-offs:**
- Self-improvement is rate-limited by Kamil's review bandwidth. A backlog of draft PRs accumulates during periods of inactivity.
- The loop is not fully autonomous. This is intentional.

---

## What this prevents

| Scenario | Prevented by |
|---|---|
| Hive merges a change that disables the approval gate | `_touches_protected` check |
| Hive approves its own REVIEW-tier edit | `edit_pending` + `/approvals/decide` requires human HTTP call |
| Hive uses `gh pr merge` or GitHub REST merge API | `SelfModifier.propose` never calls merge; it is also not in any builtin tool |
| Hive modifies `SOUL.md` to relax its own constraints | `PROTECTED_PATHS` check; `SOUL.md` is read-only via `soul.py` |
| A compromised MCP tool triggers a merge | All MCP tools are marked `dangerous=True`; require approval gate resolution |

---

## Alternatives considered

**Auto-merge after green CI:** Faster loop, but removes the human decision point. The risk is asymmetric: a subtle bug in the self-mod logic (or a malicious injected edit) could compromise the system permanently. The value of the human gate is highest precisely in the cases where the automated check misses something.

**Time-delayed auto-merge:** Merge after N hours if no human veto. Adds complexity, still removes meaningful oversight, and doesn't solve the asymmetric risk.

**Merkle-tree audit log + auto-merge:** Cryptographic auditability after the fact. Doesn't prevent the damage; only helps forensics.

---

## See also

- [`core/self_mod.py`](../../src/hive/core/self_mod.py) — `SelfModifier.propose`, `_touches_protected`
- [`Core/approval_gate.py`](../../Core/approval_gate.py) — the danger firewall (PROTECTED)
- [`docs/SECURITY.md`](../SECURITY.md) — full threat model
- [`docs/CONTRIBUTING.md`](../CONTRIBUTING.md) — PR workflow rules
