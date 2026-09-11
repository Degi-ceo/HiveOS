# Learning Loop — operator manual (SPRINT_6 P-F)

> Status: **shipped** in `sprint6/learning-loop` branch. Closes #74.

The **learning loop** adds an eval-gated self-improvement loop on top of
HiveOS's existing `self_improve_from_symptom()` flow. Without the loop,
self-modifications are gated by tests and the PR review boundary. With the
loop enabled, a candidate change is additionally **rejected** if it regresses
`pytest` or the `evals/datasets/runtime_smoke.jsonl` real-runtime evals. Rejected
candidates are still persisted (for analysis) but never applied.

This file explains: how the loop works, how to enable it, how to read
its history, and what to do when something goes wrong.

## TL;DR

```bash
# Enable the loop
export HIVE_LEARNING_LOOP_ENABLED=true
export HIVE_SANDBOX_IMAGE=python:3.12
export HIVE_LEARNING_EVAL_TIMEOUT=60   # seconds; default 60
export HIVE_LEARNING_REGRESSION_THRESHOLD=0  # strict default; range 0..1

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
  evolver.py    — legacy proposal compatibility (not the runtime gate)
  evaluator.py  — runs pytest + real-runtime evals on candidate worktree
  loop.py       — in-worktree comparator + durable verdict evidence
```

The production flow on every self-modification while learning is enabled:

1. **Tracer** collects recent failing traces (`outcome ∈ {error, denied}`
   in the last 60 minutes).
2. **SelfModifier** creates one isolated candidate worktree, applies the edit,
   verifies its changed paths, and runs its normal test command.
3. **Evaluator** scores that same still-live worktree and a baseline bound to
   the exact base commit, dataset hash, and target version. Baseline scoring
   refuses a dirty or mismatched repository, so the stored score always describes
   the named commit. Runtime evaluation
   goes through the same Docker runner as autonomous self-modification: networking
   is disabled and only the candidate repository is mounted. The subprocess imports
   code from the mounted candidate's `src/` and runs a complete isolated HiveOS.
4. **Evaluator.compare()** returns `Verdict(accept | reject, reason)` with
   pytest/eval deltas. Any missing evidence, error, regression, or incomplete
   runtime suite rejects.
5. On **accept**, SelfModifier compares a content digest covering tracked and
   untracked candidate files before and after the gate, so a test or evaluator
   cannot mutate the candidate after measurement. It then continues with staged
   secret scanning, commit, push, and draft PR creation. It never merges.
6. On **reject**, a correlated `LoopOutcome` is persisted and SelfModifier
   removes the candidate without commit or push.

The legacy direct `LearningLoop.run()` entry point rejects if no real candidate
applier is injected; it cannot record an accepted no-op.

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `HIVE_LEARNING_LOOP_ENABLED` | `false` | Master gate. When false, the loop is constructed but never invoked. |
| `HIVE_SANDBOX_IMAGE` | empty | Required when the loop is enabled. Startup fails closed if no image is configured. |
| `HIVE_LEARNING_EVAL_TIMEOUT` | `60` | Per-gate timeout (pytest + evals). On timeout, the gate counts as failed (pass_rate = 0). |
| `HIVE_LEARNING_REGRESSION_THRESHOLD` | `0` | Maximum tolerated per-metric regression and runtime-eval failure fraction, from `0` to `1`. The default is strict. |

The runtime invokes the evaluator only after SelfModifier's configured test command
has passed, so it does not repeat that full test command inside the evaluation timeout.
Standalone evaluator use keeps the pytest gate enabled by default. Every evaluator
subprocess receives a credential-stripped environment. The sandboxed evaluator uses
Python isolated mode (`-I`), applies a whole-process timeout, and force-removes its
named Docker container when cancellation occurs. Runtime candidates may not
change `.github/workflows/`, `evals/`, `src/hive/evals/`, or
`src/hive/core/learning/`, nor Python/pytest bootstrap configuration such as
`sitecustomize.py`, `conftest.py`, `pyproject.toml`, or `src/hive/__init__.py`;
these paths form the evaluation control plane and require a separately reviewed
development change.

For an operator-funded run of the complete 30-case golden dataset against Hive's
configured live model and an explicit model judge:

```bash
hive eval run evals/datasets/golden_qa.jsonl --target hive --judge target --concurrency 1
```

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
| `run_id` | TEXT | originating operation UUID |
| `candidate_digest` | TEXT | SHA-256 of the candidate diff |
| `pytest_delta` / `evals_delta` | REAL | candidate minus baseline |

The `evaluation_baselines` table is keyed by base commit SHA, dataset SHA-256,
and target id/version. Existing rows are immutable (`INSERT OR IGNORE`).

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `evaluator raised: …` | pytest or evals runner crashed (missing dep, OOM) | Increase `HIVE_LEARNING_EVAL_TIMEOUT`; check the runner logs |
| `evaluation: … regression` | Candidate score is below its commit-scoped baseline | Inspect the persisted deltas and candidate run id |
| `evaluation: baseline.error` | Baseline could not be measured | Repair the eval environment; never waive missing evidence |
| `test: …` | The candidate worktree breaks pytest | Inspect the redacted self-mod failure evidence |
| `protected` | Proposed edit touches `Config/SOUL.md` or `Core/approval_gate.py` (HARD-LOCKED) | Human-only change; do not auto-propose |
| `learning_loops` table empty | Loop is disabled, or never invoked | Set `HIVE_LEARNING_LOOP_ENABLED=true` and trigger `/learning/run` |
| `pass_rate=0.0` on every run | Dataset or pytest collection broken | Confirm `runtime_smoke.jsonl` exists and run `hive eval run ... --target hive-runtime` |

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
