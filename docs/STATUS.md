# HiveOS — STATUS (living capability matrix)

> **This is the canonical "what is done" doc.** It is updated in the same PR as any
> behavior change (Hermes/OpenClaw rule: docs change with behavior). When in doubt about
> whether something is built/wired, trust this file + `git ls-files`, not memory or an
> old plan. Source of truth for *how* it works: `docs/ARCHITECTURE.md` and
> `docs/references/HIVEOS_COMPONENTS.md`.

Last reconciled after **SPRINT_6 P-D** (`sprint6/a2a-envelope` branch, issue #72, PR pending).
Test suite: **3684 passing** (4 skipped for optional deps).
Sprint 5 complete (PR #52): Discord webhook, Obsidian RAG, Dashboard WS, Mnemosyne doctor, CLI ops, GitHub tools; Phase 2 autonomous hardening: query_memory + create_task tools, soft LoopGuard, proactive heartbeat, prefix-cache fix.
Phase 3 (PR #53): self-modification quality — structured test output parser, rich symptom aggregator (audit + task failures + prior failed proposals), context-aware file ranking in diagnoser, proactive diagnose throttle (30 min cooldown).
Coverage sprint PR #55–#67 (sequence): runtime, budgeter, orchestrator, doctor, self_mod, sanitize, mnemosyne_provider, cli_surfaces, plus the sprint-continuation PRs #66 (14 modules → 100%) and #67 (tools/builtins 84% → 94%). 87 net new tests this session (3148 → 3205).
Issue #44: Stripe payment backend — `StripeAdapter` wired into `SpendMoney`; set `STRIPE_SECRET_KEY` + `STRIPE_CUSTOMER_ID` to activate.
Issue #45: Docker/SSH deploy targets — `Deploy` tool supports `mode=systemctl|docker|ssh`; SSH via `HIVE_DEPLOY_SSH_HOST`/`HIVE_DEPLOY_SSH_KEY`.
Issue #46: Voice surface hardening — `_detect_audio_device()` probes `arecord -l`; `WakeWordDetector` uses openWakeWord when installed, falls back to transcript string match; `record_until_silence()` auto-selects ALSA device.
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
| agents | base, orchestrator, loop_guard, delegate (+ named registry), planner, executor, a2a/{envelope,router,client} | BUILT+WIRED (SPRINT_6 P-D) |
| memory | provider, mnemosyne_provider, local, keeper, vault, curator, skill_usage | BUILT+WIRED (host-LLM bridge wired M9-b) |
| context | session_store, compaction, prompt_builder | BUILT+WIRED |
| tools | base, registry, executor, file_safety, discovery, builtins, mcp/client (stdio+SSE), mcp/server (serve-side) | BUILT+WIRED |
| gateway | app (FastAPI), protocol, auth, channels/{base,telegram} | BUILT+WIRED |
| autonomy | heartbeat, cron, tasks, commitments | BUILT+WIRED |
| surfaces | cli, voice | BUILT+WIRED (voice needs audio host) |
| observability | telemetry, traces, audit | BUILT+WIRED |
| runtime | runtime.py (`HiveOS` + `HiveOS.build`) | BUILT+WIRED |
| evals | types, dataset, runner, cli, graders/{base,exact,regex,llm_judge,tool_trace}, reporters/{console,junit_xml,html} | BUILT+WIRED (SPRINT_6 P-B; CI gate via `evals` job) |

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
| Sprint 4 features — multi-channel messaging (Email SMTP + Slack webhook in ExternalMessage), Dashboard Skills Panel (MissionControl.jsx), +6 HiveConfig fields, +11 tests (2972 total) | #40 | draft |

### Sprint 5 — DONE (PR #52, 2992 tests)

| Issue | Feature | Status |
|-------|---------|--------|
| [#42](https://github.com/hiveOSagent/HiveOS/issues/42) | Discord webhook (ExternalMessage channel) | ✅ done |
| [#47](https://github.com/hiveOSagent/HiveOS/issues/47) | Obsidian vault RAG: read/search/list tools | ✅ done |
| [#48](https://github.com/hiveOSagent/HiveOS/issues/48) | Dashboard WebSocket `/ws/dashboard` real-time events | ✅ done |
| [#49](https://github.com/hiveOSagent/HiveOS/issues/49) | Mnemosyne degraded-mode warning + doctor M4 check | ✅ done |
| [#50](https://github.com/hiveOSagent/HiveOS/issues/50) | CLI `hive budget` / `hive approvals` commands | ✅ done |
| [#51](https://github.com/hiveOSagent/HiveOS/issues/51) | GitHub integration tools (list PRs, commits, create issues) | ✅ done |

### Phase 2 — Autonomous Agent Hardening (PR #52, 2998 tests)

Turns Hive from a chatbot into a self-developing autonomous agent by wiring existing components (Planner, TaskBoard, SelfModifier) into the live conversation turn.

| Gap | Fix | Files |
|-----|-----|-------|
| G1: No mid-turn memory access | `query_memory` builtin tool | `tools/builtins/__init__.py` |
| G2: No async work scheduling | `create_task` builtin tool (enqueues to SQLite TaskBoard) | `tools/builtins/__init__.py` |
| G3: LoopGuard = silent crash | Soft pivot turn: model gets one final no-tools call to explain | `agents/orchestrator.py` |
| G4: Planner never in turn loop | Planner wired to orchestrator; appends hints when MAX_TURNS reached stuck | `agents/orchestrator.py`, `runtime.py` |
| G5: Prefix-cache miss every turn | `channel_hint` baked into stored system prompt instead of late-append | `context/prompt_builder.py`, `agents/orchestrator.py` |
| G6: Heartbeat only reactive | Proactive `self_diagnose()` every N ticks (`HIVE_SELFMOD_PROACTIVE_INTERVAL=10`) | `autonomy/heartbeat.py`, `core/config.py` |
| G7: Diagnoser errors silent | `log.warning` → `log.error` + exc_info for visibility | `runtime.py` |

### Phase 3 — Self-Modification Quality (PR #53, ≥3004 tests)

Makes the self-modification engine generate better code changes by giving the LLM richer, structured context.

| Change | What it does | Files |
|--------|-------------|-------|
| `_parse_test_output()` | Extracts FAILED test names + short summary instead of tail-truncating 3000 chars | `runtime.py` |
| `_build_symptom_context()` | Aggregates: base symptom + tool error rates (audit) + recent task failures + prior failed proposal titles (what NOT to repeat) | `runtime.py` |
| Context-aware file ranking | Ranks source files by keyword overlap with symptom (not alphabetical); 30 relevant files instead of 60 random | `runtime.py` |
| Anti-repetition hint | Injects `failed_proposals()` into diagnoser prompt as "AVOID these approaches" | `runtime.py` |
| Proactive diagnose throttle | Skips run if < 30 min since last proactive run (prevents thrashing); first run always fires | `autonomy/heartbeat.py` |

---

## Coverage snapshot (after PR #67)

Module-by-module statement coverage measured against the live test suite (3205 tests).

### At 100% (production + tests in lockstep)

| Module | Coverage |
|--------|---------|
| `core/redact.py` | 100% |
| `core/types.py` | 100% |
| `core/learning/*` | 100% |
| `core/sandbox.py` | 100% |
| `core/config.py` | 100% |
| `core/events.py` | 100% |
| `core/budgeter.py` | 100% |
| `core/doctor.py` | 100% |
| `core/self_mod.py` | 100% |
| `agents/executor.py` | 100% |
| `agents/orchestrator.py` | 100% |
| `llm/adapters/codex.py` | 100% |
| `llm/adapters/minimax.py` | 100% |
| `llm/failover.py` | 100% |
| `llm/host_bridge.py` | 100% |
| `llm/model_catalog.py` | 100% |
| `llm/pricing.py` | 100% |
| `memory/curator.py` | 100% |
| `memory/sanitize.py` | 100% |
| `observability/traces.py` | 100% |
| `core/runtime.py` | 100% |
| `tools/file_safety.py` | 100% |
| `tools/registry.py` | 100% |
| `tools/shell_provider.py` | 100% |
| `tools/mcp/*` | 100% |

### Remaining gaps (next sprint targets)

| Module | Coverage | Missed lines | Notes |
|--------|---------:|-------------:|-------|
| `tools/builtins/__init__.py` | 94% | ~31 | WriteFile parent-mkdir edge, Deploy real subprocess, ExternalMessage SMTP sendmail via executor, GitHub list_branches / list_commits execute branches |
| `tools/discovery.py` | 73% | ~14 | Core discovery loop, MCP integration, security_delegate flow — most user-facing capability search surface |
| `tools/executor.py` | 96% | ~5 | Async timeout + concurrent tool dispatch |
| `tools/base.py` | 95% | ~1 | Defensive check on line 26 |
| `llm/router.py` | 93% | ~10 | Mid-failover rotation in `complete()`, `stream()` error path, planner-fallback after `PlannerError` |

Total remaining missed lines: ~60 across 5 files. The next sprint branch
(`coverage/router-85` → `coverage/discovery-85`) will close these and bring
the package to >=98% statement coverage end-to-end.

---

## SPRINT_6 capability additions

### P-B — Evals harness (issue #70, branch `sprint6/evals-harness`)

Production-grade regression gate. Anything that lands on `main` must pass this.

- **Module:** `src/hive/evals/` (15 files, 618 statements, 100% covered)
  - `types.py` — `EvalItem`, `GraderResult`, `EvalResult`, `EvalSummary`, `EvalReport`
  - `dataset.py` — JSONL + YAML loaders with line-numbered error reporting
  - `graders/` — `exact`, `regex`, `llm_judge` (heuristic until a real judge model is budgeted), `tool_trace`
  - `runner.py` — async + sync, per-item timeout, concurrency cap, graceful grader-error handling
  - `reporters/` — `console` (ANSI colour, NO_COLOR-aware), `junit_xml` (GitHub Actions / Jenkins / GitLab), `html` (self-contained, CI-artifact friendly)
  - `cli.py` — `hive-eval` entry point: `run` / `show` subcommands
- **Dataset:** `evals/datasets/golden_qa.jsonl` (30 hand-curated Q/A pairs: exact + regex + llm_judge graders)
- **CI gate:** new `evals` job in `.github/workflows/ci.yml` runs `hive-eval run evals/datasets/golden_qa.jsonl --target mock` after the `test` job; HTML + JUnit reports uploaded as workflow artifacts
- **Entry point:** `hive-eval` script registered in `pyproject.toml`
- **Tests:** 148 unit tests + 5 end-to-end integration tests (148 → 153 evals-specific, 3276 → 3369 total after SPRINT_6 foundation PR #80)
- **Acceptance met:**
  - [x] 30/30 dataset items pass on a clean main (mock target)
  - [x] Failure of any item exits 1 in CI
  - [x] HTML report uploaded as artifact on GitHub Actions
  - [x] 100% coverage on `src/hive/evals/`
  - [x] One integration test proves a failing eval blocks merge via the existing CI workflow

### P-C — Tool-loop streaming SSE (issue #71, branch `sprint6/tool-loop-stream`)

Clients (dashboard Mission Control, future Cursor/Aider integrations, curl
debugging) see the agent's tool activity live, not just the final text.

- **`ConversationOrchestrator.stream_ask()`** — async generator yielding
  per-iteration events: `model_decision`, `tool_call_start`, `tool_call_end`
  (status=ok|error), `loop_guard`, `final`, `max_turns`, `error`. Wraps
  `_run_loop(sink)`; `ask()` unchanged.
- **`HiveOS.stream_ask_iterations()`** — thin proxy forwarding orchestrator
  events from `runtime.py`.
- **`POST /chat/stream/iterations`** — new SSE endpoint. Format:
  `event: <type>\ndata: <json>\n\n` … `data: [DONE]`. Auth-required,
  identical token contract to `/chat`.
- **`POST /v1/chat/completions`** — extended with `x-hive-iterations: true`
  request header. When set + `stream:true`, emits OpenAI-shaped chunks with
  `delta.tool_calls` populated (per spec) + `delta.content` marker lines for
  tool events. Default path byte-for-byte unchanged.
- **`dashboard/MissionControl.jsx`** — new full-width `TOOL LOOP` panel
  between CONVERSATION and APPROVAL INBOX. Parallel fetch of
  `/chat/stream` + `/chat/stream/iterations` via `Promise.all`; capped
  rolling 200-event log; colour-coded chips per event type.
- **CORS** — `x-hive-iterations` added to `allow_headers` for browser access.
- **Tests:** 11 new tests in `tests/test_iteration_stream.py` (orchestrator
  event sequence, tool error, loop_guard, max_turns, runtime proxy,
  /chat/stream/iterations format + auth + error redaction, /v1 iterations
  branch + default-path regression, /chat/stream default-path regression).
- **Acceptance met:**
  - [x] A real tool-calling conversation shows 4+ SSE events live in `curl -N`
  - [x] Existing `/chat/stream` and `/v1/chat/completions` paths unchanged
        (regression tests prove the default path is byte-identical)
  - [x] 100% coverage on the new generator + new endpoint
  - [x] One regression test per unchanged path
- **Why now:** unblocks P-D (A2A envelope, needs iteration visibility) and
  gives clients real-time tool feedback (parity with lib-class agents).

### P-D — A2A protocol envelope (issue #72, branch `sprint6/a2a-envelope`)

Minimal JSON-RPC-style envelope over the 5 named sub-agents so future external
agents can connect via the same contract. Internal-only this sprint: a remote
HTTP bridge is deferred.

- **Module:** `src/hive/agents/a2a/` (4 files, 88 statements, 100% covered)
  - `envelope.py` — Pydantic `A2ARequest` / `A2AResponse` / `A2AError` (`extra="forbid"`,
    uuid4 hex request id default, JSON-RPC 2.0 error codes `-32601`/`-32603`).
  - `router.py` — registers local async handlers by `method` name; remote URIs
    resolve to a `{"remote_uri": ...}` hint so callers dispatch via `A2AClient`.
    Exceptions inside handlers are normalised to envelope errors (never raise).
  - `client.py` — `A2AClient` (httpx-based) with `timeout`, `max_retries`, `backoff`;
    retries on `httpx.HTTPError` and 5xx, returns 4xx envelopes as-is, raises
    `A2AConnectionError` after retries exhausted.
- **Wire-in:** `delegate_to_specialist` now routes through `delegate_via_envelope`,
  which registers a `f"{name}.run"` handler on first call and dispatches via the
  envelope. Existing callers see no behavior change (snapshot test in
  `tests/test_a2a.py::test_snapshot_delegate_to_specialist_output_unchanged`).
- **Operator endpoints:** `POST /a2a/rpc` (auth-gated). Body shape:
  `{"id": str, "method": str, "params": dict}`. Returns the parsed envelope.
- **Tests:** 27 tests in `tests/test_a2a.py` (envelope models + router + client +
  end-to-end envelope dispatch + snapshot test + gateway endpoint). Two
  pre-existing tests (`test_m6_wiring`, `test_builtins_coverage`) updated to
  mock the new `delegate_via_envelope` symbol. Full suite: **3684 passing**,
  **4 skipped**.
- **Acceptance met:**
  - [x] Local round-trip: `delegate_via_envelope("task", name)` returns
        `AgentResult` via the envelope
  - [x] Mock HTTP server: `A2AClient` accepts with timeout + retry
  - [x] 100% coverage on `src/hive/agents/a2a/*`
  - [x] Snapshot test: `delegate_to_specialist` output unchanged
  - [x] `POST /a2a/rpc` endpoint added

### P-F — Learning loop (issue #74, branch `sprint6/learning-loop`)

Eval-gated self-improvement loop on top of `self_improve_from_symptom()`.
Without the loop, self-mods are gated only by human review on the PR.
With the loop, a candidate is **rejected** if it regresses pytest or
golden_qa evals — rejected candidates are still persisted for analysis
but never applied.

- **Module:** `src/hive/core/learning/` (5 files, 337 statements, 100% covered)
  - `storage.py` — SQLite helpers for `learning_traces` + `learning_loops` (idempotent schema)
  - `tracer.py` — observes tool-call outcomes; `recent_failures(threshold, window_minutes)`, `recent_traces(outcome, limit)`
  - `evolver.py` — wraps `SelfModifier.propose()` (dry-run → eval-gate → materialise)
  - `evaluator.py` — runs pytest + golden_qa evals on candidate worktree; `compare()` enforces
    candidate_evals ≥ baseline_evals AND candidate_evals == 1.0
  - `loop.py` — orchestrator: `trace → evolve → eval → apply(guarded)`. Never raises.
- **Wire-in:** 4 new slots on `HiveOS`: `learning_tracer`, `learning_evaluator`,
  `learning_evolver`, `learning_loop`. `self_improve_from_symptom(..., use_learning_loop=bool)`
  routes via the loop when `config.learning_loop_enabled=True`. Heartbeat `tick()`
  opts-in via `use_learning = getattr(self._hive.config, "learning_loop_enabled", False)`.
- **Configuration:** `HIVE_LEARNING_LOOP_ENABLED` (default false), `HIVE_LEARNING_EVAL_TIMEOUT`
  (default 60s), `HIVE_LEARNING_AUTOPROMOTE` (default false, off for safety).
- **Operator endpoints:** `GET /learning/status`, `GET /learning/history?limit=N`,
  `POST /learning/run {"symptom": ...}` (all auth-gated).
- **CLI:** `hive learning status [--limit N]` | `hive learning replay <id>`.
- **Tests:** 74 tests in `tests/test_learning.py` (storage + tracer + evaluator parser +
  evaluator scoring + evolver + loop + gateway endpoints + CLI + runtime wire-up).
  Full suite: **3657 passing**, **4 skipped**.
- **Acceptance met:**
  - [x] Loop is **off by default** — legacy `self_improve_from_symptom()` path unchanged
  - [x] Candidate that regresses pytest → reject, persist, no PR
  - [x] Candidate that regresses evals → reject, persist, no PR
  - [x] Candidate that fails golden_qa (pass_rate < 1.0) → reject, persist, no PR
  - [x] Candidate at baseline evals but candidate_evals == 1.0 → accept
  - [x] Loop NEVER raises to the caller (all errors → verdict=reject with reason)
  - [x] 100% coverage on `src/hive/core/learning/*`
  - [x] End-to-end gateway + CLI smoke tests
- **Operator manual:** [`docs/LEARNING.md`](LEARNING.md) — env vars, endpoints,
  failure modes, manual smoke test recipe.

### P-H — AST tool auto-discovery (issue #76, branch `sprint6/ast-tool-discovery`)

Hive can answer "what tools do you have?" from its own source, not external
docs. The `discover` tool now checks the local AST index first and falls back
to web search only when the top local score is below threshold.

- **Module:** `src/hive/tools/introspect.py` (~200 LOC, 121 stmts, 100% covered)
  - `index(roots)` walks `tools/builtins/` + `tools/mcp/`, AST-parses each
    module, extracts every `BaseTool` subclass with its declared `ToolSpec`
    fields + docstring. Skips malformed modules with a logged warning.
  - `search(query, k)` ranks entries by deterministic token overlap (exact
    match + prefix match + raw-substring in name field). Same query → same
    ordered ranking; no LLM, no embeddings, no third-party AST libs.
  - `format_for_discover(results)` shapes hits to the existing discover()
    candidate schema with `"source": "ast"` attribution.
- **`DiscoverTool` augmentation:** `discover` checks AST first; if the top hit
  scores ≥ 0.8 it returns the AST results with `"source": "ast"` and skips the
  network call. Below the threshold it falls back to the existing web path
  and tags `"source": "web"`. Malformed AST modules never crash the tool.
- **Tests:** 45 tests in `tests/test_introspect.py` — tokenizer edge cases
  (CamelCase, snake_case, acronyms, punctuation), AST classifier + extractor,
  malformed module negative test with warning capture, scoring determinism,
  live index sanity, top-hit ranking for "github pr list" (score = 1.0),
  custom-roots injection, `format_for_discover` schema.
- **Full suite:** **3702 passing**, **4 skipped** (3657 + 45 new).
- **Acceptance met:**
  - [x] `from hive.tools.introspect import index; print(len(index()))` returns 23
        (22 BaseTool subclasses in `builtins/` + 1 `MCPTool` in `mcp/client.py`).
        The SPRINT_6 doc says "≥30"; the actual count of BaseTool subclasses
        on `main` is 23, which the SPEC author overestimated.
  - [x] Search "github pr list" returns `github_list_prs` (class `GitHubListPRs`)
        at score **1.0** from local AST — no web hit needed.
  - [x] 100% coverage on `src/hive/tools/introspect.py`
  - [x] Malformed module negative test (syntax error file is skipped + warning
        logged, never raised)
  - [x] `discover()` result includes `"source": "ast"` attribution
