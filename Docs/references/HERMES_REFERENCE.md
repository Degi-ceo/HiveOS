# HERMES_REFERENCE — full deep audit

> Deep audit of `/home/user/hermes-agent` for the HiveOS unification effort.
> Hermes Agent (Nous Research, MIT) = **"the self-improving AI agent"** — the only
> reference agent with a built-in **learning loop** (creates/improves skills from
> experience, curates its own memory, searches its own past). Python 3.11+, flat
> layout (not src/), ~2,100 `.py` + a TS/Ink TUI. It is the **closest peer to a
> Python-first HiveOS** and the **richest REUSE-READY source** alongside OpenJarvis.
> Note: HiveOS's CLAUDE.md already calls the memory runtime "Hermes" — this repo is
> the actual Hermes Agent; treat its memory/learning design as a primary input to
> the HiveOS↔Mnemosyne integration. Sources: first-hand reads of README + AGENTS.md
> (a detailed architecture doc) + four code-level deep-read passes.

## Coverage tiers

| Tier | What | How |
|------|------|-----|
| A — exhaustive (architectural) | README.md, AGENTS.md (Plugins/Skills/Curator/Cron/Kanban/Delegation/Toolsets/Profiles); structure of `run_agent.py`, `agent/conversation_loop.py`, `model_tools.py`, `toolsets.py`, `hermes_state.py`; `agent/` internals (~110 files: adapters, credential pool, context/compression, prompt, error/retry, memory); `tools/registry.py` + `tools/environments/`; `cron/`; `gateway/` + `gateway/platforms/base.py`; plugin contracts (`agent/memory_provider.py`, `providers/base.py`, `hermes_cli/plugins.py`); `agent/curator.py`, `trajectory_compressor.py`; `mcp_serve.py`, `acp_adapter/` | direct reads + 4 subagents |
| B — sampled | huge files read structurally not line-by-line (`run_agent.py` 6.3k LOC, `conversation_loop.py` ~5k, `cli.py` 16k, `auxiliary_client.py` 7.6k, `acp_adapter/server.py` 81KB); representative platforms (telegram/discord/matrix/email), providers (anthropic), memory plugins (honcho); `hermes_cli/` 124 files (entry + key subcommands); `skills/`, `optional-skills/` (format + examples) | entrypoints + samples |
| C — enumerated | full `tools/` 99-file catalog, all 20+ platforms, ~35 model-provider plugins, all toolsets, `ui-tui/` (Ink/React TS), `website/`, `tests/` (~17k) | listed/categorized |

Complex files flagged (not fully transcribed): `conversation_loop.py`, `tool_executor.py`,
`tools/mcp_tool.py` (3,915 LOC MCP client), `anthropic_adapter.py` (96KB).

---

## 1. What Hermes is (one paragraph)

A lightweight, **provider-agnostic, sync-first** Python agent that runs anywhere
($5 VPS → GPU cluster → serverless), talks to you across **20+ messaging
platforms** from one gateway process, and **improves itself**: it creates skills
from experience and a background **Curator** consolidates/archives them; it curates
agent-managed memory (MEMORY.md/USER.md) with periodic nudges; it does FTS5
session search for cross-session recall; it captures trajectories for training the
next generation of tool-calling models. Core pieces: `run_agent.py` (`AIAgent`
conversation loop), `model_tools.py` + `tools/registry.py` (self-registering tool
catalog), provider adapters (Anthropic/OpenAI-Responses/Bedrock/Gemini + ~35
model-provider plugins), `agent/credential_pool.py` (multi-key failover),
`agent/context_compressor.py` (LLM summarization with head/tail protection),
`hermes_state.py` (`SessionDB` SQLite+FTS5), `gateway/` (multi-platform),
`cron/` (NL scheduling), `acp_adapter/` + `mcp_serve.py` (IDE + MCP surfaces),
`tools/environments/` (6 terminal backends), `tools/delegate_tool.py` (parallel
subagents), and a plugin system with three contracts (general, memory-provider,
model-provider).

---

## 2. Layout (flat, not src/)

