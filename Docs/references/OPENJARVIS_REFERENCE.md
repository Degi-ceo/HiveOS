# OPENJARVIS_REFERENCE — full deep audit

> Deep audit of `/home/user/OpenJarvis` for the HiveOS unification effort.
> OpenJarvis = Stanford Scaling-Intelligence's **local-first personal-AI framework**
> ("Personal AI, On Personal Devices"), Python ≥3.10, Apache-2.0. It is the single
> most architecturally relevant reference repo for HiveOS: a Python-first,
> registry-driven, offline-capable agent system. Source of truth for this doc:
> first-hand reads of `docs/architecture/*` + `src/openjarvis/sdk.py`, plus four
> code-level deep-read passes over `src/openjarvis/**`.

## Coverage tiers

| Tier | What | How covered |
|------|------|-------------|
| A — exhaustive | `docs/architecture/*` (overview, design-principles, query-flow, engine, agents, intelligence, learning, memory, skills, channels, security); `src/openjarvis/sdk.py`; `core/`, `engine/`, `agents/`, `sessions/`, `workflow/`, `system/`, `tools/`, `connectors/`, `channels/`, `mcp/`, `a2a/`, `operators/`, `speech/`, `server/`, `cli/`, `security/`, `sandbox/`, `scheduler/`, `telemetry/`, `analytics/`, `prompt/`, `recipes/`, `traces/`, `learning/*` | Read directly (docs+sdk) + 4 subagents read source files |
| B — sampled | `evals/datasets` (43 files), `evals/scorers` (41), `evals/*` harness, `mining/*`, `examples/*` (11 example apps), large `cli/` command set | Entrypoints + representative files read; rest enumerated |
| C — enumerated | `rust/crates` (17 crates, 127 `.rs`), `frontend/` + `desktop/` (Tauri/React TS), `assets/`, `uv.lock`, generated API-reference docs | Characterized from manifests + lib entries, not line-by-line |

Nothing was found unreadable. `~1,290 .py`, `127 .rs`, `51 tsx`, `288 toml`, `115 md`.

---

## 1. What OpenJarvis is (one paragraph)

A framework for building **local-first personal AI agents** organized around
**five primitives** — Intelligence (model catalog), Engine (inference runtime),
Agentic Logic (pluggable agents), Memory (searchable storage), and Learning
(trace-driven feedback) — glued by a **decorator registry**, a thread-safe
**EventBus**, and **SQLite** persistence. Every extension point is an ABC +
registry. It is offline-first (Ollama/vLLM/llama.cpp/MLX local; cloud optional),
hardware-aware (auto-detects GPU/VRAM and recommends engine+model), and
telemetry-native (every inference records latency/tokens/energy/cost). It exposes
a Python SDK (`Jarvis`), an OpenAI-compatible FastAPI server, a 52-command Click
CLI, MCP client+server, A2A agent-to-agent protocol, 30+ messaging channels, 30+
data connectors, a workflow DAG engine, a scheduler, a container/WASM sandbox, a
security guardrails layer, and an advanced LLM-guided self-improvement loop
("spec search"). A Rust workspace mirrors the core via PyO3, and a Tauri desktop
app wraps it.

---

## 2. Source layout (`src/openjarvis/`)

