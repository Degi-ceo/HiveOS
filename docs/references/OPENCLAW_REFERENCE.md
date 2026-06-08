# OPENCLAW_REFERENCE — full deep audit

> Deep audit of `/home/user/openclaw` for the HiveOS unification effort.
> OpenClaw = "the AI that actually does things" — a privacy-first, **plugin-centric,
> multi-channel personal assistant**, written in **TypeScript ESM** (Node 22.19+/Bun),
> evolved Warelay→Clawdbot→Moltbot→OpenClaw. It is the **largest and most mature**
> reference repo (~19,800 non-vendored files: src 8.8k, extensions 6.5k across ~120
> plugins, apps 1k, packages 425, docs 759). **Because HiveOS is Python-first, OpenClaw's
> value is almost entirely ADAPT-AS-PATTERN** (designs to copy), not liftable code.
> The few language-agnostic artifacts (protocol schemas, algorithms, SQLite schemas)
> are flagged separately. Sources: first-hand reads of `VISION.md`, `docs/agent-runtime-
> architecture.md`, the repo's own `AGENTS.md` (a rich architecture-decision doc) +
> four code-level deep-read passes.

## Coverage tiers

| Tier | What | How |
|------|------|-----|
| A — exhaustive (architectural files) | `VISION.md`, `docs/agent-runtime-architecture.md`, root `AGENTS.md`/`CLAUDE.md`, `docs/CLAUDE.md`; entrypoints + contracts of `src/agents/`, `src/llm/`, `src/plugins/`, `src/gateway/`, `src/config/`, `src/state/`, `src/channels/`, `src/tools/`, `src/mcp/`, `src/acp/`, `src/memory*`, `src/context-engine/`, `src/skills/`, `src/cron/`, `src/tasks/`, `src/commitments/`, `src/auto-reply/`, `src/security/`, `packages/{agent-core,llm-core,llm-runtime,model-catalog-core,tool-call-repair,gateway-protocol,acp-core,memory-host-sdk,net-policy,plugin-sdk}` | direct reads + 4 subagents |
| B — sampled | large impl files (`embedded-agent-runner/run.ts` 166KB, `compact.ts` 62KB, `agent-session.ts` 111KB), `src/secrets/` (~120 files), representative `extensions/` (minimax, telegram, memory-lancedb/wiki/active-memory, codex, codex-supervisor) | entrypoints + manifests read |
| C — enumerated | the ~120 `extensions/` (grouped by category), `apps/`, `ui/`, `docs/**`, `test/`, `scripts/`, `qa/`, lockfiles | categorized, not line-by-line |

Nothing flagged unreadable. `src/trajectory/`, `src/context-engine/`, `src/crestodian/` were lightly covered (purpose noted as low-confidence where so).

---

## 1. What OpenClaw is (one paragraph)

A **terminal-first, plugin-first orchestration platform** for a personal assistant
that runs real tasks on real computers, across many messaging channels, with
strong security defaults. Core stays deliberately lean and **plugin-agnostic**;
nearly all capability (≈120 providers, channels, memory backends, media, tools)
ships as **extensions** behind a strict plugin-SDK boundary. The built-in agent
runtime (`src/agents/embedded-agent-runner/`) runs a streaming, hook-extensible
turn loop with context compaction, tool-loop detection, terminal-outcome
normalization, and provider failover. A typed **gateway protocol** (TypeBox) over
WebSocket connects clients; **SQLite-only** storage (Kysely) with `openclaw doctor
--fix` migrations is mandated (no JSON sidecars, no runtime compat shims). Channels
are **transport-only** with typed presentation actions. Memory is a **single-active-
plugin slot**. MCP is supported as both client and server; ACP (Agent Client
Protocol) bridges multi-language agent SDKs to the gateway. External harnesses
(OpenAI Codex) are folded in as plugins, not parallel systems.

---

## 2. Architecture decisions worth internalizing (from the repo's own AGENTS.md)

These are explicit, hard-won rules — directly applicable to HiveOS design:

- **Core stays plugin-agnostic.** No bundled ids/defaults/policy in core when
  manifest/registry/capability contracts work. Plugins cross into core only via
  `openclaw/plugin-sdk/*` barrels + manifest metadata + injected runtime helpers.