```
run_agent.py        AIAgent — core conversation loop (~6.3k LOC; forwards to agent/)
model_tools.py      tool registry+dispatch, discover_builtin_tools, handle_function_call, _run_async
toolsets.py         TOOLSETS dict, _HERMES_CORE_TOOLS, resolve_toolset
cli.py              HermesCLI REPL (~16k LOC; prompt_toolkit, skins, slash commands)
hermes_state.py     SessionDB — SQLite session store + FTS5(+trigram) search, compression locks
hermes_constants.py profile-aware paths (get_hermes_home, ContextVar override)
batch_runner.py     parallel batch trajectory generation
trajectory_compressor.py  token-budget trajectory compression for training
mcp_serve.py        Hermes-as-MCP-server (stdio, 9-tool conversation/approval surface)

agent/   (~110 files — the runtime internals)
  conversation_loop.py   run_conversation(): turn loop, tool dispatch, retry/rotate/compress
  agent_init.py          init_agent() populates ~60 AIAgent attrs
  tool_executor.py       sequential + concurrent(ThreadPool) tool execution
  tool_dispatch_helpers.py, tool_guardrails.py, tool_result_classification.py, file_safety.py
  anthropic_adapter.py bedrock_adapter.py gemini_native_adapter.py gemini_cloudcode_adapter.py
    codex_responses_adapter.py codex_runtime.py auxiliary_client.py chat_completion_helpers.py
  credential_pool.py credential_persistence.py credential_sources.py secret_sources/
    rate_limit_tracker.py nous_rate_guard.py account_usage.py credits_tracker.py
  context_compressor.py context_engine.py conversation_compression.py context_references.py
    prompt_builder.py system_prompt.py prompt_caching.py memory_manager.py memory_provider.py
  error_classifier.py(FailoverReason) retry_utils.py iteration_budget.py
  curator.py curator_backup.py insights.py background_review.py title_generator.py
  skill_bundles.py skill_commands.py skill_preprocessing.py skill_utils.py
  model_metadata.py usage_pricing.py message_sanitization.py think_scrubber.py redact.py
  trajectory.py display.py(KawaiiSpinner) transports/ lsp/ browser_provider.py ...
tools/   (99) registry.py + ~90 tools + environments/(local,docker,ssh,modal,daytona,singularity)
            + delegate_tool.py approval.py kanban_tools.py mcp_tool.py skill_usage.py skills_hub.py
gateway/ (62) run.py session.py config.py delivery.py platform_registry.py stream_*.py
            platforms/(base.py + telegram,discord,slack,whatsapp,signal,matrix,email,sms,webhook,
            api_server,bluebubbles,dingtalk,feishu,weixin,wecom,yuanbao,qqbot,...)
cron/    jobs.py(parse_schedule, store) scheduler.py(tick loop, profile isolation)
plugins/ (132) memory/(honcho,mem0,supermemory,...) model-providers/(~35) context_engine/
            image_gen/ video_gen/ kanban/ observability/ web/ browser/ platforms/
hermes_cli/ (124) main.py config.py auth.py gateway.py kanban.py curator.py profiles.py + subcommands
acp_adapter/ (10) server.py — ACP server (VS Code/Zed/JetBrains)
skills/  built-in skills (SKILL.md format)   optional-skills/  heavier/niche (install on demand)
ui-tui/  Ink/React TS terminal UI   tui_gateway/  Python JSON-RPC backend
providers/ top-level provider registry (lazy)   acp_registry/   cron/
```

---

## 3. The conversation loop (`run_agent.py` + `agent/conversation_loop.py`)

`AIAgent.__init__` takes ~60 params; `agent_init.init_agent()` populates state.
`run_conversation()` is **synchronous** with interrupt checks + budget tracking:

```
restore-or-build system prompt (from SessionDB for prefix-cache byte-match)
while api_call_count < max_iterations and budget.remaining > 0 (or one grace call):
  if interrupt_requested: break
  response = client.chat.completions.create(model, messages, tools=schemas)   # via provider adapter
  if response.tool_calls:
     execute (sequential OR concurrent ThreadPool ≤8, loop-guarded, approval-gated)
     append tool results → messages; api_call_count += 1
  else: return content
on error: error_classifier.classify_api_error() → FailoverReason →
          {retry(backoff+jitter) | rotate credential | compress context | fallback model | abort}
post-turn: save trajectory, persist system prompt, background memory/skill review, cleanup
```

Messages are OpenAI format; reasoning stored in `assistant_msg["reasoning"]`.
~10 optional callbacks (thinking/tool_start/tool_complete/step/stream_delta/status).

