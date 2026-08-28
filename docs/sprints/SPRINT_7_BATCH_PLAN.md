# SPRINT_7 — Batch Execution Plan (A → F)

> Plan for batches A through F. Saved at start of execution so it's always
> available — survives SSH drops. Each batch is a separate branch + commits.
> Per CLAUDE.md, no push, no PR — Kamil reviews locally and merges.

---

## Branches in flight (state at plan creation)

| Branch | Tip | Pillars |
|--------|-----|---------|
| `sprint7/selfmod-safety` | `fb8719e` | Pillar 4 + release + docs + gitignore |
| `sprint7/approval-hardening` | `128bd7a` | Pillar 2 + release + docs + gitignore |
| `sprint7/learned-skills` | `8ec10ca` | Pillar 1+3 + release + docs + gitignore |

**Test suite:** 3956 passed, 4 skipped. Ruff clean.

---

## Batch A — Close Pillar 1 memory hook gap

**Goal:** `apply_approved` (human-approved path) also records outcomes to memory.
Currently only the AUTO `self_improve_from_symptom` path records — approved fixes
aren't bucketed and don't contribute to learning.

**Branch:** `sprint7/self-improve-memory-complete` (from `sprint7/learned-skills`)
**Estimated effort:** ~3h
**Expected tests:** ~6 new
**Expected LOC:** ~30 production + ~150 tests

### Subtasks

A1.1 Read `core/spec_search.py` — understand how `_apply_one` records outcomes.
     Find the equivalent place in `apply_approved`.
A1.2 Lift the outcome-recording block into a private helper:
     `_record_outcome(outcome, memory_provider)` in `SelfImprovement`.
A1.3 Call it from BOTH paths (`_apply_one` AND `apply_approved`).
A1.4 Add a `--dry-run` flag to the MANUAL tier path — currently it just
     enqueues a task with no preview. `--dry-run` returns the proposed
     Edit(s) + safety findings without enqueuing.
A1.5 Tests: 6 cases
  - `apply_approved` records success to memory
  - `apply_approved` records failure (REJECT tier) to memory
  - dry-run returns proposed edits without enqueueing
  - dry-run still runs safety checks
  - approved-from-pending-store path uses memory hook
  - integration: symptom → AUTO success → memory bucket (existing test)
              + symptom → REVIEW → approve → memory bucket (new test)

### Verification
- `pytest tests/ -q` — 3962+ passed
- `ruff check` clean

---

## Batch B — Pre-flight test runner for Learned Skills

**Goal:** generated skill bodies validated BEFORE approval. Currently
`propose_skill` writes the body to SQLite and waits for human approval — but
nothing verifies the body actually executes without raising.

**Branch:** `sprint7/learned-skills-eval` (from `sprint7/learned-skills`)
**Estimated effort:** ~4h
**Expected tests:** ~12 new
**Expected LOC:** ~80 production + ~250 tests

### Subtasks

B1.1 Add `LearnedSkillStore.run_smoke_test(template_id)`:
  - Materialize the body in a temp worktree (use existing self_mod sandbox).
  - Run a tiny harness: `await template.compile()(call_tool, args)` with
    a fixture `call_tool` that returns "ok".
  - Capture pass/fail/exception into the SkillTemplate record.
B1.2 Add a `smoke_result` field to `SkillTemplate` (status enum: `none`,
    `pass`, `fail`, `error` + `smoke_log: str`).
B1.3 `propose_skill` automatically calls `run_smoke_test` once before
    flipping status to `proposed`. Templates that fail smoke get status
    `smoke_failed` instead of `proposed` and require manual override.
B1.4 `GET /skills/learned/{id}` returns smoke_result + smoke_log.
B1.5 Tests: 12 cases
  - smoke test runs the body with a fixture tool
  - body that raises → status `smoke_failed`
  - body that returns None → status `pass`
  - body that uses non-existent tool → status `smoke_failed`
  - propose_skill smoke runs before status flip
  - GET endpoint surfaces smoke_result
  - manual override path allows proposing despite smoke_failed
  - safety checks also run during smoke (integration with Pillar 4)

### Verification
- `pytest tests/ -q` — 3974+ passed
- `ruff check` clean

---

## Batch C — Heartbeat Proactive Intelligence

**Goal:** heartbeat stops being purely reactive. Currently fires only when
`recent_failures >= N`. Add a periodic proactive scan that looks for:
- Repeated tool sequences that aren't yet learned skills (candidates for Pillar 3)
- Stale facts (>30 days, never accessed)
- High-uncertainty commitments (deadlines with no progress)