- **Canonical-config-only runtime.** Runtime reads only the latest config shape;
  legacy/malformed shapes are migrated by `openclaw doctor --fix`, never by
  runtime shims/aliases/fallback readers. Core config repaired in core doctor;
  plugin config in that plugin's doctor contract (`legacyConfigRules`).
- **SQLite-only storage, Kysely access.** No JSON/JSONL/TXT/sidecar files for
  runtime state/caches/queues/registries/indexes/cursors. Shared state DB
  `state/openclaw.sqlite` for global + plugin KV; per-agent DB
  `agents/<id>/agent/openclaw-agent.sqlite` for agent-scoped state. Migrations
  database-first; old file stores live only in doctor migration code.
- **Fallback is a product decision, not convenience.** Name the shipped contract,
  failure mode, removal plan, and why doctor can't solve it — else delete it.
  "Shipped" = reachable from a release git tag.
- **Channels are transport-only.** They render portable presentation/actions,
  enforce transport limits, map native callback envelopes. They do **not** own
  product command trees or provider/plugin policy. Portable command UI uses typed
  presentation actions, not raw string inference.
- **Hot paths carry prepared facts forward** (provider id, model ref, channel id,
  capability family); no request-time rediscovery, no freshness polling
  (`stat`/`realpath`/JSON reread) in runtime hot paths.
- **Gateway protocol changes additive-first**; incompatible needs versioning +
  client follow-through; version bumps owner-confirmed only.
- **Lean code.** Refactors should reduce non-test LOC; one canonical path, delete
  the old one unless a cited shipped contract requires compat.
- **Why TypeScript?** "Orchestration system: prompts, tools, protocols,
  integrations" — chosen for hackability. (HiveOS picks Python for the same
  reason within its ecosystem.)

---

## 3. Source map (`src/` ~70 dirs, `packages/` 21, `extensions/` ~120)

```
agents/      embedded-agent-runner/(run.ts, model.ts, compact.ts, thinking.ts,
             context-engine-maintenance.ts, run/backend.ts, run/failover-policy.ts),
             sessions/(agent-session.ts, model-registry.ts), agent-hooks/(context-pruning/),
             agent-tools.ts, tool-loop-detection.ts, tool-search.ts, auth-profiles/,
             agent-run-terminal-outcome.ts, tools/(bash,read,write,edit,glob,grep,...)
llm/         providers/(anthropic, openai-*, google-shared, mistral, register-builtins),
             providers/stream-wrappers/, env-api-keys.ts            ← provider adapters
routing/     resolve-route.ts session-key.ts binding-scope.ts       ← channel→session routing
model-catalog/ manifest-planner.ts provider-index/ authority.ts     ← model discovery
channels/    message/ message-access/ inbound-event/ transport/ turn/ plugins/ status/
tools/       types.ts(ToolDescriptor) planner.ts execution.ts availability.ts boundary.ts
commands/ chat/                                                     ← command tree, chat
mcp/         channel-server.ts channel-bridge.ts plugin-tools-serve.ts openclaw-tools-serve.ts
acp/         server.ts translator.ts control-plane/manager.ts event-ledger.ts persistent-bindings.ts
memory/ memory-host-sdk/                                            ← single-slot memory contract
context-engine/ types.ts delegate.ts registry.ts host-compat.ts    ← pluggable context mgmt
skills/      discovery/ loading/ lifecycle/ runtime/ research/ security/ config/
plugins/     manifest.ts bundle-manifest.ts activation-context.ts api-facades.ts types.ts
plugin-sdk/ plugin-state/ extensionAPI.ts                          ← plugin boundary
gateway/     server/ws-connection/ credentials.ts boot.ts          ← WebSocket gateway
config/      types.*.ts doctor.ts sessions/(store.ts SQLite)        ← canonical config
state/       openclaw-state-db.ts openclaw-agent-db.ts *.generated.ts ← SQLite + Kysely
secrets/     resolve.ts apply.ts runtime-*.ts (~120 files)          ← SecretRef resolution
cron/ tasks/ commitments/ auto-reply/ flows/ trajectory/           ← autonomy/scheduling
security/    audit.ts audit-plugins-trust.ts exec-filesystem-policy.ts
daemon/ node-host/ process/ bootstrap/ pairing/ status/ sessions/
web/ web-fetch/ web-search/ link-understanding/                    ← web tools
media-generation/ media-understanding/ tts/ image-generation/ realtime-transcription/
tui/ interactive/ wizard/ i18n/ logging/ infra/ shared/ utils/ types/

packages/    agent-core llm-core llm-runtime model-catalog-core tool-call-repair
             gateway-protocol gateway-client acp-core plugin-sdk plugin-package-contract
             memory-host-sdk net-policy normalization-core markdown-core sdk
             media-core media-generation-core media-understanding-common
             model-catalog-core speech-core terminal-core web-content-core

extensions/  ~120 plugins (see §8 for categorization)
```