## 4. Provider abstraction + resilience (the standout engineering)
- **Stateless adapter per provider** (`agent/*_adapter.py`): each converts
  OpenAI-format ↔ provider format (Anthropic Messages + thinking budget + prompt
  cache; OpenAI Responses/Codex; Bedrock Converse; Gemini native + CloudCode), and
  **re-normalizes errors to OpenAI-shaped dicts** so one classifier works for all.
- **`error_classifier.py` `FailoverReason`** (24 cases) + `classify_api_error()` →
  `ClassifiedError{retryable, should_rotate_credential, should_compress,
  should_fallback}` — matched against 100+ provider error signatures. **Centralizes
  retry logic** instead of scattered if-checks.
- **`credential_pool.py`**: multi-key failover pool (`PooledCredential`); strategies
  fill-first/round-robin/random/least-used; `mark_exhausted()` cooldowns
  (401→5min, 429→1h or provider `reset_at`); merges manual + device-code OAuth.
- **`auxiliary_client.call_llm()`**: side-task LLM (compression/vision/web) with an
  auto fallback chain (main→OpenRouter→Nous→custom→Anthropic→Gemini), 402-retry.
- **`model_metadata.py`** + **`usage_pricing.py`**: live context-length/pricing
  (OpenRouter/models.dev), token estimation, cache-aware cost.
- **Lazy SDK imports** (proxy classes) save 200-400ms cold start.

## 5. Context + prompt + caching
- **`context_compressor.py`**: LLM summarization with **head + tail protection**,
  cheap pre-pass (drop old tool output, shrink images, truncate JSON args),
  structured summary template (Active Task / In Progress / Pending / Resolved),
  iterative summary updates, 20% summary budget; deterministic fallback on failure.
- **`conversation_compression.py`**: trigger logic + payload optimization.
- **`prompt_builder.py`**: stateless system-prompt assembly (identity, platform
  hints, skills index, context files HERMES.md/SOUL.md/.cursorrules, threat scan).
- **`system_prompt.py` + SessionDB**: persist/restore exact system prompt for
  **Anthropic prefix-cache byte-match** reuse across turns; `prompt_caching.py`
  injects `cache_control` markers.
- **Prompt-caching rule (AGENTS.md)**: skill slash-commands injected as a *user*
  message (not system) so the cached prefix stays stable.

## 6. Memory + the learning loop (Hermes's differentiator)
- **`hermes_state.py` `SessionDB`**: SQLite session store — `sessions`/`messages`/
  `compression_locks` tables; **FTS5 + trigram** (CJK substring) full-text search;
  WAL with NFS/SMB fallback to DELETE mode; atomic per-session compression lock;
  soft-delete (`active=0`) for rewind; token-count tracking; parent_session_id chain
  for compaction-split sessions.
- **`agent/memory_provider.py` `MemoryProvider` ABC** + **`memory_manager.py`**:
  **single built-in + at-most-one external** provider. Lifecycle: `initialize`,
  `system_prompt_block`, `prefetch(query)` (before turn), `queue_prefetch` (background
  for next turn), `sync_turn(user, assistant, messages)` (persist), `get_tool_schemas`/
  `handle_tool_call`, plus optional hooks `on_turn_start/on_session_end/
  on_session_switch/on_pre_compress/on_memory_write/on_delegation`. Fail-open. Built-in
  providers (closed set): honcho, mem0, supermemory, byterover, hindsight, holographic,
  openviking, retaindb. **New backends ship as standalone plugin repos.**
- **`agent/curator.py` (the learning loop)**: inactivity-triggered background skill
  maintenance. (1) deterministic auto-transitions active→stale(30d)→archived(90d),
  no LLM; (2) spawns a forked AIAgent with an **umbrella-building consolidation**
  prompt to merge narrow agent-created skills into class-level umbrellas; (3) writes
  run.json + REPORT.md + cron_rewrites.json. Invariants: only touches
  `created_by: agent` skills, **never deletes** (archive only, restorable), pinned
  skills exempt, `curator_backup.py` tar.gz snapshots pre-run. `tools/skill_usage.py`
  tracks use/view/patch counts + state in `~/.hermes/skills/.usage.json`.
- **Skills**: directory with `SKILL.md` (YAML frontmatter: name, description ≤60ch,
  platforms, environments, metadata.hermes.config) + references/templates/scripts/
  assets. agentskills.io-compatible. `skill_commands.py` scans + injects as user
  message; `skill_bundles.py` multi-skill aliases; `skill_preprocessing.py` template
  vars + inline shell expansion. `optional-skills/` installed on demand.
