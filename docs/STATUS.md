# HiveOS — STATUS (living capability matrix)

> **This is the canonical "what is done" doc.** It is updated in the same PR as any
> behavior change (Hermes/OpenClaw rule: docs change with behavior). When in doubt about
> whether something is built/wired, trust this file + `git ls-files`, not memory or an
> old plan. Source of truth for *how* it works: `docs/ARCHITECTURE.md` and
> `docs/references/HIVEOS_COMPONENTS.md`.

Last reconciled after **PR #23** (deploy/system-setup-phase1, squash-merged to main `487b729`).
Includes A3 host-LLM bridge (PR #21), M9-transport (PR #22), all M9/M10 milestones, and the
full production deploy + hardening pass below.
Test suite: **367 passed, 4 skipped** (4 skips are opt-in live smokes; `HIVE_LIVE_TEST=1`).
New docs added: `CONFIGURATION.md`, `API.md`, `DEVELOPMENT.md`, `DEPLOYMENT.md`.

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
| llm | router, failover, credential_pool, model_catalog, pricing, rate_limit, sanitize, adapters/{base,minimax,anthropic,codex} | BUILT+WIRED |
| agents | base, orchestrator, loop_guard, delegate (+ named registry), planner, executor | BUILT+WIRED |
| memory | provider, mnemosyne_provider, local, keeper, vault, curator, skill_usage | BUILT+WIRED (host-LLM bridge wired M9-b) |
| context | session_store, compaction, prompt_builder | BUILT+WIRED |
| tools | base, registry, executor, file_safety, discovery, builtins, mcp/client (stdio+SSE), mcp/server (serve-side) | BUILT+WIRED |
| gateway | app (FastAPI), protocol, auth, channels/{base,telegram} | BUILT+WIRED |
| autonomy | heartbeat, cron, tasks, commitments | BUILT+WIRED |
| surfaces | cli, voice | BUILT+WIRED (voice needs audio host) |
| observability | telemetry, traces, audit | BUILT+WIRED |
| runtime | runtime.py (`HiveOS` + `HiveOS.build`) | BUILT+WIRED |

## Capabilities delivered (M1–M10-d)
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
- **Wiring (M6):** discovery-first tool registered as `discover` builtin (memory-cached);
  MCP client loads external servers from `HIVE_MCP_SERVERS` at gateway startup; credentials
  vault injected at build; AgentExecutor wired into delegate subagents.
- **Hardening2 (M7):** secret redaction in audit log; `PROTOCOL_VERSION` on every gateway
  response; `BaseTool.available()` signals hide/refuse unavailable tools; session
  auto-titling via out-of-band aux-model call.
- **Providers (M8):** Anthropic + Codex adapters behind `LLMAdapter`; `make_adapter(provider)`
  registry; executor switchable via `HIVE_EXEC_PROVIDER` (minimax|anthropic).
- **Mission Control visibility (M10-a):** Four authenticated gateway endpoints expose runtime
  state: `GET /telemetry` (model/token/cost counters), `GET /traces/{session_id}` (per-session
  event trace), `GET /audit?limit=N` (recent tool-call audit from SQLite), `GET /tasks`
  (task board: pending count + last 20 tasks). Dashboard adds MODEL USAGE (polls /telemetry
  every 10 s), RECENT EXECUTIONS (polls /audit every 6 s), TASK QUEUE (polls /tasks every 5 s).
- **Action tools wired (M10-b):** `external_message` sends real Telegram messages via
  `TelegramChannel` (token from `TELEGRAM_BOT_TOKEN`); `deploy` calls `systemctl restart
  hiveos-{gateway,orchestrator,keeper}.service` with safe-target guard; `spend_money`
  returns an honest capability-absent message. All still gated (approval required).
- **Self-improvement depth (M10-c):** `TaskBoard.recent_failures(limit)` queries failed
  tasks newest-first. `HiveOS.self_improve_from_symptom(symptom)` runs the full
  `diagnose_and_run` loop and enqueues REVIEW/MANUAL outcomes as `self_improve` tasks
  visible in `/tasks`. Heartbeat `tick()` fires this loop when ≥3 recent failures are
  detected (wrapped in `try/except` so a self-improve failure never aborts the tick);
  returns new `self_improved` count in its result dict. **Post-audit fix:** `_diagnoser()`
  now parses model JSON into `Edit` objects (was discarding them). `HiveOS.edit_pending`
  stores REVIEW-tier edits so `/approvals/decide` can apply them after human approval
  (was routing to tool executor which returned "unknown tool" for `self_mod:*` names).
  **Post-audit fix 2:** `str(RiskTier.REVIEW).upper()` comparison was wrong (`"RISKTIER.REVIEW"`
  ≠ `"REVIEW"`); replaced with direct enum membership check so REVIEW/MANUAL outcomes are
  now correctly enqueued as `self_improve` tasks. `ask_stream()` was passing `[]` history;
  now loads last 40 messages from `session_store`. Global approval-gate singleton now reset
  between tests via `conftest.py` autouse fixture to prevent state leakage.
- **Specialist sub-agents (M10-d):** `.claude/agents/` contains five agent definition
  files (researcher, coder, reviewer, memory-keeper, security-reviewer) each with YAML
  frontmatter + system prompt. `agents/delegate.py` gains a named-factory registry
  (`register_agent`, `get_agent_factory`, `delegate_named`). `HiveOS.agents_registry`
  dict maps all five names to `ConversationOrchestrator` factories, registered at build
  time via `register_agent`.

---

## Open gaps (tracked; see master plan M6–M9)

### WIRED in M6 (was BUILT-NOT-WIRED) ✓
| Item | File | How it's wired now |
|---|---|---|
| Discovery-first | `tools/discovery.py` | registered as the `discover` builtin (memory-cached) + `HiveOS.discover()` |
| MCP client load | `tools/mcp/client.py` | `HiveOS.load_mcp_servers()` from `HIVE_MCP_SERVERS`, called at gateway startup; `ToolExecutor.add_tool` |
| Credentials vault | `core/credentials.py` | `credentials.inject()` at build; pool seeded from vault/env, comma-split multi-key |
| AgentExecutor | `agents/executor.py` | per-subagent retry + terminal outcome in `agents/delegate.py` |

### DONE in A3 ✓
| Item | File | How |
|---|---|---|
| Mnemosyne host-LLM backend | `llm/host_bridge.py` | `HostLLMBridge` runs on its OWN dedicated event loop + own adapter/httpx client (daemon thread); Mnemosyne's sync `.complete` (called from its consolidation thread) is serviced via `run_coroutine_threadsafe` — no cross-loop client reuse. Registered by `build_mnemosyne_provider(host_llm=)`. |

### DONE in M9-transport ✓
| Item | File | How |
|---|---|---|
| MCP serve-side | `tools/mcp/server.py` | `HiveOS.serve_mcp()` + `hive mcp-serve` expose Hive's tools to other agents over MCP stdio |
| SSE MCP client + `MNEMOSYNE_MCP_URL` | `tools/mcp/client.py` | `MCPClient(url=)` SSE transport; `load_mcp_servers` routes `http(s)://` specs to SSE and loads `MNEMOSYNE_MCP_URL` as a remote MCP server |

**BUILT-NOT-WIRED: none.** Every reference-cross-reference item is now built+wired or
explicitly deferred below.

### DONE in M7 ✓
| Item | File | How |
|---|---|---|
| Secret redaction | `core/redact.py` | masks env/auth/JWT/private-key/vendor-prefix; applied in `observability/audit.py` |
| Gateway protocol versioning | `gateway/protocol.py` | `PROTOCOL_VERSION` on every `ChatResponse` + `/health` |
| Tool availability signals | `tools/base.py` | `BaseTool.available()`; orchestrator hides + executor refuses unavailable tools |
| Session titles | `context/title.py` | `HiveOS.title_session()` (out-of-band aux-model title, idempotent) |

### DONE in M8 ✓
| Item | File | How |
|---|---|---|
| Anthropic + Codex adapters | `llm/adapters/{anthropic,codex}.py` | both behind `LLMAdapter`; Codex normalized (shared `run_codex`), `make_codex_planner` delegates to it |
| Provider-plugin contract | `llm/adapters/__init__.py` | `make_adapter(provider)` registry; runtime selects executor via `HIVE_EXEC_PROVIDER` (minimax\|anthropic) |

### DONE in M9 ✓
| Item | File | How |
|---|---|---|
| `hive mcp-serve` CLI command | `tools/mcp/server.py`, `surfaces/cli.py`, `runtime.py` | `HiveOS.mcp_server()` accessor + `hive mcp-serve` dispatches via `MCPServer.serve_stdio()` |
| Mnemosyne host-LLM async bridge | `memory/mnemosyne_provider.py`, `runtime.py` | `set_host_llm_backend()` spins a private daemon loop; `run_coroutine_threadsafe` bridges sync consolidation thread to async adapter |
| Terminal-environment abstraction | `tools/shell_provider.py`, `tools/builtins/__init__.py` | `ShellProvider` ABC + `LocalShellProvider`; `Shell` tool accepts injected provider (container/SSH providers slot in here) |

### ~~Stub bodies~~ — wired in M10-b
`spend_money`: returns honest "no payment backend" message (Stripe/Revolut adapter slot).
`deploy`: rejects unknown targets; calls `systemctl restart hiveos-<target>.service` for
  `gateway`, `orchestrator`, `keeper` (already behind approval gate).
`external_message`: sends real Telegram message via `TelegramChannel` when
  `TELEGRAM_BOT_TOKEN` is set; graceful capability-absent message when unset.

### DONE in PR #23 (deploy phase 1) ✓
| Item | File | How |
|---|---|---|
| Production systemd units | `deploy/hiveos-{gateway,orchestrator,keeper}.service` + `.timer` | Three `--user` services: gateway (FastAPI), orchestrator (heartbeat), keeper (nightly consolidation at 03:00). `ProtectHome=read-only` + explicit `ReadWritePaths` incl. `.git` for self-mod. |
| nginx config | `deploy/nginx-hiveos.conf` | Reverse-proxy port 80 + SSL on 8443 (Telegram webhook); WebSocket `proxy_http_version 1.1`. |
| Configurable loop limits | `core/config.py`, `agents/orchestrator.py`, `runtime.py` | `HIVE_MAX_ITERATIONS` (default 30) and `HIVE_MAX_PER_TOOL` (default 50) via env → `HiveConfig` → `ConversationOrchestrator` → `LoopGuard`. |
| Silent failure hardening (20+9 fixes) | multiple | Gateway `/chat`/`/ws`/SSE/Telegram error leakage closed; executor path-safety recheck after approval; `self_mod` finally-block cleanup logged; heartbeat `consolidate`/`_refresh_budget` guarded independently; Mnemosyne `_sync_complete` catches all exceptions; `system_prompt_block`/`prefetch` escalated to `log.warning`. |
| `.gitignore` key/cert guard | `.gitignore` | `*.key` and `*.pem` ignored so `self_mod`'s `git add -A` can never commit secrets. |
| Memory seed script | `scripts/seed_memories.py` | Seeds Hive identity, active system facts, and milestone history into Mnemosyne at deploy time. |
| Security regression tests | `tests/test_gateway.py`, `tests/test_tools.py` | `test_chat_hides_exception_detail`, `test_ws_error_sends_generic_message`, `test_execute_approved_rejects_traversal_path` — guard against future regressions. |

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
| M6 Wiring (discovery/MCP/credentials/executor) | #16 | merged |
| M7 Hardening2 (redact/protocol-version/tool-availability/titles) | #17 | merged |
| M8 Providers (anthropic/codex adapters + registry) | #18 | merged |
| A3 Mnemosyne host-LLM bridge | #21 | merged |
| M9-transport (MCP serve-side + SSE client) | #22 | merged |
| M9 (mcp-serve + Mnemosyne bridge + shell abstraction + dashboard SSE) | #20 | merged |
| M10-a Mission Control visibility (telemetry/traces/audit/tasks endpoints + dashboard panels) | #20 | merged |
| M10-b Action tools wired (external_message→Telegram, deploy→systemctl, spend_money honest) | #20 | merged |
| M10-c Self-improvement depth (recent_failures, self_improve_from_symptom, heartbeat trigger) | #20 | merged |
| M10-d Specialist sub-agents (.claude/agents/, named registry, delegate_named) | #20 | merged |
| Pre-merge review + conflict resolution (M9-transport + A3 merge, 2 test fixes) | #20 | merged |
| Deploy phase 1: systemd units, nginx, Mnemosyne adapter, configurable loop limits, 9 hardening fixes | #23 | merged |
