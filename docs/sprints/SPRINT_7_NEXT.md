# SPRINT_7 — Next Batches (proposed plan)

> Generated 2026-08-22 by Hive deep-review of Pillars 1-4 + release system.
> Target: continue HiveOS path from "v1.0 shipped, but still mostly reactive" toward
> "truly autonomous, self-improving agent".

## State summary

**Done this session:**
- Pillar 1 (self-improve audit + 4 bug fixes) — 11 tests
- Pillar 2 (approval gate hardening) — 14 tests
- Pillar 3 (learned skills from audit patterns) — 18 tests
- Pillar 4 (selfmod safety checks) — 50 tests
- Release versioning system (CHANGELOG / RELEASE_NOTES / VERSION / scripts)
- Full suite: **3956 passed, 4 skipped**

**Still open from Pillar plan (gaps observed during review):**

1. **Self-improvement memory doesn't write** — `apply_approved` (human-approved path)
   doesn't record outcomes to memory. Only the AUTO `self_improve_from_symptom` path
   records. So human-approved fixes aren't bucketed and don't contribute to learning.

2. **Learned skills has no evaluation harness** — `propose_skill` generates code but
   there's no `pytest run` against the generated body before approval.

3. **No proactive gap analysis** — heartbeat only fires when `recent_failures >= N`.
   It doesn't proactively ask "what new skills would help?" or "what context is stale?".

4. **Memory entity resolution is shallow** — "PR #95" and "PR_95" probably aren't merged.
   Entity recognition = how Hive actually thinks about repeated work.

5. **No live trace streaming to operator** — `audit_log.export()` exists but no
   real-time push to dashboard / Telegram.

6. **No cost projection / forecast** — budget shows "X of Y today" but no
   "at current rate, you'll hit $X by Friday" alert.

---

## Proposed batches

### Batch A — Close Pillar 1 gaps (small, ~3 hours)
**Goal:** make self-improvement memory recording complete across ALL paths.

- A1. Hook `apply_approved` into the same outcome-recording path as `_apply_one`
  (`core/spec_search.py`).
- A2. Add tests for the previously-uncovered human-approval path.
- A3. Add a `--dry-run` mode for MANUAL tier (currently it just enqueues a task).

Branch: `sprint7/self-improve-memory-complete`
Expected: ~6 new tests, ~30 LOC production.

### Batch B — Pre-flight test runner for Learned Skills (medium, ~4 hours)
**Goal:** Generated skill bodies are validated BEFORE approval.

- B1. Generate the body into a temp worktree.
- B2. Run `pytest` against a tiny smoke suite (the body must execute without raising).
- B3. Capture pass/fail into the SkillTemplate record.
- B4. UI surfaces the smoke-test result on `GET /skills/learned/{id}`.

Branch: `sprint7/learned-skills-eval`
Expected: ~12 new tests, ~80 LOC production.

### Batch C — Heartbeat Proactive Intelligence (medium, ~6 hours)
**Goal:** heartbeat stops being purely reactive. Once per cycle, do a "look around":

- C1. New `heartbeat.proactive_scan()` — runs every N cycles (configurable), looks for:
  - Repeated tool sequences that aren't yet a learned skill
  - Stale facts (>30 days, never accessed)
  - High-uncertainty commitments
- C2. Emits `proactive_suggestion` events that go to TaskBoard.
- C3. Tests prove heartbeat is not triggered more often than configured.

Branch: `sprint7/heartbeat-proactive`
Expected: ~15 new tests, ~150 LOC production.

### Batch D — Memory Entity Resolution (large, ~8 hours)
**Goal:** "PR #95" == "PR_95" == "PR-95" in entity graph.

- D1. Add an `EntityResolver` step on Mnemosyne consolidation.
- D2. Configurable aliases (string similarity + explicit mappings).
- D3. Tests with deliberately messy input ("PR 95", "pr_95", "PR95") prove they merge.

Branch: `sprint7/memory-entity-resolution`
Expected: ~20 new tests, ~200 LOC production.

### Batch E — Real-time audit push (small, ~2 hours)
**Goal:** operator sees what Hive is doing *right now*.

- E1. Add `/ws/audit` WebSocket that streams audit rows as they're written.
- E2. Dashboard subscribes and shows a live feed.
- E3. Telegram bot can be subscribed too (optional).

Branch: `sprint7/live-audit-stream`
Expected: ~8 new tests, ~100 LOC production.

### Batch F — Cost projection / forecast (small, ~2 hours)
**Goal:** budget page shows projection, not just current state.

- F1. `budgeter.forecast(days=7)` — linear projection from current rate.
- F2. New `/budget/forecast` endpoint.
- F3. Threshold-based Telegram alert.

Branch: `sprint7/budget-forecast`
Expected: ~10 new tests, ~80 LOC production.

---

## Priority recommendation

If asked for **one batch**: **Batch A** — it closes a real gap (uncovered memory path)
with minimum risk and maximum clarity (a follow-on to Pillars 1+2).

If asked for **two batches**: **A + B** — together they make the self-improvement loop
fully observable (memory records everywhere) AND safe (learned skills validated).

If asked for **three+ batches**: add **C** — without proactive heartbeat, Hive is
still fundamentally reactive. This is the biggest jump toward true autonomy.

---

## Risks / things to watch

1. **Don't merge all 4 PRs in one batch** — Kamil's pattern is human review per PR.
2. **`Core/approval_gate.py` is untouchable** — anything safety-related must wrap, not edit.
3. **Learned skills could proliferate** — Curator's "never delete" + automatic creation
   = growth. Need an LRU/age-out policy. Defer to Batch D.
4. **CI runs take 3 min** — Pillar 1 + 2 + 3 + 4 added ~10s each but ruff + pytest
   together still fit. New tests in any batch should stay under ~30.