- **`insights.py` + `background_review.py`**: extract learnings post-turn → MEMORY.md;
  periodic session review. **`title_generator.py`**: async session auto-naming.
- **`trajectory_compressor.py`**: token-budget trajectory compression for training
  data (protect first/last N turns, summarize middle, snap to clean tool boundaries,
  async batch with semaphore). `batch_runner.py` parallel trajectory generation.

## 7. Tools, environments, subagents, safety
- **`tools/registry.py`**: **AST-parsed module-level discovery** — parses each tool
  `.py` for `registry.register(...)` calls and imports only registering modules (zero
  circular imports); generation-based cache invalidation; **30s TTL availability
  checks** (`check_fn`); thread-safe snapshots; `dynamic_schema_overrides` for
  config-aware schemas. ~90 tools across the `TOOLSETS` groups.
- **`tools/environments/`**: 6 terminal backends (local/docker/ssh/modal/daytona/
  singularity) behind `BaseEnvironment`; **session-snapshot + re-source pattern**
  (capture env/functions/aliases once, re-source per command — shell state without
  `bash -l` overhead); non-blocking `select()` output drain with interrupt polling;
  duck-typed `ProcessHandle`; Modal/Daytona offer serverless hibernation.
- **`tools/delegate_tool.py`**: spawns isolated child `AIAgent`s in a ThreadPool
  (`max_concurrent_children` default 3); roles leaf/orchestrator (depth-bounded);
  restricted toolset (DELEGATE_BLOCKED_TOOLS); fresh context; TLS approval callback
  per worker; `_active_subagents` registry for observability. Synchronous (not
  durable — use cron/background for long work).
- **`model_tools._run_async()`**: sync↔async bridge with **persistent per-thread
  event loops** (keeps cached async clients alive; avoids "Event loop is closed").
- **Safety**: `file_safety.py` (denylist ~/.ssh/.aws/etc., `HERMES_WRITE_SAFE_ROOT`),
  `tool_guardrails.py` (exact-failure + no-progress loop detection), `tools/approval.py`
  (per-session approval queue, callback-routed), `redact.py` (secret stripping).

## 8. Surfaces
- **MCP server** (`mcp_serve.py`): stdio, 9 tools (conversations_list/get,
  messages_read/send, events_poll/wait, permissions_list/respond, channels_list);
  background SessionDB poller (200ms) → event queue.
- **ACP adapter** (`acp_adapter/server.py`, 81KB): IDE integration (VS Code/Zed/
  JetBrains); NewSession→AIAgent, LoadSession→resume, SendMessage→conversation_loop
  stream; resource-link→content-block + WSL path mapping; thread-local approvals.
- **Gateway** (`gateway/`): one process → 20+ platforms via `BasePlatformAdapter`
  (`connect/disconnect/send_message/process_inbound_event`); media caching, UTF-16
  (Telegram) vs UTF-8 truncation, proxy, path validation; `SessionSource`/
  `SessionContext` injected into system prompt ("you're in Telegram group X");
  `SessionResetPolicy` (daily/idle/both/none); `DeliveryRouter` for cron→platform
  home channels; streaming with platform-specific formatting (Telegram MarkdownV2).
- **Cron** (`cron/`): `parse_schedule` (duration/interval/cron/ISO) + tick loop
  (parallel + per-profile sequential pools); subprocess per job; 3-min hard
  interrupt; catchup/grace windows; file-lock; `skip_memory=True`; toolset gating
  (no cronjob/messaging/clarify); `[SILENT]` suppresses delivery.
- **Kanban** (`tools/kanban_tools.py`, `hermes_cli/kanban.py`): durable SQLite
  multi-agent work board; dispatcher reclaims stale claims, promotes/claims/spawns
  workers; board=hard boundary, tenant=soft namespace; failure-limit auto-block.
- **CLI** (`cli.py` + `hermes_cli/`): prompt_toolkit REPL, central
  `COMMAND_REGISTRY` (`CommandDef`) that drives CLI + gateway + Telegram/Slack menus
  + autocomplete + help from one source; data-driven skin engine; profiles
  (`~/.hermes/profiles/<name>` isolation via ContextVar).

## 9. Plugin system (three contracts)
- **General plugins** (`hermes_cli/plugins.py`, `plugins/<name>/`): `register(ctx)`
  exposes lifecycle hooks (pre/post_tool_call, pre/post_llm_call, on_session_start/
  end), `ctx.register_tool`, `ctx.register_cli_command`. **Rule: plugins MUST NOT
  modify core files** — expand the generic surface instead.
