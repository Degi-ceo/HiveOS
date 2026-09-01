# Hive autonomy roadmap — controlled Telegram pilot

**Decision date:** 2026-08-31
**Owner:** Kamil (human approval)
**Scope:** the local `H:\HiveOS` installation; Hive is the main agent.

## Purpose and boundary

The goal is a Jarvis-like assistant that can remember, diagnose, learn, propose improvements, and carry out bounded low-risk work. It is **not** a licence for an unbounded process to spend money, send external messages, deploy, change protected files, merge `main`, or silently alter its own authority.

This roadmap intentionally separates three capabilities that are often conflated:

| Capability | Target in this roadmap | Human role |
| --- | --- | --- |
| Conversation and recall | Telegram is the primary private surface; the agent recalls durable context and writes new learning through the canonical ledger. | Talks with Hive; can correct memories. |
| Bounded autonomous maintenance | Only explicit, local, replay-safe and observable work is eligible after every release gate has evidence. | Sets scope, reviews health and exceptions. |
| Self-development | Hive diagnoses a bounded symptom, researches first, tests a worktree, and opens a draft PR. | Reviews and merges every change; never delegated to the agent. |

`Config/SOUL.md` and `Core/approval_gate.py` remain protected. `HIVE_AUTONOMY_ENABLED` and `HIVE_AUTONOMOUS_SELFMOD_ENABLED` remain `false` throughout the first two phases. The operational release authority remains [`AUTONOMY_READINESS.md`](AUTONOMY_READINESS.md).

## Current baseline — do not rebuild it

The following is already implemented and is the foundation for the next batches:

- Private Telegram webhook admission requires a token, webhook secret, and allowlisted numeric user IDs. Durable inbound records prevent a duplicate webhook from re-running a turn; uncertain delivery is marked `ambiguous` rather than resent blindly.
- SQLite task and approval journals have worker leases, fenced terminal writes, idempotency keys, default-deny replay, and crash quarantine. Scheduler enqueue and cursor advance share a transaction.
- The canonical memory ledger owns Mnemosyne and Obsidian projections. It has owner-fenced claims and version ordering. Only deterministic local Obsidian `Hive-Shadow` writes may recover automatically; uncertain external Mnemosyne outcomes require review.
- The configured Obsidian vault is a derived long-term representation, not a second mutable source of truth. Its managed subtree is bounded and manual-note conflicts are preserved.
- Self-modification uses a separate worktree, tests, a pushed draft PR, and a human merge. Interrupted recipes are quarantined.
- `hive shadow` and `scripts/windows/shadow-soak.ps1` are read-only evidence tools. They do not create a runtime, execute tasks, call Telegram, project memory, or modify code.
- The heartbeat has a fail-closed IANA-local time window. Empty or invalid configuration denies execution.

## Phased delivery plan

### Phase 0 — preserve evidence and prove the baseline

**Objective:** know that development and diagnostics cannot mutate the real runtime state.

1. Run the complete test suite only with its test-private databases; record the actual command, exit code, test count, and commit. Historical counts are not release proof.
2. Verify the real state database and take an online backup. Keep the current historical rows intact; do not replay, cancel, or purge them.
3. Run the read-only shadow helper at a documented cadence for 24–72 hours. Preserve JSON output and exit codes, including restart and fault-injection observations.
4. Do not install a Windows task that runs a heartbeat, Telegram delivery, restore, or self-modification. A backup-only or shadow-only operator task is the maximum allowed supervision in this phase.

**Exit evidence:** clean full-suite result, verified backup plus restore drill, no unexplained shadow observations, and a reviewed operational log.

### Phase 1 — private Telegram conversation and memory pilot

**Objective:** Kamil can talk to Hive from Telegram while the system learns safely.

1. Confirm the Telegram settings through `hive doctor` without displaying secrets: bot token present, webhook secret present, and Kamil's numeric Telegram ID in the allowlist. Reject all other users and unauthenticated webhook calls.
2. Start only the gateway. Do not start `hive heartbeat`. Exercise a small manual matrix: ordinary Polish conversation, recall from a later conversation, an explicit memory correction, duplicate webhook delivery, and a deliberately refused dangerous request.
3. Store learned facts through the canonical ledger and project only the managed `Hive-Shadow` subtree of the vault. Treat a Mnemosyne receipt error or manual-note conflict as `requires_review`; never auto-retry an uncertain external write.
4. Review the audit trail, session continuity, memory record, and vault projection after each test. Do not put tokens, private chat text, or credentials in Git or the vault.

**Exit evidence:** authenticated inbound messages work for the allowlisted owner; duplicate delivery has one turn outcome; recall and correction are traceable; all failed external projections are visible and quarantined.

### Phase 2 — safe learning loop without autonomous execution

**Objective:** Hive can turn feedback and failures into durable, reviewable proposals.

