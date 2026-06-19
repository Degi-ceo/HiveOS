# HiveOS — STATUS (living capability matrix)

> **This is the canonical "what is done" doc.** It is updated in the same PR as any
> behavior change (Hermes/OpenClaw rule: docs change with behavior). When in doubt about
> whether something is built/wired, trust this file + `git ls-files`, not memory or an
> old plan. Source of truth for *how* it works: `docs/ARCHITECTURE.md` and
> `docs/references/HIVEOS_COMPONENTS.md`.

Last reconciled after **PR #40** (system gaps completion — Sprint 1–4 + docs+tests audit, draft on branch
`claude/system-gaps-completion-6cr5rk`). Includes all M10 milestones, deploy phase 1 (PR #23), and
the full observability + diagnostics expansion below.
Test suite: **2961 passing** (4 skipped); optional-dependency skips vary by environment, and live smokes remain opt-in with `HIVE_LIVE_TEST=1`.
Sprint 5 features tracked in GitHub issues #41–#51 (Email/Slack/Discord, Skills UI, Stripe, Docker/SSH deploy, Voice hardening, Obsidian RAG, Dashboard WS, Mnemosyne doctor, CLI ops, GitHub tools).
New docs added: `CONFIGURATION.md`, `API.md`, `DEVELOPMENT.md`, `DEPLOYMENT.md`, `GLOSSARY.md`, `CHANGELOG.md`, `SECURITY.md`, `CONTRIBUTING.md`, `decisions/`.

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
- **Diagnostics API expansion (P25 — batches 24–30):** 100+ gateway endpoints covering
  every subsystem; rich introspection/management methods across 16 modules (see below).

### Diagnostics & introspection methods added (PR #25)

**Observability (`observability/audit.py`):**
`error_rate(window_hours)` — fraction of tool calls that errored in the given window.

**Commitments (`autonomy/commitments.py`):**
`next_due_at(commitment_id)` — UNIX timestamp when a commitment is next due.
`upcoming(limit, now)` — active commitments sorted by next-due time (soonest first).

**Spec search / self-improvement (`core/spec_search.py`):**
`tier_summary()` — pending review count and breakdown by op type.

**Session store (`context/session_store.py`):**
`total_message_count()` — total stored messages across all sessions.

**Tool executor (`tools/executor.py`):**
`dangerous_tools()` — list of tool names flagged dangerous in the registry.

**Self-modifier (`core/self_mod.py`):**
`success_rate()` — fraction of proposals that ended in a pushed branch.
`failed_proposals(limit)` — most recent failed proposals.
`proposals_by_stage()` — proposal counts bucketed by terminal stage.

**Budgeter (`core/budgeter.py`):**
`calls_per_hour()` — rolling hourly call rate.
`cost_per_call()` — average cost per LLM call today.
`warning_status()` — returns a warning dict when near cap/credit limit, `None` if healthy.

**Cron (`autonomy/cron.py`):**
`overdue_jobs(now)` — jobs that missed their last scheduled run.
`next_due_time(now)` — earliest next-run timestamp across all enabled jobs.
`job_health()` — health snapshot: total, enabled, overdue counts.

**TaskBoard (`autonomy/tasks.py`):**
`pending_by_kind()` — PENDING count grouped by task kind.
`average_age_pending(now)` — mean age (seconds) of all PENDING tasks.
`oldest_pending_age(now)` — age of the oldest PENDING task.
`total_count()` — total task count across all states.
`failure_rate_by_kind()` — fraction failed per kind (kinds with zero failures excluded).

**Local memory (`memory/local.py`):**
`most_important_facts(limit)` — top-N knowledge rows by importance score.
`memory_stats()` — knowledge/episodic counts, avg importance, timestamps, by-kind breakdown.

**Loop guard (`agents/loop_guard.py`):**
`top_repeated_tools(n)` — top-N tools by call count in the current guard window.
`call_count(tool)` — exact call count for a named tool.

**Telemetry (`observability/telemetry.py`):**
`selfmod_success_rate()` — fraction of self-mod attempts that succeeded.
`top_model()` — model with the most inference calls.
`total_tokens()` — combined input + output token count.

**Skill usage (`memory/skill_usage.py`):**
`unused_skills()` — active skills with `use_count == 0`.
`archived_count()` — number of archived skills.

**Config (`core/config.py`):**
`llm_summary()` — model configuration dict (no secrets).
`is_production()` — True when secret is non-default and host is not localhost.
`to_safe_dict()` — full config with all secrets replaced by `"***"`.

**Traces (`observability/traces.py`):**
`total_event_count()` — total events across all sessions.
`session_count()` — number of sessions with recorded events.
`event_type_counts(session)` — per-session event type histogram.

**Credential pool (`llm/credential_pool.py`):**
`labels()` — masked display labels for all credentials.
`failure_counts()` — per-label failure count dict.
`total_failures()` — sum of all credential failures (reset by `reset_cooldowns()`).

**Model catalog (`llm/model_catalog.py`):**
`list_models()` — all registered model IDs.
`unregister(model_id)` — remove a model from the catalog; returns False if not found.

**Budgeter forecast (`core/budgeter.py`):**
`forecast()` — calls today, daily cap, remaining calls, pct used, days remaining.

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

### DONE in PR #40 (Sprint 1 + Sprint 2 — system gaps audit completion) ✓

| Gap | File(s) | What changed |
|-----|---------|--------------|
| G-2 `system_prompt_block()` | `memory/local.py`, `memory/mnemosyne_provider.py` | Returns top-5 important facts (FTS5 rank) instead of static text / bare counters |
| G-3 auto-delegation | `tools/builtins/__init__.py` | `DelegateToSpecialist` builtin tool — model can call `delegate_named()` from the tool loop; local import preserves DAG |
| G-4 seed-on-deploy | `deploy/hiveos-gateway.service` | `ExecStartPre=` calls `scripts/seed_memories.py` on every gateway start (fail-open `|| true`) |
| G-5 OpenAI-compat endpoint | `gateway/app.py` | `POST /v1/chat/completions` + `GET /v1/models` — streaming (SSE) + non-streaming; response in OpenAI ChatCompletion format |
| G-6 migration versioning | `core/doctor.py` | `schema_migrations(id, version, applied_at)` table; each migration records its key after applying — safe upgrade path for future ALTER TABLE |
| G-7 security audit | `tools/discovery.py`, `tools/builtins/__init__.py` | `discover()` gains `security_delegate: Callable \| None`; `DiscoverTool(enable_security_audit=True)` injects the `security-reviewer` sub-agent via local import (DAG-safe); each candidate's audit stored in `security_note` |
| G-8 undocumented env vars | `.env.example`, `docs/CONFIGURATION.md` | `HIVE_MAX_ITERATIONS`, `HIVE_MAX_PER_TOOL`, `HIVE_SELFMOD_THRESHOLD`, `HIVE_TOOL_TIMEOUT` documented with defaults and explanations |
| G-9 hardcoded nginx IP | `deploy/nginx-hiveos.conf` | Replaced `46.224.161.38` with `YOUR_SERVER_IP` placeholder + instructional comments |
| G-10 voice setup | `pyproject.toml`, `docs/CONFIGURATION.md` | `[voice]` extra completed (`faster-whisper`, `piper-tts`, `sounddevice`); voice setup section in docs |
| G-11 curator LLM umbrellas | `memory/curator.py`, `runtime.py`, `autonomy/heartbeat.py` | `Curator.consolidate_umbrellas()` groups narrow active/agent-created skills into pinned umbrella skills via aux LLM; sources archived; wired into heartbeat after `curate()` (fail-open) |
| G-12 CI linting | `.github/workflows/ci.yml`, `pyproject.toml` | `ruff check src/ tests/` gate added to CI; ruff config (`line-length=120`, per-file test ignores) in `pyproject.toml` |

### DONE in Sprint 3 (second deep audit — N-1 to N-6) ✓

| Gap | File(s) | What changed |
|-----|---------|--------------|
| N-1 SSRF protection | `tools/builtins/__init__.py` | `_validate_url()` blocks RFC 1918, loopback, link-local, non-http(s) schemes, URL userinfo before any HTTP request in `WebGet` |
| N-2 DockerShellProvider | `tools/shell_provider.py`, `core/config.py`, `runtime.py` | `DockerShellProvider(image, network)` runs commands in disposable containers; wired via `HIVE_SHELL_PROVIDER=docker` + `HIVE_SHELL_DOCKER_IMAGE` |
| N-3 Terminal-outcome enum | `agents/base.py`, `agents/orchestrator.py` | `TerminalOutcome` enum (COMPLETED / MAX_TURNS / LOOP_GUARD / TOOL_ERROR) on `AgentResult.outcome`; set at every exit path |
| N-4 Channel hint | `context/prompt_builder.py`, `agents/orchestrator.py`, `runtime.py`, `gateway/app.py` | `system_prompt(channel_hint=)` inserts `[Active surface: X]` between SOUL and memory block; hint flows from gateway → runtime → orchestrator; NOT persisted (stable cache prefix intact) |
| N-5 One-command installer | `install.sh` (new), `surfaces/cli.py`, `README.md` | `curl …/install.sh | bash` clones repo, creates venv, installs `.[memory]`, runs `doctor --fix`; `hive init` wizard sets API keys + HIVE_SECRET + Mnemosyne path + seeds memories |
| N-6 Professional REPL | `surfaces/cli.py` | ASCII banner (ANSI, degrades with `NO_COLOR`), first-run guard → `hive init`, slash commands (`/help /status /clear /quit`), `thinking...` indicator, color-coded prompts |

### DEFERRED / SKIP (SYNTHESIS Part D — do not build without explicit ask)
recipes/TOML, workflow DAG, A2A, connectors, learning-loop + Pareto, trajectory_compressor,
Tauri desktop, Rust/PyO3, hardware auto-detect, ContextVar multi-profile, Kanban
multi-agent board, central command registry, AST tool auto-discovery, full tool-loop token
streaming.

**Second-audit deferrals (D-23 to D-28):**

| # | Feature | Source | Why deferred |
|---|---------|--------|--------------|
| D-23 | ACP protocol (IDE integration over stdio) | OpenClaw §8 | MCP already covers Claude Code integration; ACP is TypeScript-ecosystem-first |
| D-24 | Three-contract plugin system (general/memory/model plugins with lifecycle hooks) | Hermes §9 | Too architectural for single-user; `llm/adapters/__init__.py` registry already covers the model-provider slot |
| D-25 | Agent loop hook points (`beforeToolCall`/`afterToolCall`) | OpenClaw #2 | High-effort architectural change; EventBus covers the observability use-case already |
| D-26 | Security audit engine with plugin-registered collectors | OpenClaw §10 | `observability/audit.py` + `core/redact.py` + approval gate cover single-user needs |
| D-27 | Bench stats utils (`bench/_stats.py` percentile/p50/p95) | OpenJarvis §9 #28 | No benchmarking use-case yet |
| D-28 | Mnemosyne memory importers CLI exposure | Mnemosyne §3 | Available via `mnemosyne import` CLI if package installed; not an HiveOS-owned gap |

> Note: "LLM diagnoser generating code edits in the heartbeat" has been partially shipped
> (M10-c + P25): the symptom-based diagnoser runs on demand via `POST /self-improve/symptom`
> and is triggered by heartbeat on ≥3 failures. Heartbeat-auto-trigger remains the only
> "fully autonomous" part and is already wired.

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
| Diagnostics API expansion (P25): 100+ endpoints, 16-module introspection methods | #25 | draft |
| System gaps completion (G-2–G-12): memory facts, delegation, OpenAI endpoint, migration versioning, security audit, curator LLM umbrellas, CI ruff, docs | #40 | draft |
| Docs+tests audit (A1-A5 docs, B1-B5 tests, 808-test suite): DEPLOYMENT/DEVELOPMENT/README/GLOSSARY/SECURITY, +17 new tests covering v1 endpoints, curator umbrellas, DelegateToSpecialist, security delegate | #40 | draft |
| Sprint 3 second-audit gaps (N-1 SSRF, N-2 Docker shell, N-3 terminal outcomes, N-4 channel hint, N-5 installer, N-6 professional REPL — 1165 tests) | #40 | draft |
| Sprint 3 post-sprint hardening (SSRF redirect bypass, HiveConfig validation + doctor M4, gateway channel_hint tests, /status CLI test, SECURITY.md SSRF+shell sections — 1165 tests) | #40 | draft |
| Sprint 4 — 30-task expansion (gateway hardening, CLI commands, 162 new tests: doctor/credentials/rate-limit/agent-base/CredentialPool/CommitmentBook/CronScheduler/TaskBoard/memory/LLM-router/compaction/observability/budgeter/EventBus/SelfImprovement/LoopGuard/WebSocket/telemetry/self-diagnose; ADR 006; CORS+input-validation+WS security — 1165 tests) | #40 | draft |
| Wave 3 — LLM adapter tests, tools/core edge cases, agents/planner/orchestrator, runtime methods (MiniMaxAdapter caching/aclose, AnthropicAdapter, BaseTool.to_openai_function, ToolRegistry.get KeyError, file_safety/redact depth, AgentExecutor cancel, Planner TaskKind, _safe_args, MemoryKeeper per-item, HiveOS.health/consolidate/curate_umbrellas/aclose — 1165 tests, +57 new) | #40 | draft |
| Waves 3U–4R (test coverage expansion) — parallel 8-test-per-file waves across all 35+ test files; every file now 70–80+ tests; total 2961 passing | #40 | draft |

### Sprint 5 — planned (issues #41–#51, next session after PR #40 merges)

| Issue | Feature | Priority |
|-------|---------|----------|
| [#41](https://github.com/hiveOSagent/HiveOS/issues/41) | Sprint 5 session briefing (master tracker) | — |
| [#42](https://github.com/hiveOSagent/HiveOS/issues/42) | Multi-channel messaging: Email (SMTP) + Slack + Discord webhooks | HIGH |
| [#43](https://github.com/hiveOSagent/HiveOS/issues/43) | Dashboard Skills Panel: view/pin/archive skills in MissionControl UI | MEDIUM |
| [#44](https://github.com/hiveOSagent/HiveOS/issues/44) | Payment backend: Stripe adapter for spend_money tool | MEDIUM |
| [#45](https://github.com/hiveOSagent/HiveOS/issues/45) | Deploy tool: Docker and SSH deployment targets | MEDIUM |
| [#46](https://github.com/hiveOSagent/HiveOS/issues/46) | Voice surface hardening: audio device auto-detection + wake-word engine | LOW |
| [#47](https://github.com/hiveOSagent/HiveOS/issues/47) | Obsidian vault: bidirectional read/write + RAG FTS5 search tools | HIGH |
| [#48](https://github.com/hiveOSagent/HiveOS/issues/48) | Dashboard: enriched approval queue + real-time WebSocket updates | MEDIUM |
| [#49](https://github.com/hiveOSagent/HiveOS/issues/49) | Mnemosyne: VPS install + hive doctor M4 check + runtime degraded warning | HIGH |
| [#50](https://github.com/hiveOSagent/HiveOS/issues/50) | CLI ops commands: hive logs/status/budget/approvals | MEDIUM |
| [#51](https://github.com/hiveOSagent/HiveOS/issues/51) | GitHub integration tools: list PRs, create issues, PR CI status | MEDIUM |