```
core/        registry.py types.py config.py events.py credentials.py   ← foundation
intelligence/ model_catalog.py (BUILTIN_MODELS) + back-compat shims     ← "what is the model"
engine/      _stubs.py(InferenceEngine ABC) ollama cloud openai_compat multi _discovery  ← inference runtime
agents/      _stubs.py(BaseAgent/ToolUsingAgent) simple orchestrator native_react
             native_openhands rlm openhands claude_code(+runner/) executor manager
             loop_guard errors operative monitor_operative deep_research hybrid/  ← agentic logic
sandbox/     runner.py(ContainerRunner/SandboxedAgent) mount_security wasm_runner ← isolation
tools/       _stubs.py(BaseTool/ToolExecutor/ToolSpec) + ~39 tools + storage/    ← tools & memory backends
             mcp_adapter storage/{sqlite,faiss,colbert,bm25,hybrid,chunking,ingest,context,embeddings}
connectors/  _stubs.py(BaseConnector/Document) + ~30 connectors + oauth webhooks store ← data ingestion
channels/    _stubs.py(BaseChannel) + ~30 channels + whatsapp_baileys_bridge/(TS)  ← messaging surfaces
mcp/         protocol.py client.py server.py transport.py loader.py              ← Model Context Protocol
a2a/         protocol.py(AgentCard/A2ATask) client server tool                   ← agent-to-agent
operators/   types.py(OperatorManifest) manager.py loader + data/               ← persistent scheduled agents
speech/      tts.py stt.py + openai/cartesia/kokoro/whisper/deepgram backends    ← voice
prompt/      builder.py(SystemPromptBuilder, frozen-prefix cache) system_prompts ← prompt assembly
recipes/     loader.py composer.py registry + data/builtin_recipes.yaml          ← unified TOML config format
templates/   agent_templates.py engine.py data/                                  ← agent presets
skills/      types manager executor loader parser tool_adapter importer overlay  ← composed-tool skills
             dependency security index sources/{hermes,openclaw,github}
workflow/    graph.py engine.py types.py builder loader store                    ← DAG execution
sessions/    session.py(SessionStore) manager store compression                 ← cross-channel sessions
traces/      store.py(TraceStore) collector.py analyzer.py                       ← interaction recording
telemetry/   instrumented_engine store aggregator energy_{nvidia,amd,apple,rapl} ← metrics + energy
             flops efficiency phase_metrics itl steady_state vllm_metrics
analytics/   client(PostHog) bridge identity aggregator redaction               ← anonymous usage (opt-in)
security/    guardrails scanner audit capabilities file_policy taint ssrf signing ← guardrails
             injection_scanner rate_limiter boundary subprocess_sandbox severity_policy
scheduler/   scheduler.py(TaskScheduler) store.py tools.py                       ← cron/interval/once
server/      app.py routes.py(/v1/chat/completions…) auth_middleware middleware  ← OpenAI-compat API
             session_store channel_bridge stream/ws_bridge cost_calculator +routers
cli/         52 Click command modules (serve chat ask agent daemon scheduler …)  ← terminal surface
daemon/      gateway.py service agent_loop supervisor session_expiry             ← background runner
learning/    routing/ optimize/ training/ agents/ intelligence/ spec_search/     ← trace-driven learning
bench/       latency throughput energy _stats                                    ← micro-benchmarks
evals/       core/ backends/ datasets/ scorers/ environments/ execution/ trackers ← benchmark harness
mining/      pearl providers (vllm/cpu/apple_mps) + pools                        ← proof-of-work sidecar (niche)
system/      core.py(JarvisSystem) builder.py(SystemBuilder) orchestrator bundles ← wiring/DI
sdk.py       Jarvis (high-level API)   _rust_bridge.py   __init__.py
```

---

## 3. The five primitives + cross-cutting glue

### 3.1 Core foundation (`core/`)
- **`registry.py` — `RegistryBase[T]`** (≈184 LOC). Generic decorator registry with
  **per-subclass isolated storage** (`_registry_entries_{ClassName}`), so registries
  never leak into each other. API: `register(key)` decorator, `register_value`,
  `get`, `create(key, *args)`, `items`, `keys`, `contains`, `clear`. ~15 typed
  subclasses: Model/Engine/Agent/Tool/Memory/Channel/Connector/Speech/TTS/Skill/
  RouterPolicy/Benchmark/Learning/Compression/Miner. **This is the backbone of the
  whole system's extensibility.**
- **`types.py`** — canonical dataclasses: `Role` enum (SYSTEM/USER/ASSISTANT/TOOL),
  `Message` (role, content, name, `tool_calls`, tool_call_id, metadata),
  `Conversation` (sliding-window `max_messages`), `ToolCall` (id, name, JSON
  `arguments`), `ToolResult` (tool_name, content, success, usage, cost_usd,
  latency_seconds, metadata), `ModelSpec`, `TelemetryRecord`, `Quantization` enum,
  `StepType` enum (ROUTE/RETRIEVE/GENERATE/TOOL_CALL/RESPOND), `RoutingContext`,
  `TOKEN_COUNTING_VERSION` (versioned token-count methodology).
- **`events.py` — `EventBus`** (≈199 LOC). Thread-safe synchronous pub/sub;
  `EventType` enum has 40+ types (INFERENCE_*, TOOL_CALL_*, MEMORY_*, AGENT_TURN_*,
  AGENT_TICK_*, CHANNEL_*, SECURITY_*, WORKFLOW_*, SKILL_*, A2A_*, SCHEDULER_*,
  OPERATOR_*, OPTIMIZE_*, FEEDBACK_RECEIVED). `subscribe/publish/history`; module
  singleton `get_event_bus()`. Subscribers called in registration order in the
  publishing thread. **This is the connective tissue** — telemetry, traces, audit,
  analytics all subscribe rather than couple.
