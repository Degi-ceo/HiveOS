# Autonomy readiness gate

**Status:** quarantined as of 2026-08-31. This is the operational source of truth for enabling an unattended Hive heartbeat. Architecture and historical sprint documents describe code shape or prior plans; they do not authorize runtime activation.

## Current decision

Do not enable `HIVE_AUTONOMY_ENABLED` or `HIVE_AUTONOMOUS_SELFMOD_ENABLED`. Do not start `hive heartbeat` against a user/runtime database and do not enable the orchestrator service. Preserve the current state DB for audit; do not delete or replay its historical rows.

The present implementation has durable queue, lease, approval, scheduler, and local-memory recovery primitives, but it has not yet earned unattended-operation approval. A restart cannot automatically replay an unsafe running task or uncertain approved call; both are quarantined. Remaining correctness risks include restart-safe self-modification recipes, external-provider receipts, and operational recovery evidence.

## Release gates

| Gate | Required evidence | Status |
| --- | --- | --- |
| Test isolation | Full suite uses a test-only state DB; regression test proves the configured runtime DB is untouched. | In progress |
| Safe start | Preflight blocks a live heartbeat on contaminated/test state; failure signals use a durable cursor and production source allowlist. Time-window policy remains pending. | Partial |
| Exactly-once boundary | Worker lease, owner-checked terminal transitions, durable enqueue idempotency keys, and a persisted default-deny replay flag are implemented. Only expired tasks marked replay-safe at enqueue are recoverable; all other expired leases are quarantined for review. | Partial |
| Outcome state machine | `done`, `failed`, `canceled`, and `waiting_approval` are distinct durable states. Bounded exponential retry is limited to tasks explicitly declared replay-safe; default-deny failures remain manual. | Partial |
| Scheduler delivery | Cron and commitments use one SQLite transaction for the queue write plus cursor advance; fault-injection rollback tests prove that neither half persists alone. Stable occurrence keys remain as compatibility protection for historical partial rows. | Partial |
| Durable approval/run journal | Approval IDs are persisted on waiting tasks; approve → done, reject/TTL/kill → canceled, and an unconfirmed post-approval result → requires_review. Approved tool calls persist an execution marker before invocation; confirmed results are durable, while a restart or unconfirmed result is quarantined for review without replay. Full per-provider receipts and resume checkpoints remain pending. | Partial |
| Recovery tests | Deterministic task, approval, memory-projection crash points, two-worker contention, lost response, and restart tests pass. | Partial — task/approval/memory regression coverage exists; full cross-provider matrix remains. |
| Operations | SQLite backup/integrity/restore drill; Windows supervision; 24–72h read-only shadow soak. | Not started |

## Rollout

1. Keep both autonomy flags false while implementing and testing the gates.
2. Run a read-only/shadow heartbeat using an isolated state DB; it may plan and record evidence, but not invoke effectful tools or self-modification.
3. Run a 24–72 hour soak with restart and fault-injection evidence.
4. Enable only local low-risk actions under durable approval and budget limits.
5. Keep external side effects, deployments, spending, and merge-to-main behind explicit human approval permanently.

## Rollback

Disable both autonomy flags and stop the orchestrator service. Do not purge the database. Preserve the SQLite WAL files, inspect the run/task journal, and restore only from a verified backup if integrity is lost.

## Acceptance evidence

A gate is complete only when its focused tests, the affected integration tests, lint/compile checks, and a real dry-run are green on the candidate commit. A passing historical test count or a code review alone is insufficient.