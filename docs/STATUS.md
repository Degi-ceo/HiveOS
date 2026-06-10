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
| core (leaf) | registry, events, types, config, doctor, credentials, soul+approval (bridges), self_mod, spec_search, budgeter, sandbox | BUILT+WIRED |
| llm | router, failover, credential_pool, model_catalog, pricing, rate_limit, sanitize, adapters/{base,minimax} | BUILT+WIRED |
| agents | base, orchestrator, loop_guard, delegate, planner, executor | BUILT+WIRED |
| memory | provider, mnemosyne_provider\*, local, keeper, vault, curator, skill_usage | BUILT+WIRED (\*host-LLM backend deferred) |
| context | session_store, compaction, prompt_builder | BUILT+WIRED |
| tools | base, registry, executor, file_safety, discovery, builtins, mcp/client, mcp/server\* | BUILT+WIRED (\*mcp/server serve-side not wired) |
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

### WIRED in M6 (was BUILT-NOT-WIRED) ✓
| Item | File | How it's wired now |
|---|---|---|
| Discovery-first | `tools/discovery.py` | registered as the `discover` builtin (memory-cached) + `HiveOS.discover()` |
| MCP client load | `tools/mcp/client.py` | `HiveOS.load_mcp_servers()` from `HIVE_MCP_SERVERS`, called at gateway startup; `ToolExecutor.add_tool` |
| Credentials vault | `core/credentials.py` | `credentials.inject()` at build; pool seeded from vault/env, comma-split multi-key |
| AgentExecutor | `agents/executor.py` | per-subagent retry + terminal outcome in `agents/delegate.py` |

### BUILT-NOT-WIRED (still deferred — concurrency/transport design needed)
| Item | File | Gap & why deferred |
|---|---|---|
| Mnemosyne host-LLM backend | `memory/mnemosyne_provider.py` | `set_host_llm_backend` exists, but bridging the async router to Mnemosyne's **sync `.complete` called from its consolidation thread** risks cross-event-loop reuse of the shared httpx client. Needs a dedicated-loop/own-client design — do it right, not fast. |
| MCP server (serve Hive's tools) | `tools/mcp/server.py` | client load done; serving Hive's own tools over MCP (`hive mcp-serve`) not wired |
| `MNEMOSYNE_MCP_URL` | `core/config.py` | needs an **SSE** MCP client (current client is stdio); in-process provider is the path today |

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
| Review fixes (doctor/docs) | #14 | merged |
| M-DOCS | #15 | merged |
| M6 Wiring (discovery/MCP/credentials/executor) | — | in progress |
