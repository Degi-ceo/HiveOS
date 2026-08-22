# HiveOS — Release Notes (per-PR / per-branch)

> One section per branch in flight or merged this sprint. Read top-down — newest first.
> For full milestone history see `docs/CHANGELOG.md`.

---

## `gpt-ui-improvements` — GPT UI improvements concept preview

**Status:** draft PR, awaiting human review
**Runtime impact:** none unless `?ui-preview=1` is explicitly present
**UI concept release:** v0.8.2

**What it does**
- Adds a clean, dark/amber fixture preview for all 29 approved mockup states.
- Keeps the current `Centre` application as the default entrypoint.
- Documents every screen, subview, cross-screen relationship, existing API and backend gap.
- Supplies a locked prompt and workflow for regenerating one mockup per image.
- Deep-audits all 29 screens, 70 tabs, 111 actions and 93 relationships without backend calls.
- Adds URL/history state, overlay close/Escape, command and notification entry points,
  responsive mobile action parity and complete keyboard tab behavior.
- Records findings and coverage in `docs/UI_AUDIT_2026-08-22.md`.
- Verifies 107/107 dashboard tests and the production build; preview coverage is
  100% statements/lines/functions and 97.24% branches.

**Rollback**
- Remove the preview import/branch in `dashboard/src/main.jsx` and delete
  `dashboard/src/ui-preview/`; no backend or persisted data migration is involved.

---

## `sprint7/selfmod-safety` @ `99b63bb` — PILLAR 4: Self-Modification Risk Tier Hardening

**Status:** local, awaiting human review & merge
**Tests:** 3956 passed, 4 skipped (full suite); 50 new in `tests/test_self_mod_safety.py`
**Ruff:** clean

**What it does**
- Pre-flight safety checks BEFORE any self-modification reaches the modifier or approval gate.
- Five independent checks (Python syntax, dangerous shell patterns, protected paths, test coverage preservation, file count).
- Table-driven tier policy: AUTO + warn → escalate to REVIEW; REVIEW + critical → escalate to MANUAL (or block); MANUAL stays MANUAL.

**Files**
- **NEW**:** `core/self_mod_safety.py` (313 LOC)
- `core/spec_search.py` (+128 / -5) — `_apply_one` runs checks; `apply_approved` re-runs as final guard; `EditOutcome.safety_findings`
- `core/config.py` (+9) — `selfmod_enable_safety_checks`, `selfmod_safety_max_files`
- **NEW**:** `tests/test_self_mod_safety.py` (479 LOC, 50 tests)

**Config knobs (env)**
- `HIVE_SELFMOD_ENABLE_SAFETY_CHECKS` (default `true`)
- `HIVE_SELFMOD_SAFETY_MAX_FILES` (default `20`)

---

## `sprint7/learned-skills` @ `fa193e8` — PILLAR 3: Learned Skills

**Status:** local, awaiting human review & merge

**What it does**
- Detect repeated tool-call sequences in audit log → propose a `SkillTemplate` → human approves → register in tool registry.
- Generated bodies are DAG-safe: only call tools already in the registry.
- Lifecycle: `proposed → approved → registered → rejected | archived` (never deleted).

**Files**
- **NEW**:** `tools/learned_skills.py` (568 LOC) — `SkillTemplate`, `detect_patterns()`, `propose_skill()`, `LearnedSkillStore` (SQLite), `LearnedSkill(BaseTool)`
- `gateway/app.py` (+120) — 6 new routes before the `/skills/{name}` catchall
- `runtime.py` (+24) — `LearnedSkillStore` wired into `HiveOS`
- **NEW**:** `tests/test_learned_skills.py` (309 LOC, 18 tests)

---

## `sprint7/learned-skills` @ `b431c44` — PILLAR 1: Self-Improvement Loop Audit

**Status:** local, awaiting human review & merge

**What it does** (4 bug fixes)
- **Bug 1:** Success memory recording was dead code — `outcome.status == "pushed"` never matched; AUTO success returns `"applied"`. Hive never learned from successful self-improvements.
- **Bug 2:** Failure memory recording used wrong stage names — modifier emits `"test"`, `"push"`, `"worktree"`, `"no_changes"`, not `"test_fail"`, `"push_fail"`.
- **Bug 3:** No cooldown on failure-triggered self-improve path. Heartbeat fired LLM diagnoser every tick once `recent_failures() >= threshold`.
- **Bug 4:** `apply_approved` failure detail lost test-log context.

**Files**
- `runtime.py` — outcome-recording block rewired to actual statuses (`applied`, `failed`, `blocked_protected`)
- `core/spec_search.py` — `apply_approved` now uses consistent `f"{stage}: {str(log)[:200]}"` format
- `autonomy/heartbeat.py` + `core/config.py` — `selfmod_failure_cooldown_sec` (default 1800s, env `HIVE_SELFMOD_FAILURE_COOLDOWN_SEC`); `_last_failure_self_mod_ts` instance var
- **NEW**:** `tests/test_self_improve_loop_e2e.py` (11 tests)

---

## `sprint7/approval-hardening` @ `c1e4aed` — PILLAR 2: Approval Gate Hardening

**Status:** local, awaiting human review & merge

**What it does**
- **TTL / expiration** of stale pending approvals (default 30 min, configurable)
- **Kill-switch** (emergency stop): engages force-reject all pending + blocks new; release returns to normal
- **Structured audit history** (ring buffer, 1000 records, queryable by tool/outcome/since)
- **Batch approval** (one decision covers N pending ids)

**Files**
- **NEW**:** `core/approval_enhancements.py` (280 LOC)
- `gateway/app.py` — 3 new endpoints + import + decide-route swap
- `tools/executor.py` — kill-switch check + audit hook
- `core/spec_search.py` — kill-switch check + audit hook for REVIEW tier
- **NEW**:** `tests/test_approval_hardening.py` (270 LOC, 14 tests)

---

## Older / merged

- `sprint7/centre-nav-sh1` @ `5363890` — PR #96: SH1 sidebar nav + SH3 command palette + iOS mobile support (OPEN, awaiting human merge)
- `sprint7/cleanup-coverage` @ `fdea7b0` — PR #97: cleanup merged worktrees + coverage gap closed + stale docs corrected (awaiting human merge)
- `main` @ `9d58b07` — PR #95: SPRINT_6 P-G + P-I sections, mark v1.0 shipped