**Branch:** `sprint7/heartbeat-proactive` (from `sprint7/learned-skills`)
**Estimated effort:** ~6h
**Expected tests:** ~15 new
**Expected LOC:** ~150 production + ~300 tests

### Subtasks

C1.1 Add config fields: `heartbeat_proactive_interval_sec` (default 86400,
     1 day), `heartbeat_stale_fact_days` (default 30).
C1.2 New `Heartbeat.proactive_scan()` method:
  - Detect candidate patterns (delegates to `learned_skills.detect_patterns`).
  - Find stale facts via Mnemosyne (if available, else SKIP with warning).
  - Find high-uncertainty commitments (heuristic: `commitment.no_progress > N days`).
C1.3 Heartbeat `tick()` calls `proactive_scan()` every N ticks (configurable).
C1.4 Findings → `TaskBoard` as `proactive_suggestion` tasks (priority
     below human-tasks but above AUTO self-improvement).
C1.5 Tests: 15 cases
  - proactive_scan finds patterns not yet registered as learned
  - proactive_scan skips already-registered patterns
  - proactive_scan finds stale facts (mock Mnemosyne)
  - proactive_scan finds overdue commitments
  - tick() calls scan every Nth iteration
  - scan emits TaskBoard rows with correct priority
  - scan disabled when interval=0
  - scan doesn't crash when Mnemosyne unavailable
  - scan is rate-limited (no more than 1 per interval)
  - integration: learned-skill detection feeds proposed-skill endpoint

### Verification
- `pytest tests/ -q` — 3989+ passed
- `ruff check` clean

---

## Batch D — Memory Entity Resolution

**Goal:** "PR #95" == "PR_95" == "PR-95" in entity graph. Today, Mnemosyne stores
each surface form as a separate fact, so retrieval misses related work.

**Branch:** `sprint7/memory-entity-resolution` (from `sprint7/learned-skills`)
**Estimated effort:** ~8h
**Expected tests:** ~20 new
**Expected LOC:** ~200 production + ~400 tests

**Status:** DONE — implemented on `sprint7/memory-entity-resolution` (2 commits,
3957 passed / 4 skipped, ruff clean).

### Delivered

- **`src/hive/memory/entity_resolver.py`** (~135 LOC, pure-Python):
  - `EntityResolver.canonical_key(surface)` — NFKD + lowercase + drop non-word
    chars + collapse whitespace.
  - `EntityResolver.resolve(surface)` — returns `ResolvedEntity(canonical_key,
    aliases, confidence)`; alias-map hit is confidence 0.9 vs 1.0 for pure
    normalization.
  - `EntityResolver.merge(facts)` — groups by canonical_key, deep-merges data
    dicts, concatenates lists, accumulates fact ids, preserves every distinct
    surface form as an alias.
- **`src/hive/memory/keeper.py`** — `MemoryKeeper` now accepts an optional
  `resolver=` and a `consolidate(use_entity_resolution=True)` flag that
  deduplicates by canonical key before `learn()`. Setting the flag to `False`
  preserves the pre-Batch-D behaviour (verbatim surface form, per-item
  already_known check).
- **`src/hive/runtime.py`** — `HiveOS.consolidate()` now defaults to entity
  resolution when `config.entity_resolution_enabled=True` and passes
  `HiveOS.consolidate()` call into the keeper with that flag. The alias map is
  parsed from `HIVE_ENTITY_RESOLUTION_ALIAS_MAP` (inline JSON or path); the
  helper `_load_entity_alias_map` is fail-open so a broken spec degrades to an
  empty map rather than crashing consolidation.
- **`src/hive/core/config.py`** — new fields:
  - `entity_resolution_enabled: bool` (HIVE_ENTITY_RESOLUTION_ENABLED, default True)
  - `entity_resolution_alias_map: str` (HIVE_ENTITY_RESOLUTION_ALIAS_MAP, default "")
- **`tests/test_entity_resolver.py`** — 22 tests (5 normalization, 5 resolution,
  5 merge, 5 integration, 2 bonus sanity checks).
- Backwards-compat: `use_entity_resolution=False` keeps the pre-Batch-D
  per-surface check; existing `tests/test_memory.py::test_keeper_consolidate_continues_on_item_error`
  preserved with the legacy flag explicitly set.

### Verification
- `pytest tests/ -q` — 3957 passed, 4 skipped
- `ruff check src/ tests/` — clean

---

## Batch E — Real-time audit push (WebSocket)

