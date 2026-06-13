# ADR 005 — REVIEW-tier edits stored in process memory (`edit_pending`)

**Status:** Accepted  
**Date:** 2026-06-13  
**Deciders:** Kamil (owner), Hive (architect)

---

## Context

When `SelfImprovement` proposes a REVIEW-tier edit (e.g. `op: patch_code`), the edit cannot be applied immediately — it must wait for human approval via `POST /approvals/decide`. The approval gate stores the approval metadata (tool name, args, `approval_id`) in `gate._pending`. But the `Edit` object itself — specifically its `apply` callable — must be retrievable when the human later approves it.

The `apply` callable is a Python closure. It cannot be serialized to SQLite or JSON without reflection hacks.

Options considered:

| Option | Serializable? | Survives restart? | Complexity |
|---|---|---|---|
| In-process dict on `HiveOS` (`edit_pending`) | No (closure) | No | Zero |
| SQLite with pickled closure | Sort of | Yes | High (pickle security, schema migration) |
| SQLite with re-runnable recipe (op + path + text) | Yes | Yes | Medium (re-derive apply_fn from stored recipe) |
| Redis with TTL | Sort of | Configurable | High (new dependency, TTL tuning) |

---

## Decision

**Store REVIEW-tier `Edit` objects in an in-process dict on the `HiveOS` dataclass: `edit_pending: dict` (keyed by `approval_id`).**

- When `SelfImprovement._apply_one` encounters a REVIEW-tier edit, it calls `gate.request(tool, args)` to generate an `approval_id`, then stores `edit_pending[approval_id] = edit`.
- When the human calls `POST /approvals/decide {approval_id, approved: true}`, `gateway/app.py` checks whether the tool name starts with `self_mod:`. If so, it pops the `Edit` from `edit_pending` and calls `improver.apply_approved(edit)` (routes through `SelfModifier.propose`). If not, it routes to `tool_executor.execute_approved` (the regular dangerous-tool path).
- Rejected edits: `edit_pending.pop(approval_id, None)` cleans up immediately.

The dict is initialized as `{}` in `HiveOS.build()`. It holds only live, unapplied REVIEW edits — typically zero or one at a time.

---

## Consequences

**Good:**
- Zero implementation complexity: `dict` assignment, no schema, no migration, no serialization.
- The closure's full context (file paths, text transforms, any captured state) is preserved exactly as the diagnoser created it — no lossy re-serialization.
- Security: the closure is never exposed to external input. The approval gate receives only the `approval_id` UUID from the client; the actual `apply` function comes from the trusted in-process store.
- Keeping REVIEW edits rare and short-lived (human reviews quickly or edit is re-triggered on next heartbeat failure detection) means the loss-on-restart risk is low in practice.

**Bad / trade-offs:**
- **Process restart loses pending edits.** If the gateway restarts between `gate.request` and the human's `POST /approvals/decide`, the `approval_id` becomes invalid. The human sees a 404 or "edit not found" error. The fix: re-trigger the self-improvement symptom (the heartbeat will re-detect the failure and re-propose).
- The `GET /approvals` endpoint shows the gate's `_pending` dict (approval metadata), but a separate UI check would be needed to show how many `edit_pending` entries exist. Currently `len(hive.edit_pending)` is not exposed as a metric (low priority — the dashboard's approval inbox already shows pending approvals).
- In-memory storage means no audit trail of *which* REVIEW edits were proposed, approved, or rejected beyond the current process lifetime. The audit log records the tool call that triggered the edit, but not the `Edit` object itself.

---

## The restart risk in practice

The window between `gate.request` (REVIEW edit queued) and human approval is:
- Short: typically minutes to hours (Kamil checks the dashboard at regular intervals).
- Visible: the approval inbox shows the pending item; if it disappears after a restart, Kamil notices.
- Self-healing: the heartbeat detects repeated failures independently on each tick. If the edit that fixed the failure was not applied (because the process restarted), the symptom recurs, and the heartbeat re-triggers the diagnosis on the next tick, generating a new `approval_id`.

This is an acceptable trade-off for the zero-complexity implementation. The alternative (SQLite-persisted re-runnable recipes) would require careful schema design to avoid serializing arbitrary Python callables and would add 200+ lines of migration and deserialization code — disproportionate to the actual risk.

---

## Alternatives considered

**SQLite with re-runnable recipe:** Store `(op, path, old_text, new_text)` instead of the closure. Survives restart. But the diagnoser can produce edits that are not simple text replacements (e.g. future op types). Would require a schema migration every time a new `EditOp` is added. Medium complexity.

**Pickle + SQLite BLOB:** Serializes the closure directly. Insecure (pickle is arbitrary code execution on load), fragile across Python versions, and adds a dependency. Rejected.

**Redis with TTL:** External dependency. TTL tuning is error-prone. No benefit over the in-process dict for a single-process system.

---

## See also

- [`src/hive/runtime.py`](../../src/hive/runtime.py) — `HiveOS.edit_pending` field + wiring
- [`src/hive/core/spec_search.py`](../../src/hive/core/spec_search.py) — `SelfImprovement._apply_one` (REVIEW path)
- [`src/hive/gateway/app.py`](../../src/hive/gateway/app.py) — `/approvals/decide` routing
- [`docs/SECURITY.md`](../SECURITY.md#review-tier----op-patch_code-new_tool-add_capability-refactor) — REVIEW tier security model
- [`docs/GLOSSARY.md`](../GLOSSARY.md#e) — `edit_pending` definition