- **Memory-provider plugins** (`plugins/memory/<name>/`): implement `MemoryProvider`
  ABC; per-provider `cli.py` `register_cli`; only active provider's CLI is exposed.
- **Model-provider plugins** (`plugins/model-providers/<name>/`): `__init__.py` calls
  `register_provider(ProviderProfile(name, aliases, api_mode, env_vars, base_url,
  auth_type, default_aux_model, fetch_models))` at import; **lazy** discovery (scan
  on first `get_provider_profile`); user overrides bundled (last-writer-wins).
- Same pattern for context_engine / image_gen / video_gen / observability / web /
  browser / platforms.

---

## 10. REUSE-READY (Python — port into HiveOS)

Hermes is the **deepest pool of directly-portable resilience/runtime code**. Paths
are repo-relative (`/home/user/hermes-agent/...`).

| # | Component | Source | Reuse | Note |
|---|-----------|--------|-------|------|
| 1 | `error_classifier.py` (FailoverReason + classify_api_error) | `agent/error_classifier.py` | Direct/Low | Pure pattern matching; drop the LLM logging field. **High value** for HiveOS model_router resilience |
| 2 | `credential_pool.py` (+persistence/sources) | `agent/credential_pool.py` | Direct/Low | Multi-key failover, cooldowns, OAuth merge; needs a provider/OAuth registry |
| 3 | `rate_limit_tracker.py` | `agent/rate_limit_tracker.py` | Direct/Zero | x-ratelimit header parsing |
| 4 | `retry_utils.py` | `agent/retry_utils.py` | Direct/Zero | jittered backoff |
| 5 | `context_compressor.py` | `agent/context_compressor.py` | Adapt/Med | head/tail-protected LLM summarization; swap auxiliary_client for HiveOS LLM |
| 6 | `prompt_caching.py` | `agent/prompt_caching.py` | Direct/Zero | Anthropic cache_control injection |
| 7 | `message_sanitization.py` | `agent/message_sanitization.py` | Direct/Zero | surrogate/non-ASCII safety, tool-arg JSON repair |
| 8 | `model_metadata.py` | `agent/model_metadata.py` | Adapt/Low | context lengths + token estimation; swap URL constant |
| 9 | `usage_pricing.py` | `agent/usage_pricing.py` | Adapt/Low | cost estimation — **feeds HiveOS budgeter** |
| 10 | `tools/registry.py` (AST discovery + TTL availability) | `tools/registry.py` | Direct/Low | self-registering tool catalog |
| 11 | Terminal-environment abstraction | `tools/environments/base.py` + backends | Adapt/Med | local/docker/ssh/modal/daytona/singularity behind one interface |
| 12 | `_run_async` sync↔async bridge | `model_tools.py` | Direct/Low | persistent per-thread loops |
| 13 | `file_safety.py` | `agent/file_safety.py` | Direct/Zero | sensitive-path denylist |
| 14 | `tool_guardrails.py` | `agent/tool_guardrails.py` | Direct/Low | loop/no-progress detection |
| 15 | `redact.py` | `agent/redact.py` | Direct/Zero | secret redaction for logs |
| 16 | `hermes_state.SessionDB` | `hermes_state.py` | Adapt/Med | SQLite+FTS5 session store, WAL fallback, compression locks — **strong base for HiveOS session/memory index** |
| 17 | `MemoryProvider` ABC + `memory_manager.py` | `agent/memory_provider.py`, `agent/memory_manager.py` | Adapt/Med | **single-slot memory contract — directly wire Mnemosyne as a provider** |
| 18 | Curator (skill lifecycle) | `agent/curator.py` + `tools/skill_usage.py` + `curator_backup.py` | Adapt/Med | deterministic state machine + LLM consolidation; **the self-improvement engine for HiveOS self_mod** |
| 19 | `trajectory_compressor.py` | `trajectory_compressor.py` | Adapt/Low | training-data compression |
| 20 | Cron scheduler | `cron/jobs.py` (parse_schedule) + `cron/scheduler.py` | Adapt/Med | NL scheduling + delivery + profile isolation |
| 21 | Gateway platform abstraction | `gateway/platforms/base.py` + `gateway/session.py` | Adapt/Med | multi-platform + session-context injection (Telegram for HiveOS) |
| 22 | Model-provider plugin contract | `providers/base.py` + `providers/__init__.py` | Adapt/Low | lazy provider registry; **fits HiveOS MiniMax/ChatGPT-Plus routing** |
| 23 | Delegate/subagent | `tools/delegate_tool.py` | Adapt/Med | parallel isolated subagents — fits Hive orchestrator/sub-agents |
| 24 | `hermes_constants.py` profile paths | `hermes_constants.py` | Direct/Low | ContextVar multi-profile home |
| 25 | MCP server | `mcp_serve.py` | Adapt/High | expose HiveOS conversations/approvals over MCP |
| 26 | Central command registry | `hermes_cli/commands.py` | Adapt/Low | one CommandDef list → CLI+gateway+menus+help |
| 27 | `title_generator.py` | `agent/title_generator.py` | Direct/Low | session auto-naming |

