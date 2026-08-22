# HiveOS — Changelog (root mirror)

> ⚠️ **Canonical source:** `docs/CHANGELOG.md`. This file mirrors the latest entries for
> at-a-glance visibility and resilience when SSH drops mid-session. Both files are kept
> in sync; if they diverge, `docs/CHANGELOG.md` wins.

---

## [GPT UI improvements concept preview v0.8.2] — IN REVIEW (2026-08-22)

- Deep-audited all 29 approved mockup states, all 70 tab transitions,
  111 primary/row actions and 93 related-view controls.
- Added the missing Cron, Commitments and three mobile mockup states.
- Fixed history/deep links, tabs, overlays, safe action routing, responsive action
  visibility and keyboard/focus accessibility.
- Added `docs/UI_AUDIT_2026-08-22.md` as the verification ledger.
- Added preview coverage enforcement; 107/107 dashboard tests and production build pass.
- The v0.8.1 foundation added the isolated fixture-only preview at `/?ui-preview=1`.
- Added a verified UI-to-API relationship matrix and backend gap register.
- Added the mockup generation guide and locked professional dark/amber visual system.
- Unified Hub with the former Overview concept and reduced Memory density.
- No production API wiring or live-main merge is included.

---

## [Sprint 7 — Pillar 1/2/3/4: Safety & Autonomy Hardening] — IN PROGRESS (2026-08-22)

Four pillars shipped on local branches, awaiting human review & merge per CLAUDE.md.

### Pillar 4 — Self-Modification Risk Tier Hardening (`sprint7/selfmod-safety`)
**Branch:** `sprint7/selfmod-safety` @ `99b63bb`

Pre-flight safety validation before any self-modification proposal reaches the modifier or the approval gate. Five independent, composable checks; table-driven tier policy that auto-escalates AUTO → REVIEW on warning findings and REVIEW → MANUAL on critical findings. Configurable via env (`HIVE_SELFMOD_ENABLE_SAFETY_CHECKS`, `HIVE_SELFMOD_SAFETY_MAX_FILES`).

**New module:**
- `core/self_mod_safety.py` — `SafetyCheckResult`, `check_python_syntax`, `check_dangerous_patterns` (12 regex patterns incl. `rm -rf`, `eval(`, `subprocess.Popen`), `check_protected_paths`, `check_test_coverage`, `check_file_count`, `run_all_checks`, `should_reject_for_tier`, `apply_tier_policy`, `highest_severity`

**Wired into:**
- `core/spec_search.py` — `SelfImprovement.__init__` gains `safety_enabled`, `safety_max_files`, `safety_check_fn`, `audit`. `_apply_one` runs checks before `SelfModifier.propose()`. `apply_approved` re-runs safety as final guard.
- `core/config.py` — `selfmod_enable_safety_checks` (default True), `selfmod_safety_max_files` (default 20)

**Tests:** 50 new in `tests/test_self_mod_safety.py`. Full suite **3956 passed, 4 skipped**, ruff clean.

### Pillar 3 — Learned Skills (`sprint7/learned-skills` @ `fa193e8`)
**Branch:** `sprint7/learned-skills`

Pattern detection over audit log → SkillTemplate generation → human approval → registry registration. Hive can now learn new capabilities from observed tool-call sequences.

**New module:**
- `tools/learned_skills.py` (568 LOC) — `SkillTemplate`, `detect_patterns()` (sliding window over `ok` audit rows), `propose_skill()` (DAG-safe body that only calls existing tools), `LearnedSkillStore` (SQLite on `cfg.state_db`), `LearnedSkill(BaseTool)` runtime wrapper

**Gateway endpoints:**
- `GET /skills/learned`, `GET /skills/learned/{id}`, `POST /skills/learned/propose`, `POST /skills/learned/{id}/approve`, `POST /skills/learned/{id}/reject`, `POST /skills/learned/detect`

**Tests:** 18 new in `tests/test_learned_skills.py`.

### Pillar 2 — Approval Gate Hardening (`sprint7/approval-hardening` @ `c1e4aed`)
**Branch:** `sprint7/approval-hardening`

TTL-based expiration, emergency stop (kill-switch), structured audit history, batch approval.

**New module:**
- `core/approval_enhancements.py` (280 LOC) — `ExpirationPolicy`, `KillSwitch` (threading.Event), `AuditRecord`, `resolve_with_history()`, `resolve_batch()`, `sweep_expired()`, `engage_kill_switch()`, `release_kill_switch()`

**New gateway endpoints:**
- `POST /approvals/expire`, `GET /approvals/emergency-stop`, `POST /approvals/emergency-stop`, `GET /approvals/history?tool=&outcome=&since=`

**Wired into:**
- `tools/executor.py` — kill-switch check + audit request hook
- `core/spec_search.py` — kill-switch check for REVIEW-tier self-mod

**Tests:** 14 new in `tests/test_approval_hardening.py`.

### Pillar 1 — Self-Improvement Loop Audit (`sprint7/learned-skills` @ `b431c44`)
**Branch:** `sprint7/learned-skills`

Four real bugs fixed in the symptom → diagnosis → proposal → PR → approval → applied loop.

- **Bug 1:** Success memory recording was dead code (`outcome.status == "pushed"` never matched; AUTO success returns `"applied"`). Hive never learned from successful self-improvements.
- **Bug 2:** Failure memory recording used wrong stage names. Modifier emits `"test"`, `"push"`, `"worktree"`, `"no_changes"` — not `"test_fail"`, `"push_fail"`.
- **Bug 3:** No cooldown on failure-triggered self-improve path. Heartbeat fired LLM diagnoser every tick once `recent_failures() >= threshold`.
- **Bug 4:** `apply_approved` failure detail lost test-log context.

**New tests:** 11 in `tests/test_self_improve_loop_e2e.py` covering full REVIEW-tier cycle, AUTO success recording, failure bucketing by stage, heartbeat cooldown, exception isolation, MANUAL tier no-op, empty-diagnosis no-op.

**New config:** `selfmod_failure_cooldown_sec` (default 1800s, env `HIVE_SELFMOD_FAILURE_COOLDOWN_SEC`).

---

## [Sprint 6 P-I Jarvis Front — Mission Control v1.0] — PR #94 (2026-06-30)

Holographic SH1 dashboard (Centre.jsx) with conic-gradient cyan/violet glass morphism. Theme.css + pages.css + 9 page placeholders. Closes #77. Full status snapshot in `docs/STATUS.md` (SPRINT_6 P-I section).

---

## [Sprint 6 P-G Kanban + Cleanup] — PR #88 + #95 (2026-06-29)

Mission Control Agents Kanban (5 columns: Queued/Running/Done/Failed/Archived). WS-live updates via `DelegateToSpecialist → bus`. Sprint 6 closed as v1.0 shipped.

---

## Older releases

See `docs/CHANGELOG.md` for the full history (Sprints 1–6).
