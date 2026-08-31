# Memory claim contract

**Status:** accepted design; implementation in progress.  
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

`MemoryLedger.remember()` stays backward compatible through keyword-only defaults. `MemoryLedger.correct()` requires an existing memory identity or stable key, explicit human actor, correction reason, and idempotency key. Duplicate idempotency keys return the prior outcome without another version or outbox operation.

## Retrieval policy

The canonical retrieval selector returns only the current version for each stable key and includes a compact explanation: version, provenance kind, source, confidence, freshness state, and correction link. A non-expired corrected claim outranks its predecessor. Expired records are omitted from normal recall unless an explicit audit/history request includes them.

The local fallback must either use this selector or apply equivalent version filtering; it must never return a stale legacy `knowledge` row after a canonical correction.

## Projection mapping

- **Obsidian:** render claim metadata in managed-note frontmatter. Existing manual-edit conflict handling remains unchanged and prevents overwrite.
- **Mnemosyne:** place stable Hive fields in metadata; map compatible veracity/freshness fields only. The normal receipt/quarantine policy remains unchanged.

## Additive migration and rollback

The migration only adds nullable/defaulted columns to `memory_versions`. Historical rows read as `unknown`, `0.5`, creation time, no expiry, and no correction link. New inserts must use named columns. Rollback is code rollback only: no columns or history are dropped.

## Required evidence before promotion

- legacy-schema migration without data loss;
- correction/idempotency/restart/concurrency tests;
- projection metadata and manual-note conflict tests;
- model-tool path proves exactly one canonical write and no direct provider bypass;
- canonical/local retrieval prefers a correction and explains the selected claim;
- sanitized Telegram correction evaluation cases;
- full isolated suite, lint, and compile checks green.