# Autonomy readiness gate

**Status:** quarantined as of 2026-08-31. This is the operational source of truth for enabling an unattended Hive heartbeat. Architecture and historical sprint documents describe code shape or prior plans; they do not authorize runtime activation.

## Current decision

Do not enable `HIVE_AUTONOMY_ENABLED` or `HIVE_AUTONOMOUS_SELFMOD_ENABLED`. Do not start `hive heartbeat` against a user/runtime database and do not enable the orchestrator service. Preserve the current state DB for audit; do not delete or replay its historical rows.

The present implementation has a durable queue but not a durable execution protocol. A restart can replay a running task, historical test failures can trigger self-improvement, and pending approvals/workflow state are not all durable. These are correctness risks, not merely missing telemetry.

## Release gates

| Gate | Required evidence | Status |
| --- | --- | --- |
| Test isolation | Full suite uses a test-only state DB; regression test proves the configured runtime DB is untouched. | In progress |
| Safe start | Preflight blocks a live heartbeat on contaminated/test state; failure signals use a durable cursor, time window, and source allowlist. | In progress |
| Exactly-once boundary | Task has idempotency key, worker lease, and compare-and-set state transitions. Expired leases only are recoverable. | Not started |
| Outcome state machine | `OK`, error, retry, cancellation, and pending approval map to distinct durable states. | Not started |
| Scheduler delivery | Cron/commitment occurrence creation and schedule advancement are atomic and idempotent. | Not started |
| Durable approval/run journal | Approval, task/run relationship, tool intent/result, and resume checkpoints survive restart. | Not started |
| Recovery tests | Deterministic crash points, two-worker contention, lost response, and restart tests pass. | Not started |
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