- **`config.py`** (≈2,174 LOC — huge). `JarvisConfig` top-level dataclass with 25+
  nested sections; `detect_hardware()` (nvidia-smi/rocm-smi/system_profiler/
  /proc), `recommend_engine()` (decision tree: no-GPU→llamacpp, Apple→ollama/mlx,
  NVIDIA datacenter→vllm, AMD→lemonade/vllm), `recommend_model()` (VRAM tier
  table), `load_config()` (TOML overlay on hardware-derived defaults), back-compat
  properties for migrated keys, `apply_security_profile()`, `validate_config_key()`.
- **`credentials.py`** — TOML credential store at `~/.openjarvis/` with `0o600`
  perms; `load_credentials`, `save_credential`, `inject_credentials()` (loads into
  `os.environ` pre-exec), `TOOL_CREDENTIALS` mapping.

### 3.2 Intelligence (`intelligence/model_catalog.py`)
`BUILTIN_MODELS` is a large declarative list of `ModelSpec` entries (Qwen, Llama,
Granite, GPT-OSS, GLM, Trinity MoE, plus cloud GPT/Claude/Gemini/MiniMax) with
parameter count, context length, active params (MoE), quantization, min VRAM,
supported engines, provider, pricing metadata. `register_builtin_models()`
(idempotent), `merge_discovered_models(engine_key, ids)` (adds runtime-discovered
models with minimal specs). Routing logic **moved out** to `learning/`; only
back-compat shims remain here. `IntelligenceConfig` (in `core/config.py`) holds
the configured model identity: default/fallback model, weight path, checkpoint/
LoRA path, quantization, `preferred_engine` (pins a model to an engine), provider,
and generation defaults (temperature/max_tokens/top_p/top_k/repetition_penalty/
stop). Engine-selection priority: explicit flag → `preferred_engine` →
`engine.default` → first healthy.

### 3.3 Engine (`engine/`)
- **`InferenceEngine` ABC** (`_stubs.py`): `generate()→dict{content,usage,
  tool_calls?,finish_reason}`, async `stream()→AsyncIterator[str]`,
  `stream_full()→StreamChunk{content|tool_calls|finish_reason}`, `list_models()`,
  `health()`, optional `prepare()`. `engine_id`, `is_cloud`. `ResponseFormat`
  supports JSON-mode / JSON-schema.
- **13+ backends**: `ollama` (native API), `_OpenAICompatibleEngine` base for vLLM/
  SGLang/llama.cpp/MLX/LM Studio/Exo/Nexa/Lemonade/Uzu/Apple-FM, `cloud`
  (OpenAI/Anthropic/Google SDKs, provider auto-detected by model name), `litellm`
  (100+ providers), `multi` (`MultiEngine` routes local↔cloud).
- **Tool-call normalization**: each backend converts provider-specific tool-call
  formats (OpenAI/Anthropic content-blocks/Google parts/Ollama) into one flat
  `{id,name,arguments}` shape consumed by agents.
- **`_base.messages_to_dicts()`** converts `Message[]`→OpenAI dicts once;
  `estimate_prompt_tokens()` gives cache-agnostic counts; `EngineConnectionError`
  wraps httpx connect/timeout. `_discovery`: `get_engine`, `discover_engines`
  (health-probe + sort default first), `discover_models`.

### 3.4 Agentic Logic (`agents/`)
- **`BaseAgent` ABC** (`_stubs.py`, ≈377 LOC): `run(input, context, **kw)→
  AgentResult`. Concrete helpers eliminate boilerplate: `_emit_turn_start/end`,
  `_build_messages` (system prompt + conversation + input), `_generate` (engine
  call with stored defaults), `_check_continuation` (handle `finish_reason=
  length`), `_strip_think_tags`, `_apply_persona`, `_max_turns_result`.
  `accepts_tools` class flag drives CLI/SDK tool auto-detection.
- **`ToolUsingAgent`** intermediate base: adds `tools`, `ToolExecutor`,
  `LoopGuard`, `max_turns` (default 10).
- **`AgentContext`** (conversation, tools, memory_results, metadata) /
  **`AgentResult`** (content, tool_results, turns, metadata).
- **9 agents**: `SimpleAgent` (1-shot), `OrchestratorAgent` (multi-turn tool loop;
  two modes — `function_calling` via OpenAI tool schema, and `structured`
  THOUGHT/TOOL/INPUT/FINAL_ANSWER text protocol that doubles as SFT/RL training
  format; supports parallel tool exec via ThreadPoolExecutor), `NativeReActAgent`
  (Thought-Action-Observation), `NativeOpenHandsAgent` (CodeAct: generates+runs
  Python, pre-fetches URLs), `RLMAgent` (recursive LM with persistent REPL +
  `llm_query`/`llm_batch` + `FINAL()`), `OpenHandsAgent` (wraps openhands-sdk),
  `ClaudeCodeAgent` (spawns Node.js runner for `@anthropic-ai/claude-code` SDK via
  sentinel-delimited JSON), plus `OperativeAgent`/`MonitorOperativeAgent`
  (persistent scheduled) and `DeepResearchAgent`.