**Goal:** operator sees what Hive is doing RIGHT NOW (not on 6s polling).

**Branch:** `sprint7/live-audit-stream` (from `sprint7/learned-skills`)
**Estimated effort:** ~2h
**Expected tests:** ~8 new
**Expected LOC:** ~100 production + ~150 tests

### Subtasks

E1.1 New gateway endpoint: `GET /ws/audit` (WebSocket, auth-gated).
E1.2 New `AuditBroadcaster` in `observability/audit.py` — publishes new
     audit rows to a queue. Subscribers (`/ws/audit`) get them in real time.
E1.3 Dashboard subscribes (frontend change in `dashboard/src/`).
E1.4 Tests: 8 cases
  - WS endpoint rejects unauth
  - WS endpoint accepts auth
  - broadcaster publishes to multiple subscribers
  - subscriber receives new audit row within 100ms
  - subscriber disconnection cleans up
  - existing audit export still works (no regression)
  - rate-limit: max 1 broadcast per 50ms (prevent flood)
  - integration: tool executor write → broadcaster → WS → assertion

### Verification
- `pytest tests/ -q` — 4017+ passed
- `ruff check` clean

---

## Batch F — Cost projection / forecast

**Goal:** budget page shows projection, not just current state.
Today budgeter shows "$X of $Y today" but no "at current rate, $X by Friday" alert.

**Branch:** `sprint7/budget-forecast` (from `sprint7/learned-skills`)
**Estimated effort:** ~2h
**Expected tests:** ~10 new
**Expected LOC:** ~80 production + ~180 tests

### Subtasks

F1.1 New `budgeter.forecast(days: int, now: datetime | None = None)` method:
  - Linear projection from current spend rate over `days`.
  - Returns: `{ projected_total, daily_avg, days_until_cap, status }`.
F1.2 New gateway endpoint: `GET /budget/forecast?days=7`.
F1.3 Telegram alert: when `days_until_cap <= 1`, send warning.
  - Configurable threshold (`HIVE_BUDGET_FORECAST_ALERT_DAYS`, default 1).
F1.4 Tests: 10 cases
  - forecast on empty history → safe defaults
  - forecast with constant rate → matches
  - forecast with bursty rate → bounded by max
  - days_until_cap when under cap → positive int
  - days_until_cap when over cap → 0
  - threshold alert triggers correctly
  - threshold alert doesn't fire when disabled
  - GET endpoint formats response
  - integration: telemetry → budgeter → forecast endpoint

### Verification
- `pytest tests/ -q` — 4027+ passed
- `ruff check` clean

---

## Cumulative target

After all 6 batches (A → F):
- **+~71 new tests** (6 + 12 + 15 + 20 + 8 + 10)
- **+~640 production LOC**
- **4027 tests passing**
- **6 local branches awaiting Kamil's review & merge**

---

## Dependencies & ordering

| Batch | Depends on | Blocks |
|-------|------------|--------|
| A | Pillar 1 (done) | nothing |
| B | Pillar 3 (done) | nothing |
| C | Pillar 3 (done) | nothing |
| D | independent | nothing |
| E | independent | nothing |
| F | independent | nothing |

**No inter-batch dependencies** — all 6 can run in parallel if Kamil wants fast turnaround.

---

## Risks

1. **Merge conflicts** — all branches fork from `sprint7/learned-skills` (or `main`
   after Pillar 1+3 merge). Merging in arbitrary order may conflict on shared files.
2. **Test count inflation** — `pytest` runtime grows linearly. Batches A-F add ~71
   tests; total suite should still run under 5 min.
3. **Mnemosyne dependency** — Batch D may fail on CI if Mnemosyne extra isn't installed.
   Add `pytest.mark.skipif` similar to existing patterns.
4. **WebSocket test flakiness** — Batch E WebSocket tests are notoriously flaky.
   Use `pytest-asyncio` patterns proven in existing WS tests.

---

## Recovery & status commands

```bash
bash scripts/status.sh                  # instant status snapshot
cat docs/sprints/SPRINT_7_BATCH_PLAN.md # this file
git log --all --oneline | head -20      # commit timeline
bash scripts/release-notes.sh <ref>    # auto-generate per-branch notes
```

If SSH drops mid-batch:
1. `bash scripts/status.sh` — see current state
2. `cat docs/sprints/SPRINT_7_BATCH_PLAN.md` — see where you are in the plan
3. `git status` in the right worktree — see uncommitted changes
4. Resume the batch from the next subtask