# Learning Loop — operator manual (SPRINT_6 P-F)

> Status: **shipped** in `sprint6/learning-loop` branch. Closes #74.

The **learning loop** adds an eval-gated self-improvement loop on top of
HiveOS's existing `self_improve_from_symptom()` flow. Without the loop,
self-modifications are gated only by human review on the PR. With the
loop enabled, a candidate change is **rejected** if it regresses
`pytest` or the `evals/datasets/golden_qa.jsonl` evals. Rejected
candidates are still persisted (for analysis) but never applied.

This file explains: how the loop works, how to enable it, how to read
its history, and what to do when something goes wrong.

## TL;DR

```bash
# Enable the loop
export HIVE_LEARNING_LOOP_ENABLED=true
export HIVE_LEARNING_EVAL_TIMEOUT=60   # seconds; default 60

# Restart the gateway
hive serve

# Trigger a manual loop iteration via the API
curl -X POST http://localhost:8088/learning/run \
     -H "X-Hive-Token: $HIVE_SECRET" \
     -H "content-type: application/json" \
     -d '{"symptom":"missing tool discovery for new plugin"}'

# Inspect history
hive learning status
hive learning replay 1
curl -s -H "X-Hive-Token: $HIVE_SECRET" \
     http://localhost:8088/learning/history?limit=20 | jq
```

The loop is **off by default**. Existing `self_improve_from_symptom()`
behavior is preserved when the flag is unset.

## Architecture (4 modules + wire-in)

```
core/learning/
  storage.py    — SQLite helpers for learning_traces + learning_loops
  tracer.py     — observes tool-call outcomes into learning_traces
  evolver.py    — wraps SelfModifier.propose(), produces Proposal
  evaluator.py  — runs pytest + evals on candidate worktree
  loop.py       — orchestrator: trace → evolve → eval → apply(guarded)
```

The flow on every `LearningLoop.run(symptom)`:

1. **Tracer** collects recent failing traces (`outcome ∈ {error, denied}`
   in the last 60 minutes).
2. **Evolver** calls `SelfModifier.propose(dry_run=True)`, which creates a
   `hive/learning-<ts>` branch, runs the apply-fn, runs pytest, and
   reports back. **No PR is opened at this stage.**
3. **Evaluator** scores the candidate worktree (pytest + evals) and the
   baseline (current main).
4. **Evaluator.compare()** returns `Verdict(accept | reject, reason)`.
   **Accept** iff candidate_evals ≥ baseline_evals AND candidate_evals
   == 1.0 (golden_qa is mandatory).
5. On **accept**, the loop opens a draft PR via the existing
   `SelfModifier.propose(dry_run=False)` path. (Auto-merge only fires
   if `HIVE_LEARNING_AUTOPROMOTE=true` — off by default for safety.)
6. On **reject**, the loop persists a `LoopOutcome(verdict=reject, ...)`
   row to `learning_loops`. **No PR is opened.** No code is touched.

All errors are caught — `loop.run()` NEVER raises to the caller.

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `HIVE_LEARNING_LOOP_ENABLED` | `false` | Master gate. When false, the loop is constructed but never invoked. |
| `HIVE_LEARNING_EVAL_TIMEOUT` | `60` | Per-gate timeout (pytest + evals). On timeout, the gate counts as failed (pass_rate = 0). |
| `HIVE_LEARNING_AUTOPROMOTE` | `false` | (Future) when true, the loop self-merges an accepted PR after CI green. **Off by default** — only humans merge today. |

The learning tables (`learning_traces`, `learning_loops`) live in the
same SQLite database as `task_board` (`HIVE_STATE_DB`, default
`data/hive.sqlite`). Schema is created lazily on first call via
`CREATE TABLE IF NOT EXISTS` — no migration step.

## Heartbeat integration

`src/hive/autonomy/heartbeat.py` (line ~94) decides whether to invoke
self-improvement based on `task_board.recent_failures(limit=10)` count.
When `HIVE_LEARNING_LOOP_ENABLED=true`, the same threshold triggers the
loop instead of the legacy `self_improve_from_symptom()` flow:

```python
use_learning = bool(getattr(self._hive.config, "learning_loop_enabled", False))
outcomes = await self._hive.self_improve_from_symptom(
    symptom, use_learning_loop=use_learning,
)
```

The legacy path is preserved — heartbeats that don't opt-in behave
exactly as before.

## Operator endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/learning/status` | GET | Counts by verdict + 10 most-recent loop outcomes |
| `/learning/history?limit=N` | GET | Last N loop outcomes (newest first). Capped at 200. |
| `/learning/run` | POST | Manually trigger one iteration. Body: `{"symptom": "..."}` |