- **`executor.py` `AgentExecutor`** (≈799 LOC): managed single-tick runner —
  `execute_tick(agent_id)` acquires lock via `manager.start_tick()`, runs with
  up-to-3 retries + exponential backoff, classifies errors (retryable vs fatal),
  collects traces, finalizes via `end_tick()`. Stale ticks (600s no activity)
  auto-overtaken.
- **`manager.py` `AgentManager`**: persistent agent lifecycle in 6 SQLite tables
  (agents, tasks, bindings, checkpoints, messages, learning logs).
- **`loop_guard.py` `LoopGuard`**: 4 strategies — SHA-256 identical-call hash,
  ping-pong A-B-A-B sliding window, per-tool polling budget, context-overflow
  compression (optional Rust backend).
- **`errors.py`**: `classify_error()`, `retry_delay(attempt)`.

### 3.5 Memory (`tools/storage/`, ABCs surfaced as Memory primitive)
- **`MemoryBackend` ABC**: `store(content, source, metadata)→id`, `retrieve(query,
  top_k)→RetrievalResult[]`, `delete`, `clear`. `RetrievalResult{content, score,
  source, metadata}`.
- **5 backends**: `sqlite` (FTS5/BM25, zero-dep default, persistent), `faiss`
  (dense, MiniLM-L6-v2, in-memory), `colbert` (late-interaction MaxSim), `bm25`
  (Okapi), `hybrid` (RRF fusion of sparse+dense, over-fetch ×3, weighted).
- **Pipeline**: `chunking.py` (paragraph-aware, `ChunkConfig` size 512/overlap 64/
  min 50), `ingest.py` (file/dir walk, type detection, skips binaries+hidden+
  vendored), `embeddings.py` (`Embedder` ABC + SentenceTransformer),
  `context.py` `inject_context()` (retrieve→filter by min_score→truncate to
  max_context_tokens→prepend system message with `[Source: …]` attribution).

### 3.6 Learning (`learning/`) + Traces (`traces/`)
- **Traces**: `TraceStore` (append-only SQLite, `traces`+`trace_steps` tables +
  FTS, WAL, `subscribe_to_bus`), `TraceCollector` (wraps any agent, subscribes to
  INFERENCE/TOOL_CALL/MEMORY events, builds `TraceStep[]`, persists `Trace`,
  publishes TRACE_COMPLETE — zero-touch instrumentation), `TraceAnalyzer`
  (`summary`, `per_route_stats`, `per_tool_stats`, `traces_for_query_type`).
- **Routing** (`learning/routing/router.py`): `RouterPolicy` ABC + `QueryAnalyzer`;
  `HeuristicRouter` (6 priority rules: urgency→smallest, code→coder model,
  math/long→largest, short→smallest, default→fallback chain);
  `build_routing_context()` (regex code/math detection, length, urgency).
  `RouterPolicyRegistry` keys: heuristic, learned (`TraceDrivenPolicy`), sft
  (`SFTRouterPolicy`), grpo (stub). `ensure_registered()` lazy re-registration
  survives test `clear()`.
- **`TraceDrivenPolicy`**: classifies query (code/math/short/long/general), scores
  models by 0.6·success + 0.4·feedback over traces, `update_from_traces()` (batch)
  + `observe()` (online, conservative switch).
- **Reward** (`heuristic_reward.py`): `RewardFunction` ABC; weighted
  latency(0.4)/cost(0.3)/efficiency(0.3) → [0,1].
- **Learning policy taxonomy**: `LearningPolicy`→`IntelligenceLearningPolicy`
  (routing) + `AgentLearningPolicy` (behaviour: `AgentAdvisorPolicy`,
  `ICLUpdaterPolicy`).
- **`learning_orchestrator.py`** `LearningOrchestrator.run()`: trace-mine SFT/
  routing/agent pairs (`training/data.py TrainingDataMiner`) → evolve agent
  configs (`agents/agent_evolver.py`) → optimize skills (`agents/skill_optimizer.py`
  DSPy/GEPA → overlay TOML) → optional LoRA → eval-gate.
- **`optimize/`**: `OptimizationEngine` (propose→evaluate→analyze, Pareto frontier
  over accuracy/latency/cost/energy), `LLMOptimizer`, `OptimizationStore` (SQLite),
  `feedback/judge.py TraceJudge` (LLM-as-judge scoring).
