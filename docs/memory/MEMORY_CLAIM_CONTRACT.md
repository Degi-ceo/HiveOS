# Memory claim contract

**Status:** implemented and regression-tested.
**Authority:** the canonical Hive SQLite ledger is the only source of truth. Mnemosyne and the managed Obsidian `Hive-Shadow` subtree are derived projections.

## Invariants

- A claim is append-only. A correction creates a successor version; it never deletes or mutates the prior claim.
- Every configured-provider write, including model memory tools, enters the ledger before any projection.
- A model cannot silently label its own assertion as a human correction or assign itself high trust. Human corrections require an explicit actor and non-empty reason.
- An expired claim remains audit-visible. Freshness changes retrieval eligibility or ranking; it never erases history.
- Unknown external delivery outcomes remain `requires_review`. Quality metadata does not make a remote write replay-safe.

## Claim fields

Fields belong to a specific immutable `memory_versions` row, not the mutable current pointer.

| Field | Type | Default for existing/new ordinary records | Meaning |
| --- | --- | --- | --- |
| `source` | string | existing value | Free-form origin label, retained for compatibility. |
| `provenance_kind` | enum | `unknown` | `human`, `agent`, `tool`, `imported`, `system`, or `unknown`. |
| `confidence` | number | `0.5` | Caller-supplied `0.0..1.0`; it is not an autonomous truth score. |
| `observed_ts` | nullable timestamp | version creation time | When the claim was observed to hold. |
| `fresh_until_ts` | nullable timestamp | null | Optional validity boundary; expiry downgrades/excludes retrieval but retains the claim. |
| `veracity` | enum | `unknown` | `stated`, `inferred`, `tool`, `imported`, or `unknown`. |
| `correction_of_version` | nullable integer | null | Prior version replaced by this human correction. |
| `correction_reason` | nullable string | null | Human-supplied reason for the correction. |

## Write and correction state machine

```text
remember → immutable version N → ordered projections
human correction → validate actor/reason → immutable version N+1
                 → event=corrected → current pointer=N+1 → ordered projections
unknown external result → requires_review (never automatic retry)
```

`MemoryLedger.remember()` stays backward compatible through keyword-only defaults. `MemoryLedger.correct()` requires an existing memory identity or stable key, explicit human actor, correction reason, and idempotency key. Duplicate idempotency keys return the exact version created by the original event, even after later corrections; they never create another version or outbox operation. The owner-facing entry point is Telegram `/correct <stable-key> | <claim> | <reason>`: the authenticated gateway derives the human actor and update-bound idempotency key, so the model cannot invoke or impersonate this path.

## Retrieval policy

`MemoryLedger.recall_current()` returns only the current version for each stable key and includes a compact explanation: version, provenance kind, source, confidence, freshness state, and correction link. Both configured providers use the canonical selector instead of their legacy or remote search path. Static system context is stricter: it includes only non-expired, durable `fact`/`skill`/`mcp`/`research`/`fix` claims with `human`, `system`, or `tool` provenance; session transcripts and ordinary agent memory are excluded. Prefetch context remains query-scoped but is labelled as untrusted reference data. An empty canonical result is authoritative and never permits a legacy fallback. A non-expired corrected claim outranks its predecessor. An expired current record is omitted from normal recall and never causes an older version to be resurrected; an explicit audit/history request may include it.

The local fallback must either use this selector or apply equivalent version filtering; it must never return a stale legacy `knowledge` row after a canonical correction.

## Projection mapping

- **Obsidian:** render the current claim metadata in managed-note frontmatter and preserve each immutable version under `Hive-Shadow/_System/history/<memory-id>/vN.md`. A manual edit to the current note, the incoming history version, or any earlier managed history version quarantines the new projection; Hive never overwrites the evidence. Model-facing Obsidian read/search/list tools are scoped to the managed `Hive-Shadow` subtree; user-authored vault notes remain outside the canonical-memory trust boundary.
- **Mnemosyne:** place stable Hive fields in metadata, including correction link and reason; map compatible veracity/freshness fields only. The normal receipt/quarantine policy remains unchanged.

## Operator diagnostics and restart recovery

`MemoryLedger.projection_summary()` and authenticated `GET /memory/projections` expose only counts by target and state (`pending`, `running`, `applied`, `requires_review`, `unknown`). They intentionally never return claim content, stable keys, operation IDs, bindings, workers, leases, or provider error text. Telegram `/memory` displays only aggregate open and owner-review counts.

Startup and explicit restart recovery first quarantine expired non-replay-safe external leases without invoking Mnemosyne, then drain only deterministic Obsidian work. Therefore an interrupted external delivery becomes visible as `requires_review`, never a silently replayed call.

## Additive migration and rollback

The migration only adds nullable/defaulted columns to `memory_versions`. Historical rows read as `unknown`, `0.5`, creation time, no expiry, and no correction link. New inserts must use named columns. Rollback is code rollback only: no columns or history are dropped.

## Required evidence before promotion

- legacy-schema migration without data loss;
- correction/idempotency/restart/concurrency tests;
- projection metadata, immutable-history tamper, and manual-note conflict tests;
- model-tool path proves exactly one canonical write and no direct provider bypass;
- canonical/local retrieval prefers a correction and explains the selected claim;
- sanitized Telegram correction evaluation cases;
- full isolated suite, lint, and compile checks green.