All three require `X-Hive-Token`.

## CLI

```
hive learning status           # counts + 10 most-recent loops
hive learning status --limit N # change how many recent loops are shown
hive learning replay <loop_id> # dry-run replay of one loop decision
hive learning replay 999       # returns rc=1 if not found
```

`hive status` now includes a `learning_loop : enabled/disabled` line.

## Tables

### `learning_traces`

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | autoincrement |
| `ts` | REAL | unix timestamp |
| `session_id` | TEXT | session that produced the trace |
| `tool` | TEXT | tool name |
| `args_json` | TEXT | JSON blob of args (already redacted by audit emit) |
| `outcome` | TEXT | `ok` \| `error` \| `denied` |
| `latency_ms` | REAL | wall-clock duration |
| `error_class` | TEXT (nullable) | exception class name on `error` |
| `error_message` | TEXT (nullable) | redacted message |

### `learning_loops`

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | autoincrement |
| `ts` | REAL | when the loop started |
| `symptom` | TEXT | input symptom |
| `verdict` | TEXT | `accept` \| `reject` |
| `pytest_baseline` / `pytest_candidate` | REAL | 0.0–1.0 pass-rate |
| `evals_baseline` / `evals_candidate` | REAL | 0.0–1.0 pass-rate |
| `worktree_branch` | TEXT (nullable) | candidate branch name |
| `pr_url` | TEXT (nullable) | populated on accept only |
| `reject_reason` | TEXT (nullable) | populated on reject only |

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `evaluator raised: …` | pytest or evals runner crashed (missing dep, OOM) | Increase `HIVE_LEARNING_EVAL_TIMEOUT`; check the runner logs |
| `apply raised after accept: …` | The `SelfModifier.propose(dry_run=False)` call failed (network, git error) | Inspect git state in `.worktrees/`; retry |
| `dry-run failed at stage=test` | The candidate branch already breaks `pytest`. Eval gate never runs. | Inspect the worktree at `.worktrees/hive/learning-<ts>/` |
| `dry-run failed at stage=protected` | Proposed edit touches `Config/SOUL.md` or `Core/approval_gate.py` (HARD-LOCKED) | Human-only change; do not auto-propose |
| `learning_loops` table empty | Loop is disabled, or never invoked | Set `HIVE_LEARNING_LOOP_ENABLED=true` and trigger `/learning/run` |
| `pass_rate=0.0` on every run | Dataset or pytest collection broken | Run `hive doctor` to confirm golden_qa.jsonl exists |

## Testing the loop manually

```bash
# 1. Confirm config
hive status | grep learning_loop
# learning_loop : disabled

# 2. Enable + restart
export HIVE_LEARNING_LOOP_ENABLED=true
hive serve &

# 3. Trigger a real loop iteration (use a benign symptom)
curl -X POST http://localhost:8088/learning/run \
     -H "X-Hive-Token: $HIVE_SECRET" \
     -H "content-type: application/json" \
     -d '{"symptom":"test dry-run"}' | jq

# 4. Read history
hive learning status
# accept count : 0
# reject count : 1
# Recent loops (last 1):
#     REJECT  id=1  pytest=0.00/0.00  evals=0.00/0.00  symptom=test dry-run

# 5. Inspect the reject reason
hive learning replay 1
# Loop 1 (recorded …)
# verdict       : reject
# symptom       : test dry-run
# reject_reason : dry-run failed at stage=test
```

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Eval timeout on slow CI | `HIVE_LEARNING_EVAL_TIMEOUT` (default 60s); on timeout the gate counts as failed |
| Loop proposes a destructive edit | SelfModifier's existing pytest gate catches it FIRST (no eval run needed) |
| Loop regresses itself via apply | `apply()` only fires after accept; reject path is no-op for state |
| Heartbeat tick becomes slow | Loop is opt-in (`learning_loop_enabled`); default off |
| Self-mod in worktree consumes LLM budget | `SelfModifier.propose()` already uses `TaskKind.AUX` |
| Operator confusion: what is this loop? | This file + `hive learning status` CLI as the entry point |

## See also

- [[sprint6-pb-evals-handoff]] — P-B (the comparator this loop depends on)
- [[sprint6-pc-tool-loop-stream-handoff]] — P-C (iteration visibility)
- [[docs/sprints/SPRINT_6_AUTONOMY_LIB]] § Phase F — original sprint spec
- Issue #74 — the P-F issue this PR closes