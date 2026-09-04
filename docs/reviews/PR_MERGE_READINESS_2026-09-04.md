# Pull-request merge readiness — 2026-09-04

This record is evidence for the locally reviewed state only. It does not claim
that hosted CI has passed for commits which have not been pushed. No secrets,
message payloads, or credentials are recorded here.

## Remote inventory

| PR | Remote state | Local reviewed follow-up | Merge condition |
|---|---|---|---|
| #107 — Gpt UI improvements | Draft, `CLEAN`; prior hosted checks green | `20c7507`, `a239d84`: Windows screenshot archive compatibility and generated-archive ignore | Push reviewed commits, run fresh CI, remove Draft, then human merge review |
| #117 — approval task lifecycle | `UNSTABLE`; remote CI is stale and failed | `f5ce598`: terminal approval decisions close durable tasks; focused approval/task tests passed | Push reviewed commit and require fresh green CI |
| #118 — learned skills declarative runtime | `UNSTABLE`; remote CI is stale and failed | `63525fb`: align smoke tests with declarative, non-executing learned-skill runtime | Push reviewed commit and require fresh green CI |

## Issue inventory

- **#78** — locally completed on `review/issue78-j4`; commits `b4455a9` through
  `fa8ba94` cover J4–J8, CLI safety tests, architecture test reliability, and
  documentation. It needs a separate pushed branch and PR before hosted CI can
  prove merge readiness.
- **#39** — explicitly labelled deferred/tracking. It contains no authorised,
  bounded implementation request, so it remains open rather than being closed
  or expanded speculatively.

## Local evidence

- `pytest -q tests/test_architecture.py tests/test_cli_commands.py
  tests/test_cli_foundation.py` — **115 passed**.
- `ruff check tests/test_architecture.py src/hive/surfaces/cli
  tests/test_cli_commands.py` — passed.
- `git diff --check` — passed before commits.
- Dashboard branch: unit tests, preview build, preview E2E, and screenshot
  package checks were previously run locally; hosted CI must be rerun after its
  two follow-up commits are pushed.

## Deliberate boundaries

No branch was pushed, merged, deployed, or used for a real outbound/channel
smoke. A push is intentionally a separate owner-authorised action because it
changes the remote review surface and triggers hosted workflows. Merge remains
human-controlled after fresh CI.