- **`spec_search/`** (the standout) — **frontier-teacher improves the local
  student's whole harness, not weights**. 4-phase `Trigger→Diagnose→Plan→Execute→
  Gate→Record`. `SpecSearchOrchestrator` drives: `DiagnosisRunner`+`TeacherAgent`
  (frontier model re-runs student+self on benchmarks, finds 2-5 failure clusters)
  → `LearningPlanner` (structured-output → typed `Edit[]` from a fixed op set:
  SET_MODEL_FOR_QUERY_CLASS, PATCH/REPLACE_SYSTEM_PROMPT, ADD/REMOVE_TOOL,
  EDIT_TOOL_DESCRIPTION, SET_AGENT_CLASS, …; **deterministic risk-tier lookup**
  auto/review/manual — teacher cannot self-assign tier) → `execute/loop.py`
  (route by `AutonomyMode` AUTO/TIERED/MANUAL, dispatch to per-pillar
  `EditApplier`s) → `gate/benchmark_gate.py` (re-run benchmark, accept/rollback) →
  `CheckpointStore` (git-backed config rollback) + `SessionStore`. **Directly
  relevant to HiveOS `self_mod` + `approval_gate`.**

### 3.7 System wiring (`system/`)
- **`JarvisSystem`** (`core.py`) — single-source-of-truth dataclass with ~50
  fields (config, bus, engine, model, agent, tools, tool_executor, memory_backend,
  router, telemetry_store, trace_store/collector, scheduler, container_runner,
  workflow_engine, session_store, capability_policy, audit_logger, boundary_guard,
  operator_manager, agent_manager/scheduler/executor, speech_backend,
  skill_manager, learning_orchestrator, mcp_clients). Grouped properties:
  `security`, `observability`, `agents`, `scheduling`. `ask()` → lazy
  `QueryOrchestrator`.
- **`SystemBuilder`** (`builder.py`) — fluent, config-driven DI:
  `.engine(k).model(m).agent(a).tools([...]).build()` resolves from registries,
  instantiates+injects all subsystems, returns wired `JarvisSystem`. **Clean DI
  without a container.**
- **`QueryOrchestrator`** (`orchestrator.py`) — `ask(query, context, agent, …)`:
  inject memory context → detect/resolve agent → run loop or direct engine call.

---

## 4. Query flow (end-to-end, from `query-flow.md` + `sdk.py`)

```
ask(query) → load_config (hardware) → get_engine (preferred→default→first healthy)
 → discover+merge models → router.select_model(RoutingContext) [if model unset]
 → [agent mode] AgentContext built → memory inject_context (top_k=5, attribution)
     → agent.run: loop ≤max_turns { engine.generate(messages, tools)
         → tool_calls? execute via ToolExecutor (parallel-capable, loop-guarded),
            append TOOL messages, continue : else strip <think>, return }
   [direct mode] instrumented_generate(engine, messages, model)
 → TelemetryStore records (latency/tokens/energy/cost); TraceCollector saves Trace
 → response to caller (CLI stdout / SDK return / SSE stream)
