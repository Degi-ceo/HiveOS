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

### Subtasks

D1.1 New `memory/entity_resolver.py` — pure-Python normalization:
  - Strip non-alphanumerics, lowercase, collapse whitespace → `canonical_key`.
  - Optional alias map (configured via `HIVE_ENTITY_ALIASES`, JSON file).
D1.2 New `Mnemosyne.consolidate_with_resolution()` (replaces or wraps
     `consolidate()`) — applies canonical key during the merge phase so
     "PR #95" and "PR_95" collapse to one entity.
D1.3 Wire into `Heartbeat.tick()` and `curator.consolidify()`.
D1.4 Backwards-compat: keep `consolidate()` as alias.
D1.5 Tests: 20 cases
  - normalizer handles "PR #95", "pr_95", "PR-95" → same canonical key
  - normalizer handles case-only differences
  - normalizer handles whitespace
  - alias map overrides defaults
  - consolidation merges facts with same canonical key
  - consolidation preserves the original surface form in a `aliases` field
  - retrieval by any alias returns the merged entity
  - integration: heartbeat → consolidation → retrieval chain

### Verification
- `pytest tests/ -q` — 4009+ passed
- `ruff check` clean

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

## Batch F — Cost projection / forecast **DONE**

**Goal:** budget page shows projection, not just current state.
Today budgeter shows "$X of $Y today" but no "at current rate, $X by Friday" alert.

**Branch:** `sprint7/budget-forecast` (from `sprint7/learned-skills`)
**Estimated effort:** ~2h
**Status:** SHIPPED locally (awaiting Kamil merge).
**Actual tests:** 21 new (`tests/test_budget_forecast.py`)
**Actual LOC:** ~220 production + ~280 tests

### Subtasks

F1.1 New `Budgeter.forecast_spend(days: int = 7, *, now: datetime | None = None)` method:
  - Linear projection from a bounded rolling history of past days' spend.
  - Returns a frozen `ForecastResult` dataclass with
    `{ projected_total, daily_avg, max_daily, days_until_cap, status, confidence }`.
  - `status`: `ok` (>3 days), `warn` (1-3), `critical` (≤1), `exceeded` (past cap).
  - Confidence = 1 − stddev/mean, clamped to [0, 1].
  - Added a per-day `_daily_history` deque (capped at `history_window`, default 7)
    that fills automatically on `_roll_day`. Optional `history_path` JSON file for
    cross-restart persistence.
F1.2 Gateway endpoint: `GET /budget/forecast?days=7` (existing endpoint swapped
  to the new spend-forecast). Auth-gated like the other `/budget/*` routes.
  `days` is clamped to [1, 365].
F1.3 Telegram alert via new `autonomy/budget_alert.py` + `Heartbeat._check_budget_alert()`:
  - Fires once per `ok → warn/critical/exceeded` transition (no spam).
  - Skipped below the configured threshold (`HIVE_BUDGET_FORECAST_ALERT_DAYS`,
    default 1). Falls back to log when no Telegram channel is configured.
F1.4 Config field: `budget_forecast_alert_days` + env
  `HIVE_BUDGET_FORECAST_ALERT_DAYS`. Validated as ≥ 0.
F1.5 Tests (21, all passing):
  - empty history → safe defaults (status=ok, days_until_cap=None)
  - constant rate projects linearly
  - bursty rate surfaces max_daily
  - days_until_cap when under cap → positive int
  - days_until_cap when over cap → 0 + status=exceeded
  - status=warn when 1-3 days
  - status=critical when ≤1 day
  - high confidence on constant history
  - `to_dict()` is JSON-safe
  - history persists to disk on `history_path`
  - GET endpoint returns the spend-forecast shape
  - GET endpoint accepts `?days=` query
  - GET endpoint clamps invalid `days`
  - Telegram send on transition ok → warn
  - no spam when status is unchanged
  - no send when status=ok
  - threshold blocks short horizons
  - no-telegram fallback → log only
  - config default = 1
  - config env override
  - config validate rejects negative

### Verification
- `pytest tests/ -q` — 3977 passed, 4 skipped (3956 baseline + 21 new)
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