---

## 4. The built-in agent runtime (the centerpiece)

Runtime in `src/agents/embedded-agent-runner/` (`run.ts` ~166KB); reusable core in
`packages/agent-core/` (`agent-loop.ts`). The **turn loop**:

```
runEmbeddedAttempt():
 SETUP: resolve model (model.ts; agent overrides, live switch, fallback chain)
        → resolve auth profile (auth-profiles/order.ts) → build runtime context → load tools
 TURN LOOP (while !shouldStop):
   turn_start → add user msg
   PROMPT PREP: system prompt + think-level map (per provider) + tool schema projection
                (anthropic native / openai function / google decl / mistral) + cache prep
   CONTEXT PRUNE: token accounting vs model max (context-engine-maintenance.ts);
                  extension hooks; overflow → compaction
   PROVIDER REQUEST: run/backend.ts selects adapter, builds request, provider.stream()
   STREAM: assistant_start → [delta|thinking|tool_call_start/delta/end] → assistant_end
           (final AssistantMessage: text, toolCalls, thinking, usage, stop_reason)
   TOOL EXEC (per call): beforeToolCall hook → dispatch (bash/read/write/edit/glob/grep/
              search/message/MCP) → truncate result → afterToolCall hook → append result
   TURN END: tool_result events → tool-loop-detection → shouldStopAfterTurn hook
   COMPACTION (if overflow): compact.ts builds successor transcript (keep assistant turns,
              summarize old user msgs, preserve tool results), pre/post hooks, 5-min safety
              timeout, post-compaction loop guard
   NEXT TURN: prepareNextTurn hook (can override context/model/thinking) or stop
 FINALIZE: agent-run-terminal-outcome.ts normalizes → completed|hard_timeout(sticky)|
           timed_out|cancelled(sticky)|aborted|blocked|failed; cleanup; return
```

Key contracts:
- **Stream functions never throw** — failures encoded as stream events; final event
  is always an AssistantMessage with a canonical `stopReason`.
- **Hook points** (`packages/agent-core/src/types.ts`): `beforeToolCall`
  (block/override), `afterToolCall` (override content/isError/terminate),
  `shouldStopAfterTurn`, `prepareNextTurn` (rewrite model/context/thinking).