```
Engine wrapping stack (composable, all implement `InferenceEngine`):
`MultiEngine( InstrumentedEngine( GuardrailsEngine( OllamaEngine ) ) )`.

---

## 5. I/O surfaces & integration

- **Tools** (`tools/_stubs.py`): `BaseTool`(spec→`ToolSpec`, `execute`→`ToolResult`,
  `to_openai_function`); `ToolExecutor` dispatch = lookup→JSON-parse args→optional
  boundary-guard redaction→RBAC capability check→taint check→confirmation→
  TOOL_CALL_START→execute with timeout (default 30s via ThreadPoolExecutor)→
  taint-detect result→TOOL_CALL_END. ~39 built-in tools (calculator, think,
  retrieval, llm, file_read/write, shell_exec, code_interpreter[+docker], browser
  [Playwright], http_request, git, db_query, web_search, pdf, image, audio, tts,
  memory_*, skill_manage, apply_patch, approval_store, …).
- **MCP** (`mcp/`): `MCPServer` exposes OpenJarvis tools (tools/list, tools/call,
  readOnly/destructive hints); `MCPClient` discovers+calls remote tools;
  `MCPTransport` ABC → InProcess/Stdio/StreamableHTTP (session-id + bearer);
  `tools/mcp_adapter.py MCPToolAdapter` wraps a remote MCP tool as a native
  `BaseTool`. Default tool timeout 600s.
- **A2A** (`a2a/`): Google A2A spec over JSON-RPC; `AgentCard` served at
  `/.well-known/agent.json`; `A2ATask` state machine SUBMITTED→WORKING→COMPLETED/
  FAILED; `A2AServer` (tasks/send|get|cancel, constant-time bearer auth);
  `a2a/tool.py` wraps remote agent as a tool.
- **Connectors** (`connectors/`): `BaseConnector` (`is_connected`, `disconnect`,
  `sync(since, cursor)→Document[]`, `sync_status`, optional `auth_url`/
  `handle_callback` OAuth, optional `mcp_tools`); universal `Document` schema;
  ~30 connectors (Gmail, GDrive, Notion, Slack, Outlook, Obsidian, Apple
  Notes/Health/Music/Contacts, Oura, Strava, Spotify, GitHub, RSS, …).
- **Channels** (`channels/`): `BaseChannel` (connect/disconnect/send/status/
  list_channels/on_message); background daemon-thread listener; ~30 platforms
  (Telegram, Discord, Slack, WhatsApp[Baileys Node bridge + Twilio], Signal,
  Matrix, iMessage, SMS, email, Teams, …). **Subprocess-bridge pattern** (Python
  ↔ Node JSON-line over stdio) for non-Python protocols.
- **Speech** (`speech/`): `TTSBackend`/`STTBackend` ABCs; OpenAI/Cartesia/Kokoro
  TTS; faster-whisper/OpenAI/Deepgram STT.
- **Server** (`server/`): FastAPI OpenAI-compatible `/v1/chat/completions`
  (streaming SSE + tool dispatch), `/v1/models`, `/v1/channels`, `/v1/savings`,
  `/v1/security/scan`, `/health`; `AuthMiddleware` (bearer), security-header
  middleware, `ChannelBridge`, webhook routes. **Key design**: if client passes
  explicit `tools=`, skip the agent and return raw model tool_calls (don't silently
  re-run the tool loop).
- **CLI** (`cli/`): 52 Click commands (serve, chat, ask, agent, daemon, scheduler,
  channel(s), memory, skill, tool, digest, mine, eval, vault, config, doctor,
  telemetry, auth, gateway, init, quickstart, registry dump, optimize, workflow,
  connect, operators, scan, …) + a Textual TUI dashboard. `doctor --fix` migrates
  config/state.

## 6. Operational subsystems

- **Sessions** (`sessions/session.py`): `SessionIdentity` (one user ↔ many channel
  ids), `SessionStore` (SQLite, `get_or_create`, consolidation by message-count,
  decay by `max_age_hours`, `compression.py` summarizes old turns).
- **Scheduler** (`scheduler/`): `TaskScheduler` background polling daemon (cron/
  interval/once), `SchedulerStore` (SQLite), publishes SCHEDULER_TASK_*.
- **Operators** (`operators/`): `OperatorManifest` (TOML: tools, system_prompt,
  schedule, capabilities, metrics) → `OperatorManager` (register/discover/activate/
  pause/resume/run_once) creates scheduler task `operator:{id}` that ticks via
  `system.ask(agent="operative", …)`.
- **Workflow** (`workflow/`): `WorkflowGraph` (nodes AGENT/TOOL/CONDITION/LOOP,
  `execution_stages()` topological sort) + `WorkflowEngine` (sequential stages,
  parallel within stage via ThreadPoolExecutor, context propagation).
- **Telemetry** (`telemetry/`): `InstrumentedEngine` wrapper publishes INFERENCE_*
  + TELEMETRY_RECORD; `TelemetryStore` (SQLite, schema migration loop, token-count
  versioning, double-instrument guard); multi-vendor `EnergyMonitor` (NVIDIA/AMD/
  Apple/RAPL) via `sample()` context manager; ITL percentiles, FLOPs, IPJ/IPW/MFU
  efficiency KPIs.
- **Security** (`security/`): `GuardrailsEngine` wraps any engine (scan input +
  output; modes WARN/REDACT/BLOCK); `BaseScanner`→SecretScanner/PIIScanner/
  InjectionScanner; `AuditLogger` (SQLite, subscribes to SECURITY_* events);
  `CapabilityPolicy` (RBAC per agent/tool); `file_policy.is_sensitive_file`,
  `ssrf.check_ssrf`, `taint`, `signing` (Ed25519), `rate_limiter`,
  `subprocess_sandbox`. **Sandbox** (`sandbox/`): `ContainerRunner` (Docker/Podman,
  `--network none`, mount allowlist, sentinel JSON I/O, orphan cleanup),
  `SandboxedAgent` wrapper, `wasm_runner` (wasmtime).
- **Analytics** (`analytics/`): opt-in PostHog via `EventBridge` (EventBus→PostHog),
  anonymized, never blocks startup.

## 7. Rust + desktop (Tier C)
17-crate Rust workspace (`rust/crates`, MSRV 1.88) mirroring the Python primitives
(`openjarvis-core/engine/agents/tools/security/telemetry/learning/traces/sessions/
scheduler/workflow/skills/recipes/templates/a2a/mcp`) + `openjarvis-python` PyO3
cdylib bindings (used by `_rust_bridge.py`, e.g. LoopGuard, OptimizationStore).
`frontend/` + `desktop/` = React 19 + Tauri 2 (Rust IPC: get/set_config,
start/stop_server, notifications), shadcn/Tailwind/Zustand/recharts.

---

## 8. Key patterns worth internalizing (the OpenJarvis "way")

1. **ABC + decorator registry + lazy import** — every extension point. `__init__.py`
   try/except-imports all built-ins so missing optional deps degrade gracefully.
2. **EventBus as the only coupling point** — telemetry/traces/audit/analytics
   subscribe; producers never import consumers.
3. **Composable engine wrappers** — Guardrails/Instrumented/Multi all implement
   `InferenceEngine` and nest arbitrarily. One concern per wrapper.
4. **Single canonical `Message`/`ToolCall` type**, normalized at the engine edge.
5. **Config-driven DI via SystemBuilder** — no globals (except bus singleton).
6. **SQLite-first persistence** for sessions/tasks/traces/telemetry/audit (WAL,
   `check_same_thread=False`, migration loops).
7. **Hardware-aware defaults** — detect → recommend engine+model.
8. **Trace-driven learning loop** — traces feed routing/skill/agent improvement.
9. **Three-tier param resolution** — explicit arg > config > class default > literal.
10. **Risk-tiered, gated self-improvement** — deterministic tiers + benchmark gate +
    git rollback (spec_search). **Maps onto HiveOS self_mod + approval_gate.**

---

## 9. REUSE-READY (Python — port into a Python-first HiveOS)

Confidence/effort and concrete source paths. "Direct" = lift with import renames.

| # | Component | Source path(s) | Reuse | Note |
|---|-----------|----------------|-------|------|
| 1 | Registry | `core/registry.py` | Direct/Low | Zero-dep `RegistryBase[T]`; basis for all HiveOS plugins |
| 2 | Canonical types | `core/types.py` | Direct/Low | Message/Conversation/ToolCall/ToolResult/ModelSpec |
| 3 | EventBus | `core/events.py` | Direct/Low | Thread-safe pub/sub + event taxonomy |
| 4 | Credentials store | `core/credentials.py` | Direct/Low | 0o600 TOML + `inject_credentials()` |
| 5 | Hardware detect + recommend | `core/config.py` (functions only) | Adapt/Med | Keep detection+tiering; drop OpenJarvis-specific JarvisConfig |
| 6 | InferenceEngine ABC + OpenAI-compat base | `engine/_stubs.py`, `engine/_base.py`, `engine/_openai_compat.py`, `engine/_discovery.py` | Direct/Low | Implement HiveOS engines (incl. MiniMax via OpenAI-compat/Anthropic endpoint) |
| 7 | Agent base classes | `agents/_stubs.py` | Direct/Low | BaseAgent + ToolUsingAgent + AgentContext/Result |
| 8 | OrchestratorAgent | `agents/orchestrator.py` | Adapt/Med | Multi-turn tool loop (function_calling mode) |
| 9 | LoopGuard | `agents/loop_guard.py` | Direct/Low | Pure-Python fallback; stops degenerate loops |
| 10 | Executor lifecycle | `agents/executor.py`, `agents/manager.py`, `agents/errors.py` | Adapt/Med | Tick lock + retries + error classify (for managed/scheduled agents) |
| 11 | Tool dispatch | `tools/_stubs.py` | Direct/Low | BaseTool/ToolExecutor/ToolSpec; RBAC/taint injectable |
| 12 | MCP client/server/transport + adapter | `mcp/*.py`, `tools/mcp_adapter.py` | Direct/Low | Pure JSON-RPC; gives HiveOS instant MCP interop |
| 13 | Memory backends + pipeline | `tools/storage/{_stubs,sqlite,chunking,ingest,context,embeddings}.py` | Direct/Adapt/Low | SQLite/FTS default; swappable; **cross-check vs Mnemosyne (Phase 4) before adopting** |
| 14 | Trace store/collector/analyzer | `traces/*.py` | Adapt/Med | Zero-touch instrumentation via events |
| 15 | Telemetry | `telemetry/instrumented_engine.py`, `telemetry/store.py` | Adapt/Low | Wrap-to-instrument; energy optional |
| 16 | Sessions | `sessions/session.py` | Adapt/Low | Cross-channel identity + decay/consolidation |
| 17 | Scheduler | `scheduler/scheduler.py`, `scheduler/store.py` | Adapt/Low | cron/interval/once + events |
| 18 | Security guardrails + scanners | `security/{guardrails,scanner,audit,_stubs}.py` | Adapt/Low | Composable wrapper + redaction modes (complements HiveOS approval_gate) |
| 19 | Sandbox | `sandbox/runner.py`, `sandbox/mount_security.py` | Adapt/High | Container isolation for self_mod/tools (needs Docker) |
| 20 | Prompt builder | `prompt/builder.py` | Adapt/Med | Frozen-prefix prompt-cache + persona SOUL/MEMORY/USER loading — **fits HiveOS SOUL.md** |
| 21 | Recipes (unified TOML) | `recipes/loader.py`, `recipes/composer.py` | Adapt/Med | One config → SDK / eval / operator |
| 22 | Workflow DAG | `workflow/engine.py`, `workflow/graph.py` | Direct/Low | Topo-sort + parallel stages |
| 23 | Server (OpenAI-compat) | `server/routes.py`, `server/app.py`, `server/auth_middleware.py`, `server/middleware.py`, `server/session_store.py` | Adapt/Med | Gateway for HiveOS; bearer auth + SSE |
| 24 | System wiring | `system/builder.py`, `system/core.py`, `system/orchestrator.py` | Adapt/Med | Slim JarvisSystem → HiveCoreContext; SystemBuilder DI |
| 25 | Channels base + bridge pattern | `channels/_stubs.py`, `channels/telegram.py`, whatsapp bridge | Adapt/Med | HiveOS needs Telegram/voice surfaces |
| 26 | Connectors base | `connectors/_stubs.py` (+ obsidian ref) | Adapt/Med | Universal Document + cursor sync |
| 27 | A2A | `a2a/*.py` | Direct/Low | Agent-to-agent + discovery card |
| 28 | Bench stats | `bench/_stats.py` | Direct/Low | percentile/mean/p50/p95 |

## 10. ADAPT-AS-PATTERN (copy the design, not necessarily the code)

- **spec_search self-improvement** (`learning/spec_search/*`) → blueprint for
  HiveOS `self_mod`: frontier teacher (Opus/ChatGPT-Plus planner) diagnoses →
  proposes typed edits → deterministic risk tier → `approval_gate` for review-tier
  → benchmark gate → git rollback. **The single highest-value pattern for HiveOS.**
- **Learning loop** (traces → mine → evolve → eval-gate) for self-tuning routing/
  skills.
- **Multi-objective Pareto optimization** (`learning/optimize`) for cost/energy/
  accuracy config search.
- **Structured-mode agent = trainable format** (`orchestrator.py`): one agent type
  usable at inference and as SFT/RL data.
- **Tauri desktop shell** as a model for a HiveOS dashboard (vs the current
  Vite/React `Dashboard/`).
- **Rust-mirror + PyO3** for hot paths (loop guard, optimization) — defer.

## 11. What to NOT take

- **Mining/Pearl** (`mining/*`) — proof-of-work sidecar, irrelevant to HiveOS.
- **Full evals harness** (`evals/*`, 80+ datasets/scorers) — research-scale; adopt
  only `evals/core/types.py` metric shapes + `bench/_stats.py` if/when needed.
- **30+ channels / 30+ connectors wholesale** — take the ABCs + 1-2 surfaces Kamil
  actually uses (Telegram, Obsidian), not the long tail.
- **PostHog analytics** — HiveOS is single-user/private; skip external telemetry.
- **Heavy memory backends** (FAISS/ColBERT) — likely superseded by Mnemosyne
  (decide in Phase 6 after Phase 4).

## 12. Relevance to current HiveOS (preview of Phase 5/6)
HiveOS already names: `core/model_router.py`, `core/orchestrator.py`,
`core/planner.py`, `core/budgeter.py`, `core/self_mod.py`, `core/approval_gate.py`,
`gateway/app.py`, `memory/{brain,memory_keeper,mnemosyne}.py`, `tools/{registry,
discovery}.py`. OpenJarvis offers production-grade, battle-tested versions of
nearly every one: registry+EventBus+types (foundation HiveOS lacks), engine
abstraction + MiniMax-ready OpenAI-compat base (`model_router`), QueryOrchestrator
(`orchestrator`), HeuristicRouter/TraceDrivenPolicy (`model_router`/`planner`),
telemetry cost recording (`budgeter`), spec_search (`self_mod`), GuardrailsEngine
(complements `approval_gate`), FastAPI server (`gateway`), ToolExecutor+registry
(`tools`). The Phase-6 synthesis should treat OpenJarvis's `core/` + `engine/` +
`agents/` + `system/` as the **primary skeleton donor** for a Python-first HiveOS,
with Mnemosyne as the memory layer and hermes/openclaw for specific patterns.