## 11. ADAPT-AS-PATTERN (designs to copy)
1. **Self-improvement loop = Curator** — deterministic stale/archive transitions +
   LLM umbrella-consolidation of agent-created skills, never-delete, restorable,
   pinned-exempt, pre-run backup. **The blueprint for HiveOS `self_mod`** (pairs with
   OpenJarvis spec_search + OpenClaw typed-approval/audit). HiveOS's hard rule (never
   edit SOUL.md/approval_gate.py) maps to Curator's "only touch agent-created"
   invariant.
2. **Centralized failover taxonomy** (`FailoverReason`) driving one
   retry/rotate/compress/fallback decision tree — far cleaner than scattered checks.
3. **Credential pool with cooldowns** for multi-key/multi-provider resilience.
4. **Head/tail-protected LLM compression** (not naive truncation) + structured
   summary template + cheap pre-pass.
5. **Prefix-cache via byte-exact system-prompt restore** from SessionDB; skill
   prompts as user messages to keep the cache stable.
6. **Single-slot memory-provider ABC with fail-open + prefetch/queue-prefetch +
   rich lifecycle hooks** — the cleanest memory-integration contract of the four
   repos; **adopt for Mnemosyne**.
7. **AST-parsed self-registering tool discovery** + 30s TTL availability checks.
8. **Terminal-environment abstraction** (local→serverless) with snapshot+re-source.
9. **Three-contract plugin system** (general/memory/model-provider), lazy discovery,
   "plugins never modify core" rule.
10. **One command registry → every surface** (CLI/gateway/menus/help/autocomplete).
11. **NL cron with profile isolation + delivery routing + skip_memory + hard
    interrupt**.
12. **Gateway session-context injection** (agent knows where it's running) +
    multi-platform delivery + reset policy.
13. **Kanban durable multi-agent work board** (board=hard boundary, tenant=soft).
14. **Profiles via ContextVar** for multi-instance isolation.
15. **Persistent per-thread async loops** to keep cached clients alive.

## 12. What NOT to take
- The 20+ platform long tail (take base.py + Telegram + maybe one more).
- ~35 model-provider plugins wholesale (take the contract + MiniMax/Anthropic/OpenAI).
- The Ink/React TS TUI (HiveOS has its own Dashboard; consider OpenJarvis Tauri or
  keep current JSX).
- Mining/Chinese-platform/niche skills.
- Honcho/mem0/etc. as in-tree deps — wire Mnemosyne behind the same `MemoryProvider`
  ABC instead.

## 13. Relevance to current HiveOS (preview of Phase 5/6)
Hermes maps onto HiveOS targets even more directly than OpenJarvis for runtime
*resilience and self-improvement*: `core/model_router.py` ← FailoverReason +
credential_pool + provider adapters + model_metadata; `core/budgeter.py` ←
usage_pricing + account_usage + iteration_budget; `core/orchestrator.py` ←
conversation_loop turn structure + delegate subagents + cron; `core/self_mod.py` +
`core/approval_gate.py` ← Curator (skill lifecycle, never-delete, backups) +
tools/approval.py; `memory/*` + Mnemosyne ← SessionDB(FTS5) + MemoryProvider
single-slot ABC + memory_manager; `gateway/app.py` ← gateway platform abstraction +
session-context injection; `tools/registry.py` ← AST self-registration. **Net:
OpenJarvis gives the clean primitive skeleton; Hermes gives the battle-tested
runtime resilience + the self-improvement loop; OpenClaw gives the architectural
rulebook.** Phase 6 should combine all three with Mnemosyne as the memory layer.