- **Terminal-outcome state machine** (`agent-run-terminal-outcome.ts`): sticky
  hard_timeout/cancelled, liveness-block precedence, phase attribution — one place
  to derive timeout/cancel precedence (don't rederive in projections).

## 5. Provider/model layer
- **Provider contract** (`packages/llm-core/src/types.ts`): `ApiProvider.stream(model,
  context, options) → AsyncIterable<AssistantMessageEvent>`. Registry
  (`packages/llm-runtime/src/api-registry.ts`) `registerApiProvider`/`getApiProvider`;
  builtins lazy-load (`src/llm/providers/register-builtins.ts`).
- **Per-provider adapters** (`src/llm/providers/*`, 44–51KB each): anthropic,
  openai-completions, openai-chatgpt-responses, google-shared, mistral, azure. Each
  converts context→provider request, formats tool calls, normalizes the SDK stream
  to unified events, computes usage/cost.
- **Model catalog compat config** (`packages/model-catalog-core/src/model-catalog-
  types.ts`): per-model flags — `supportsStore/supportsTools/supportsReasoningEffort`,
  `thinkingFormat` (openai|openrouter|deepseek|together|qwen|zai), `toolSchemaProfile`,
  `unsupportedToolSchemaKeywords`, `cacheControlFormat`, `openRouterRouting`, etc.
  Provider-neutral; **this is a language-agnostic schema worth porting**.
- **Tool-call-repair** (`packages/tool-call-repair/*`): detects plain-text tool
  calls in streams, recovers malformed JSON (grammar.ts), promotes to structured
  blocks — provider-agnostic robustness layer.
- **Prompt caching**: deterministic ordering of tools/messages/extensions; session
  affinity key; short (5min) vs long (1h Anthropic) retention.
- **Failover** (`run/failover-policy.ts`): auth_failure→rotate_profile,
  rate_limit→fallback_model, context_overflow→compaction, timeout→hard_timeout.
  Auth-profile cooldown prevents hammering broken credentials.

## 6. Plugin architecture (OpenClaw's defining feature)
- **Two plugin styles**: **code plugins** (`openclaw.plugin.json` + `src/index.ts`,
  runtime hooks: providers/channels/tools/runtimes) and **bundle-style** (codex/
  claude/cursor manifests; capabilities auto-detected from file paths; smaller,
  more stable, better security boundary — preferred when expressible).
- **Manifest-first discovery + lazy activation** (`src/plugins/manifest.ts`,
  `bundle-manifest.ts`, `activation-context.ts`): config + manifest → activation
  snapshot → lazy runtime load. Config validated **before** runtime loads.
- **Plugin-SDK boundary** (`packages/plugin-sdk`, `src/plugin-sdk/*`,
  `extensionAPI.ts`): plugins use only public barrels (`api.ts`, `runtime-api.ts`)
  + manifest metadata + injected helpers; never import core `src/**`. Bundled and
  external plugins use the **same** SDK seams (no private backdoors). External
  official plugins own their deps and are excluded from core dist; core resolves
  them via registry-aware `facade-runtime`.
- **Extension API surface** (`definePluginEntry({ id, configSchema, register(api){...} })`):
  `api.registerProvider/registerChannel/registerTool/registerCommand/
  registerMemory*/registerAgentHarness/registerNodeHostCommand`, `api.lifecycle`,
  `api.runtime` (config/state/sandbox/logging), `api.pluginConfig`.

## 7. Gateway, config, storage, secrets
- **Gateway** (`src/gateway/`, `packages/gateway-protocol`, `packages/gateway-client`):
  WebSocket + RPC; TypeBox schema registry (`protocol-schemas.ts`) compiled to
  validators; methods for agents (create/update/send/poll/wait/events), channels,
  talk (realtime audio), tools (catalog/invoke), config, approvals, plugins.
  Additive-first versioning. **Language-agnostic protocol artifact.**
- **Config** (`src/config/types.*.ts`, `doctor.ts`): modular per-feature canonical
  types; canonical-only runtime reads; `openclaw doctor --fix` migrates old shapes
  (legacy file/sidecar → normalized SQLite, with audit trail). High bar for new
  config/env surfaces; prefer removal/consolidation.
- **Storage** (`src/state/openclaw-state-db.ts`, `openclaw-agent-db.ts`): SQLite +
  Kysely; shared state DB (global + plugin KV) vs per-agent DB; schema-versioned;
  migration audit tables (`migration_runs`); `0o700`/`0o600` perms; WAL. No raw SQL
  except DDL/migrations.
- **Secrets** (`src/secrets/`, `~/.openclaw/credentials/`, per-agent
  `auth-profiles.json`): `SecretRef` syntax (`$env:VAR`, `$file:path`); precedence
  modes (env-first/config-first); fail-closed (raise on unresolved ref).

## 8. I/O surfaces
- **Channels** (`src/channels/`): transport-only; **typed presentation actions**
  (command/url/web-app/select/approval) as a closed discriminated union so transport
  adapters never string-guess `/command`; durable send lifecycle
  (`beforeSendAttempt`/`afterSendSuccess`/`afterCommit`), `DurableFinalDeliveryCapability`,
  `reconcileUnknownSend()` for ambiguous-state recovery; normalized `MessageReceipt`
  + `ConversationDescriptor` for reply routing.
- **Tools** (`src/tools/`): **descriptor + planner + executor** separation —
  `ToolDescriptor{owner(core|plugin|channel|mcp), executor, availability}`;
  `ToolAvailabilityExpression` over signals (always/auth/config/env/plugin-enabled/
  context); planner evaluates once → `{visible, hidden}` plan shared by all sources;
  executor routes to the owner. Effective tool policy layered
  (global+profile+provider+group+sandbox+subagent).
- **MCP** (`src/mcp/`): two standalone stdio servers — channel-server (bridges
  conversations to MCP clients like Claude Code) and plugin-tools-serve (exposes
  plugin tools, e.g. memory_recall). MCP treated as a **public API surface**, not a
  hack. Also an MCP client integration.
- **ACP** (`src/acp/`, `packages/acp-core`): language-neutral Agent Client Protocol
  over stdio — bridges multi-language agent SDKs to the gateway; session translator,
  control-plane manager, SQLite event ledger for reconnect durability,
  persistent-bindings. `acp-core` is pure types (Python/Rust/Go SDKs can implement).
- **Memory** (`src/memory-host-sdk/`, `packages/memory-host-sdk`): **single-active-
  memory-plugin slot**; stable `MemorySearchManager` contract (search/readFile/status/
  sync/probeEmbedding); backends builtin LanceDB/FTS or QMD (Quartz markdown);
  embedding-provider routing with FTS fallback. Bundled memory plugins:
  `memory-lancedb` (vector + auto-capture/recall), `memory-wiki` (Obsidian vault +
  LLM-managed index, `wiki_search`/`wiki_get`), `active-memory` (recent-context).
- **Context-engine** (`src/context-engine/`): pluggable interface (bootstrap/ingest/
  assemble/compact/maintain/afterTurn + subagent spawn hooks); third-party engines
  can `delegateCompactionToRuntime()`; host-capability declarations.
- **Skills** (`src/skills/`): workspace-first precedence; discovery/loading/lifecycle
  (install from tarball/GitHub/ClawHub, signature verify, security audit)/runtime
  (session-scoped policy + snapshot + cron). New skills → ClawHub, not core.

## 9. Autonomy (the three-layer proactivity model — high value for HiveOS)
- **`src/cron/`**: scheduled background jobs (`CronService` facade over locked ops;
  schedule string, delivery context, session reaper, heartbeat).
- **`src/tasks/`**: agent-driven work items (SQLite registry, delivery state
  machine pending→delivered→failed, executor policy decides auto-notify vs parent
  review, flow runtime syncs external results back).
- **`src/commitments/`**: **inferred future obligations** extracted from
  conversation after each turn (background model call, batched, scheduled into
  cron/tasks). Ephemeral active recall + long-term + knowledge vault = stacked memory.
- **`src/auto-reply/`**: proactive reply dispatch — command detection, heartbeat
  filtering/debounce, media staging, channel/auth-scoped routing.
- **`src/flows/`**: doctor health checks + repair workflows + model picker + setup.

## 10. Security
- **Audit engine** (`src/security/audit.ts`): collects findings across filesystem
  perms, exec policy, plugin trust, DM rules, dangerous config, gateway auth;
  severity + remediation + suppression; **plugins register audit collectors**.
- **Exec/filesystem policy** (`exec-filesystem-policy.ts`): safe-bin denylist,
  writable-path restriction, symlink-escape prevention, Docker/browser isolation.
- **`packages/net-policy/`**: reusable IP parsing (private/loopback/CIDR), URL
  userinfo stripping, sensitive redaction — used in logging, API calls, audit.
- **Plugin trust** (`audit-plugins-trust.ts`): validates no deep core imports, no
  relative escapes, manifest metadata.

## 11. Extensions ecosystem (~120, Tier C — categorized)
- **LLM providers (~50)**: anthropic, anthropic-vertex, openai, **minimax**, qwen,
  deepseek, groq, mistral, google, xai, perplexity, together, openrouter, ollama,
  lmstudio, vllm, sglang, huggingface, cerebras, fireworks, deepinfra, moonshot,
  zai, kimi-coding, bedrock, azure/foundry, vertex, litellm, vercel-ai-gateway,
  cloudflare-ai-gateway, copilot, novita, nvidia, venice, synthetic, … Each =
  `definePluginEntry` registering providers; manifest in `package.json` `openclaw`.
- **Messaging channels (~25)**: telegram, discord, slack, signal, whatsapp, matrix,
  imessage, sms, msteams, mattermost, feishu, googlechat, irc, twitch, line, nostr,
  zalo, qqbot, tlon, synology-chat, nextcloud-talk, webhooks, voice-call, … Each uses
  `defineBundledChannelEntry` (lazy plugin, secrets contract, account inspector).
- **Memory (4)**: memory-core (host engine), memory-lancedb, memory-wiki,
  active-memory.
- **Media (~12)**: image/video/music/media-understanding cores, elevenlabs,
  azure-speech, deepgram, inworld, senseaudio, comfy, fal, runway, pixverse.
- **Web/search (~8)**: brave, duckduckgo, exa, tavily, firecrawl, searxng,
  web-readability, parallel.
- **External harness**: **codex** (registers `createCodexAppServerAgentHarness()` —
  runs agents in a remote Codex app-server fleet; conversation bindings; migration
  provider; CLI commands) + **codex-supervisor** (MCP fleet-supervision tools).
  Codex is **folded in as a plugin** — no parallel auth/config/routing.
- **Infra/tools**: diagnostics-otel/prometheus, policy, file-transfer, phone-control,
  device-pair, document-extract, diffs, oc-path, qa-*, thread-ownership.

## 12. Rust/desktop/apps (Tier C)
`apps/` + `ui/` host companion apps (macOS/iOS/Android/Windows/Linux per VISION);
TUI via `@earendil-works/pi-tui`. (Not core to a Python HiveOS.)

---

## 13. ADAPT-AS-PATTERN (the design ideas to copy into HiveOS)

Ranked by value for a Python-first personal-AI orchestrator:

1. **Plugin-agnostic core + strict plugin-SDK boundary** (`src/plugins/*`,
   `packages/plugin-sdk`, AGENTS.md Architecture). Keep HiveOS core lean; expose
   providers/channels/tools/memory as plugins via a Python equivalent of
   `definePluginEntry(register(api))` + manifest. **The single most important
   structural lesson** — pairs with OpenJarvis's registry pattern.
2. **Streaming, hook-extensible agent loop with terminal-outcome normalization**
   (`packages/agent-core/src/agent-loop.ts`, `src/agents/agent-run-terminal-
   outcome.ts`). Port the event sequence + hook points (beforeToolCall/afterToolCall/
   shouldStopAfterTurn/prepareNextTurn) and the sticky terminal-outcome decision
   tree — these are language-agnostic algorithms.
3. **SQLite-only storage + doctor migrations + canonical-config-only runtime**
   (AGENTS.md; `src/state/*`, `src/config/doctor.ts`). HiveOS should store agent
   state/sessions/traces/approvals in SQLite and migrate via a `hive doctor --fix`
   equivalent — no JSON sidecars, no runtime compat shims. Aligns with OpenJarvis's
   SQLite-first choice.
4. **Transport-only channels with typed presentation actions** (`src/channels/
   message/types.ts`). HiveOS surfaces (Telegram/voice/dashboard) render portable
   typed actions (command/url/select/approval); core declares intent, channels map.
   Avoids string-guessing. Includes `reconcileUnknownSend()` durable-queue recovery.
5. **Tool descriptor + planner + executor separation with declarative availability**
   (`src/tools/{types,planner,execution}.ts`). One tool plan across core/plugins/
   channels/MCP; availability as boolean expressions over signals (auth/config/env/
   plugin/context), evaluated once per turn (no repeated probing).
6. **Context-engine as a pluggable interface with runtime-delegation**
   (`src/context-engine/types.ts`, `delegate.ts`) + compaction successor algorithm
   (`embedded-agent-runner/compact.ts`: keep assistant turns, summarize old user
   msgs, loop-guard). Lets HiveOS swap context strategies and reuse a default
   compactor.
7. **Single-active-memory-plugin slot** (`packages/memory-host-sdk`). Forces
   arbitration + stable contract + clean FTS fallback. **Apply this to the
   HiveOS↔Mnemosyne integration** — one active memory provider behind a stable
   interface (decide in Phase 6 after Phase 4).
8. **MCP as a first-class public API surface** (`src/mcp/*`) + **ACP for
   multi-language agent bridging** (`src/acp/*`, `packages/acp-core`). HiveOS can
   expose its capabilities over MCP and consume external MCP tools; ACP shows how to
   bridge a Python agent to a gateway with reconnect-durable session ledgers.
9. **Three-layer autonomy: cron + tasks + commitments** (`src/cron`, `src/tasks`,
   `src/commitments`). Separates scheduled jobs, agent-spawned work, and
   model-inferred future obligations — a richer model than HiveOS's current
   heartbeat orchestrator. Plus `auto-reply` proactivity.
10. **Provider registry + model-catalog compat config + tool-call-repair**
    (`packages/llm-runtime/api-registry.ts`, `model-catalog-core/model-catalog-
    types.ts`, `tool-call-repair/*`). Lazy provider registration; per-model
    capability flags (thinking format, tool schema profile, cache format);
    robust plain-text tool-call recovery. Directly relevant to HiveOS's MiniMax +
    ChatGPT-Plus routing.
11. **Auth-profile cooldown + failover decision tree** (`auth-profiles/`,
    `run/failover-policy.ts`). Rotate credentials on failure, fall back models on
    rate-limit, cooldown broken profiles.
12. **Gateway protocol versioning via TypeBox schema registry** (`packages/gateway-
    protocol`). HiveOS gateway should define a typed, versioned protocol (Pydantic/
    jsonschema) rather than ad-hoc JSON.
13. **net-policy as a standalone reusable module** (`packages/net-policy`).
14. **Security audit with plugin-registered collectors** (`src/security/audit.ts`).
15. **External harness folded in as a plugin (Codex pattern)** (`extensions/codex*`).
    HiveOS's ChatGPT-Plus-via-Codex planner and any external runner should be
    plugins, not parallel auth/config/routing systems.

## 14. REUSE-READY (language-agnostic artifacts only — OpenClaw is TypeScript)

These can be ported by re-implementing the spec/algorithm in Python (not lifted):

| Artifact | Source | Form |
|----------|--------|------|
| Agent-loop event/state machine + hook signatures | `packages/agent-core/src/agent-loop.ts`, `types.ts` | algorithm → Python enums/dataclasses |
| Terminal-outcome normalization rules | `src/agents/agent-run-terminal-outcome.ts` | decision tree |
| Tool-call-repair grammar + plain-text detection | `packages/tool-call-repair/*` | regex/grammar algorithm |
| Model-catalog compat-config schema | `packages/model-catalog-core/src/model-catalog-types.ts` | JSON-able schema |
| Thinking-level → provider mapping | `src/agents/embedded-agent-runner/thinking.ts` | mapping table |
| Compaction successor algorithm | `src/agents/embedded-agent-runner/compact.ts` | deterministic transform |
| Tool-loop-detection algorithm | `src/agents/tool-loop-detection.ts` | sequence-hash heuristic |
| Failover decision tree | `src/agents/embedded-agent-runner/run/failover-policy.ts` | cascade rules |
| SQLite session/state schema + migration-audit tables | `src/config/sessions/*`, `src/state/*.generated.ts` | DDL |
| Gateway protocol method/schema spec | `packages/gateway-protocol/src/schema/*` | TypeBox→JSON Schema |
| ACP protocol types | `packages/acp-core/*` | pure types/spec |
| Memory-host-SDK `MemorySearchManager` contract | `packages/memory-host-sdk/host/types.ts` | interface spec |
| Tool availability-signal model | `src/tools/types.ts` | schema |

Everything else is **none (TypeScript)** — provider SDK adapters, channel impls,
skill loaders, secrets pipeline, the TUI, Kysely query code.

## 15. What HiveOS should and should not take from OpenClaw
- **Take the architecture**: plugin boundary, SQLite-first + doctor migrations,
  transport-only typed channels, descriptor/planner/executor tools, pluggable
  context-engine, single-slot memory, three-layer autonomy, MCP/ACP surfaces,
  provider registry + model-catalog compat, terminal-outcome + failover algorithms.
- **Don't lift code** — it's TS. Re-implement the above in Python, ideally on top of
  OpenJarvis's already-Python registry/EventBus/types/engine ABCs (Phase 1), so the
  two references combine: **OpenJarvis = the Python skeleton, OpenClaw = the
  architectural rulebook + protocol/algorithm specs.**
- **Don't take**: the ~120 extensions wholesale, companion apps, mining, the full
  channel/provider long tail, ClawHub/i18n/docs pipeline. Take the *contracts* and
  1–2 channels (Telegram, voice) HiveOS actually needs.

## 16. Relevance to current HiveOS (preview of Phase 5/6)
OpenClaw validates and sharpens HiveOS's intended design: its `gateway/app.py`
should adopt a typed versioned protocol; `core/orchestrator.py` should use a
hook-extensible streaming loop with terminal-outcome normalization;
`core/model_router.py` benefits from the provider-registry + model-catalog-compat +
failover patterns; `core/self_mod.py` + `core/approval_gate.py` align with the
typed-approval-action + audit-collector model; `tools/registry.py` should adopt the
descriptor/planner/executor split; `memory/*` + Mnemosyne should sit behind a
single-slot memory contract; and HiveOS gains a richer autonomy model
(cron+tasks+commitments) than a bare heartbeat. Net: **OpenClaw supplies the
mature architectural patterns that OpenJarvis's Python code can be shaped to fit.**