1. Implement [`MEMORY_CLAIM_CONTRACT.md`](memory/MEMORY_CLAIM_CONTRACT.md): source, confidence, freshness, conflict status, and a human correction path for every durable knowledge claim. Retrieval must show why a fact was selected and prefer corrected over superseded material.
2. Add evaluation fixtures made from sanitized, owner-approved Telegram scenarios: recall, contradiction handling, tool refusal, safe task planning, and Polish interaction quality. **Implemented:** `telegram_safe_learning_v1` runs deterministically offline, creates content-free durable aggregate evidence, and must pass freshly before the opt-in learning-loop diagnosis path. It does not authorize an edit or any autonomous action.
3. Add bounded diagnosis: only allowlisted production-origin symptoms, durable cursor, rate limit, budget limit, and a no-op/dry-run mode. A diagnosis may create a draft proposal but cannot execute an edit outside the existing approval flow.
4. Require independent review of every proposed self-change; candidate worktree tests, lint, and relevant focused integration tests must pass before a draft PR is opened.

**Exit evidence:** reproducible evaluation report, no regression in memory recovery or Telegram idempotency, and at least one dry-run diagnosis that produced a fully auditable non-production proposal.

### Phase 3 — durable operation hardening

**Objective:** close the remaining correctness gaps before any autonomous task is considered.

1. Provider boundary: require an audited idempotency or receipt contract for every external memory provider and side-effecting tool. Without that contract, persist the intent and quarantine unknown results; do not claim exactly-once delivery.
2. Recovery matrix: test process death at claim, external-call, receipt, binding, approval, task completion, and restart points; include two workers and out-of-order memory versions. Prove that no uncertain side effect is automatically duplicated.
3. Scheduler evidence: enforce one canonical SQLite database for `TaskBoard`, cron, and commitments; then test a failure after task insertion but before cursor update and two-scheduler contention. Do not use cross-file SQLite transactions as a substitute.
4. Operations: document and test Windows process supervision only after the shadow soak, backup/restore drill, logs, alerts, and explicit stop procedure are accepted.
5. Security: continue boundary tests for Telegram authentication, path containment, secrets redaction, approval linkage, and self-mod protected-file rejection.

**Exit evidence:** all rows in [`AUTONOMY_READINESS.md`](AUTONOMY_READINESS.md) have fresh evidence, not merely code or prior tests. The owner explicitly approves a new operating mode.

### Phase 4 — tightly scoped autonomous maintenance

**Objective:** optional, local, reversible maintenance only.

If and only if the owner approves after Phase 3, enable a narrow execution window and a small allowlist of local replay-safe tasks. Start with a dry-run or approval-required mode; enforce one worker, budget ceilings, rate limits, a kill switch, and notifications. External messaging, deployments, payments, credential handling, destructive actions, protected-file changes, and merging `main` remain permanently human-approved.

Self-development remains: symptom → evidence → discovery-first research → isolated worktree → tests → draft PR → independent review → Kamil merges. The system must record failed attempts and their lessons, but it must not retry a risky or uncertain action just because it remembers it.

## Next implementation batch — priority order

1. **Heartbeat time-window proof and truthful docs.** Add focused tests that prove an empty/invalid window blocks a tick before scheduling, claiming, or planning. Keep the implementation fail-closed. Reconcile `ARCHITECTURE.md` and `STATUS.md` with this behavior.
2. **Telegram-pilot readiness report.** Add a non-secret diagnostic/reporting path and manual acceptance checklist for gateway-only use. It must never send a test message, enable a webhook, or expose configuration values.
3. **Memory provenance and correction design.** First write the schema/API contract and migration/rollback plan for source, confidence, freshness, supersession, and correction; then implement it with ledger ordering and recovery tests.
4. **Evaluation harness for learning.** **Implemented:** sanitized, versioned offline acceptance cases cover recall, correction, refusal, safe task planning, and Polish quality; fresh all-pass evidence gates the opt-in diagnosis path. Next: add explicitly owner-approved model evaluation only in an isolated environment.
5. **Provider-receipt and recovery matrix.** Only after the contract is explicit should new retry/recovery code be implemented. Unknown external outcomes stay quarantined.

## What Kamil does now

1. Use Telegram only after the Phase 1 gateway-only checklist is completed; keep both autonomy flags false.
2. Tell Hive explicitly when a remembered fact is wrong; that correction becomes a high-value test case for Phase 2.
3. Review the draft PRs Hive creates. A PR is the learning artefact; merging remains the human decision.
4. Treat an approval request, `requires_review`, `ambiguous`, a budget warning, or a shadow anomaly as a request to inspect evidence, never as a reason to bypass a gate.

## Success metrics

| Area | Metric before promotion |
| --- | --- |
| Telegram | 100% unauthenticated/non-allowlisted inbound requests rejected; duplicate delivery produces one durable turn outcome. |
| Memory | Every recalled durable fact has provenance; corrections supersede prior claims without deletion; uncertain external projection is visible for review. |
| Learning | Every proposed change links to a bounded symptom, tests, evaluation result, and draft PR; no protected file or `main` is changed. |
| Operations | Full isolated suite, backup/restore drill, and 24–72h read-only shadow evidence all pass on the candidate commit. |
| Autonomy | No action outside an explicit local allowlist, time window, budget, and approval policy; a kill switch stops new work immediately. The implemented policy catalog is deterministic and evidence-only: historical approvals cannot lower future requirements. |
