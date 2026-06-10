# HiveOS — STATUS (living capability matrix)

> **This is the canonical "what is done" doc.** It is updated in the same PR as any
> behavior change (Hermes/OpenClaw rule: docs change with behavior). When in doubt about
> whether something is built/wired, trust this file + `git ls-files`, not memory or an
> old plan. Source of truth for *how* it works: `docs/ARCHITECTURE.md` and
> `docs/reference/HIVEOS_COMPONENTS.md`.

Last reconciled against `main` after the **M1–M5** roadmap (resilience, self-improvement,
autonomy, surfaces, hardening). Test suite: **210 passed, 3 skipped** (3 skips are
opt-in live smokes; `HIVE_LIVE_TEST=1`).

## Legend
- **BUILT+WIRED** — code exists and is constructed/used by `HiveOS.build()` or the live call graph.
- **BUILT-NOT-WIRED** — code exists + tested, but nothing in the runtime uses it yet.
- **MISSING** — recommended by a reference report, not yet built.
- **DEFERRED/SKIP** — intentionally out of scope (SYNTHESIS Part D).

---

## Subsystems (all BUILT+WIRED)

| Subsystem | Modules | Status |
|---|---|---|
| core (leaf) | registry, events, types, config, doctor, credentials\*, soul+approval (bridges), self_mod, spec_search, budgeter, sandbox | BUILT+WIRED (\*credentials BUILT-NOT-WIRED) |
| llm | router, failover, credential_pool, model_catalog, pricing, rate_limit, sanitize, adapters/{base,minimax} | BUILT+WIRED |
| agents | base, orchestrator, loop_guard, delegate, planner, executor\* | BUILT+WIRED (\*executor BUILT-NOT-WIRED) |
| memory | provider, mnemosyne_provider, local, keeper, vault, curator, skill_usage | BUILT+WIRED |
| context | session_store, compaction, prompt_builder | BUILT+WIRED |
| tools | base, registry, executor, file_safety, discovery\*, builtins, mcp/{client,server}\* | BUILT+WIRED (\*discovery + mcp BUILT-NOT-WIRED) |
| gateway | app (FastAPI), protocol, auth, channels/{base,telegram} | BUILT+WIRED |
| autonomy | heartbeat, cron, tasks, commitments | BUILT+WIRED |
| surfaces | cli, voice | BUILT+WIRED (voice needs audio host) |
| observability | telemetry, traces, audit | BUILT+WIRED |
| runtime | runtime.py (`HiveOS` + `HiveOS.build`) | BUILT+WIRED |

## Capabilities delivered (M1–M5)
- **Resilience (M1):** failover taxonomy, multi-key credential pool w/ cooldowns,
  rate-limit-aware proactive cooldown, per-token cost budgeter, hardened Codex planner
  (stdin/timeout/fallback), opt-in live smokes.
- **Self-improvement (M2):** risk-tiered `spec_search` (AUTO/REVIEW/MANUAL, model can't
  self-escalate), Curator skill lifecycle (never-delete, pinned-exempt, backup), self-mod
  opens a real draft PR via GitHub REST; all wired into `HiveOS`.
- **Autonomy (M3):** durable SQLite TaskBoard (survives restart) + cron (croniter optional)
  + commitments; heartbeat drives the board.
- **Surfaces (M4):** SSE token streaming (`/chat/stream`, `ask_stream`); transport-only
  Telegram channel + webhook.
- **Hardening (M5):** delegate/mcp/vault tests, telemetry cost + trace export, self-mod
  Docker sandbox, fixed deploy units + `hive heartbeat`/`consolidate`.

---

## Open gaps (tracked; see master plan M6–M9)

### BUILT-NOT-WIRED (code exists; connect it — M6)
| Item | File | Gap |
|---|---|---|
| Discovery-first | `tools/discovery.py` | not a registered tool; nothing consults it (HARD SOUL rule) |
| MCP client/server | `tools/mcp/{client,server}.py` | runtime loads no MCP servers; tools not served |
| Mnemosyne host-LLM backend | `memory/mnemosyne_provider.py` | router not registered as Mnemosyne's LLM (dup cost) |
| Credentials vault | `core/credentials.py` | unused; key read from env, pool single-key |
| AgentExecutor | `agents/executor.py` | tick/retry lifecycle unused by runtime |
| `MNEMOSYNE_MCP_URL` | `core/config.py` | declared, not consumed |

### MISSING (recommended; build — M7/M8)
| Item | Source | Notes |
|---|---|---|
| `llm/adapters/{anthropic,codex}.py` | Hermes / SYNTHESIS B | Codex is subprocess-only; no provider-plugin contract |
| `redact.py` | Hermes #15 | audit logs raw tool args |
| `title_generator` | Hermes #27 | sessions have no auto-name |
| gateway protocol versioning | OpenClaw | `gateway/protocol.py` has no version field |
| tool availability signals | OpenClaw #8 | registry is a plain dict |
| terminal-environment abstraction | Hermes #11 | local shell only |

### Stub bodies (gated, safe) — wire to real services when defined
`tools/builtins`: `spend_money`, `deploy`, `external_message` return placeholder text.

### DEFERRED / SKIP (SYNTHESIS Part D — do not build without explicit ask)
recipes/TOML, workflow DAG, A2A, connectors, learning-loop + Pareto, trajectory_compressor,
Tauri desktop, Rust/PyO3, hardware auto-detect, ContextVar multi-profile, Kanban
multi-agent board, central command registry, AST tool auto-discovery, full tool-loop token
streaming, LLM diagnoser generating code edits in the heartbeat.

---

## Milestone ledger
| Milestone | PR | State |
|---|---|---|
| P0–P9 foundation | #2 | merged |
| M1 Resilience | #3 | merged |
| M2 Self-improvement | #9 | merged |
| M2 integration | #10 | merged |
| M3 Autonomy | #11 | merged |
| M4 Surfaces | #12 | merged |
| M5 Hardening | #13 | merged |
| Review fixes (doctor/docs) | #14 | open |
| M-DOCS (this) | — | in progress |
