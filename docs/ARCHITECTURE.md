# HiveOS — Architecture (authoritative, current system)

> **This documents the system as actually built** (the installable `hive` package),
> with every claim citing a real `src/hive/...` path. Companions:
> `docs/STATUS.md` (what's done / gaps) and `docs/reference/HIVEOS_COMPONENTS.md`
> (per-module table). The original *plan* lives in `docs/references/SYNTHESIS.md`
> (historical). Part II below keeps the design **rationale** (the "why").

HiveOS is the system; **Hive** is the agent. Python-first, async, installable as `hive`.

---

# Part I — The built system

## 1. Identity & safety spine (never bypass)
- `Config/SOUL.md` — immutable identity + safety contract. Loaded read-only via
  `src/hive/core/soul.py` (lazy, PEP 562). **Never edited/moved.**
- `Core/approval_gate.py` — the danger firewall. Reached read-only via an `importlib`
  bridge in `src/hive/core/approval.py` (re-exports `gate`, `PROTECTED_PATHS`,
  `DANGEROUS_TOOLS`). **Never edited/moved.**
- Both are PROTECTED: `core/self_mod.py::_touches_protected` refuses any change touching
  them; the tool executor routes dangerous calls through the gate; Hive never merges to
  `main` (humans do).

## 2. Package layout (`src/hive/`)
```
core/    registry events types config doctor credentials soul approval
         self_mod spec_search budgeter sandbox          # leaf layer
llm/     router failover credential_pool model_catalog pricing rate_limit
         sanitize  adapters/{base,minimax,anthropic,codex}  # make_adapter registry
agents/  base orchestrator executor loop_guard delegate planner
memory/  provider mnemosyne_provider local keeper vault curator skill_usage
context/ session_store compaction prompt_builder
tools/   base registry executor file_safety discovery builtins  mcp/{client,server}
gateway/ app protocol auth  channels/{base,telegram}
autonomy/heartbeat cron tasks commitments
surfaces/cli voice
observability/ telemetry traces audit
runtime.py   # HiveOS dataclass + HiveOS.build() — composition root
```

## 3. Dependency DAG (enforced)
`core` is a **leaf** (imports nothing higher). `llm`/`memory`/`tools`/`context` →
`core`. `agents` → `core`+`llm`+`tools`+`memory`+`context`. `gateway`/`autonomy`/
`surfaces` → `agents`+`runtime`. `observability` subscribes to the EventBus only.
The composition root is `runtime.py` (top level, **not** in `core`, because it imports
every layer). Enforced by `tests/test_architecture.py`:
- a subprocess probe asserts `hive.core.*` (and memory/context/tools/observability)
  import no higher layer at import time;
- a **static AST scan** asserts no `hive.core/*` file imports a higher layer **even in a
  function-local import** (this caught a real `core→llm` leak once).
Consequence: cross-layer needs are **injected** (e.g. `memory.keeper` takes a
`Summarizer` callable; it never imports `llm`).

## 4. Composition root — `runtime.py`
`HiveOS.build(config=None, router=None)` constructs and wires every subsystem from a
frozen `HiveConfig`, then returns a `HiveOS` dataclass holding them. Inject `router`
to run fully offline (all tests do). Wiring highlights:
- EventBus created per build (no cross-talk); budgeter, telemetry, traces subscribe.
- Router = `ModelRouter(adapter=MiniMaxAdapter, credential_pool, budget=budgeter.gate)`.
- Memory = `build_mnemosyne_provider(...)` **or** `LocalMemoryProvider` fallback.
- Tools = `register_builtins(_Registry, memory, github_token)` (incl. the discovery-first
  `discover` tool); `ToolExecutor(tools, audit=audit_log.record)`. MCP servers from
  `HIVE_MCP_SERVERS` are loaded into the registry at gateway startup
  (`HiveOS.load_mcp_servers`). Credential pool seeded from the 0o600 vault
  (`credentials.inject`) + comma-split multi-key.
- Self-improvement = `SelfModifier(open_pr=github_pr_opener?, run=sandbox_run)` +
  `SelfImprovement`; skill lifecycle = `SkillUsageStore` + `Curator`.
- Autonomy = `TaskBoard` + `CronScheduler` + `CommitmentBook` (shared state DB).
- `HiveOS` methods: `ask`, `ask_stream`, `consolidate`, `curate`, `self_improve`, `aclose`.

## 5. Data model (SQLite-first; no JSON sidecars for runtime state)
| Store (file) | Tables | DB |
|---|---|---|
| `context/session_store.py` | `sessions`, `messages` (+ `messages_fts`) | shared `state_db` |
| `memory/local.py` | `episodic`, `knowledge` (+ `knowledge_fts`) | shared `state_db` |
| `memory/skill_usage.py` | `skill_usage` | shared `state_db` |
| `autonomy/tasks.py` | `hive_tasks` | shared `state_db` |
| `autonomy/cron.py` | `hive_cron` | shared `state_db` |
| `autonomy/commitments.py` | `hive_commitments` | shared `state_db` |
| `observability/audit.py` | `audit_log` | `data_dir/audit.sqlite` |
| Mnemosyne (when installed) | its own schema | `mnemosyne_home` |
Each store self-initializes its schema (WAL). `core/doctor.py` verifies the DB is
present/openable; it does **not** duplicate store DDL (avoids drift — fixed in #14).
Named file artifacts (allowed): Obsidian vault notes (`memory/vault.py`), curator
backups (`data/backups/skills`), the 0o600 credential vault (`core/credentials.py`).

## 6. EventBus (`core/events.py`)
Thread-safe synchronous pub/sub; subscribers run in registration order, isolated from
each other's exceptions. **Contract: subscribers must be fast/non-blocking.** Producers
never call observability directly. Event types: `INFERENCE_{START,END}`,
`TOOL_CALL_{START,END}`, `MEMORY_{STORE,RETRIEVE}`, `AGENT_{TURN,TICK}_{START,END}`,
`APPROVAL_{REQUESTED,RESOLVED}`, `TELEMETRY_RECORD`, `BUDGET_BLOCK`, `SELFMOD_{START,END}`.
`INFERENCE_END` carries `{model, input_tokens, output_tokens, cost_usd}` → budgeter
(cost accumulator) + telemetry.

## 7. Model routing & resilience (`llm/`)
`ModelRouter.complete(kind=EXECUTE|AUX|PLAN)`: PLAN → Codex planner (subprocess, hardened:
stdin + timeout + fallback to executor); else the executor model chain (exec →
exec_fallback) through one decision tree: `failover.classify` → retry (jittered backoff)
/ rotate credential / fall back to next model / abort, gated by `budgeter.gate`.
`MiniMaxAdapter` speaks the Anthropic Messages API (interleaved thinking, prompt-cache
`cache_control`, message sanitization, x-ratelimit capture). `router.stream` yields SSE
deltas. Cost is computed in the router (`llm/pricing`) and emitted on the event — `core`
never imports pricing. **Providers are pluggable** (`llm/adapters.make_adapter`): the
executor is `minimax` or `anthropic` (same Anthropic wire) via `HIVE_EXEC_PROVIDER`;
`codex` is the planner (subprocess behind the same `LLMAdapter` contract).

## 8. Agent turn & autonomy
- **Turn** (`agents/orchestrator.py::ConversationOrchestrator.ask`): restore/build the
  prefix-cached system prompt (`context/prompt_builder`) + memory prefetch → loop ≤N:
  `router.complete(tools)`; tool_calls → loop-guard (`agents/loop_guard`) → gate-routed
  `tools/executor` → append results; else final. Post-turn: persist to session store +
  `memory.sync_turn`. Subagents via `agents/delegate` are **leaves** (can't nest).
- **Heartbeat** (`autonomy/heartbeat.py`): each tick fires due cron + commitments onto
  the durable `TaskBoard`; if nothing due, plan 1–3 tasks; claim + dispatch (bounded
  concurrency, mark done/failed); then `consolidate` (keeper) + `curate` (Curator) +
  budget refresh. Queued work survives restart (SQLite board).

## 9. Self-improvement (`core/spec_search.py` + `core/self_mod.py`)
A typed `Edit` gets a `RiskTier` from a **deterministic table** (model can't self-escalate):
AUTO → `SelfModifier.propose` (isolated worktree → test → push → draft PR via GitHub REST;
never merges, refuses PROTECTED files); REVIEW → human approval via the gate; MANUAL →
recorded only. Optional Docker sandbox (`core/sandbox.py`) runs candidate tests isolated.
`Curator` (`memory/curator.py`) ages agent-created skills active→stale→archived
(never-delete, pinned-exempt, pre-run backup).

## 10. Surfaces & config
- **Gateway** (`gateway/app.py`, FastAPI): `/health`, `/chat`, `/chat/stream` (SSE),
  `/ws`, `/budget`, `/approvals`(+`/decide`), `/telegram/webhook`. Constant-time bearer
  auth (`gateway/auth.py`); typed Pydantic boundary (`gateway/protocol.py`) carrying a
  `PROTOCOL_VERSION` on every response + `/health` (additive-first); transport-only
  channels (`gateway/channels/`).
- **Hardening (M7):** secrets are masked by `core/redact.py` before hitting the audit
  trail/logs; tools self-report `available()` (unavailable ones are hidden from the model
  and refused by the executor); sessions get an out-of-band aux-model title
  (`HiveOS.title_session` / `context/title.py`).
- **CLI** (`surfaces/cli.py`): `hive {chat|ask|serve|heartbeat|consolidate|doctor}`.
- **Config** (`core/config.py`): frozen `HiveConfig.from_env()`, no import-time side
  effects. Env surface: MiniMax (`MINIMAX_API_KEY`, `*_BASE`, `HIVE_EXEC_MODEL`,
  `HIVE_EXEC_FALLBACK_MODEL`, `HIVE_AUX_MODEL`, `HIVE_REMAINS_URL`), planner
  (`HIVE_PLANNER_*`), budgeter (`HIVE_DAILY_CALL_CAP`, `HIVE_WINDOW_WARN_PCT`), gateway
  (`HIVE_HOST/PORT/SECRET`), memory (`MNEMOSYNE_HOME`, `MNEMOSYNE_MCP_URL`,
  `OBSIDIAN_VAULT_PATH`), autonomy (`HIVE_HEARTBEAT_SEC`, `HIVE_MAX_AGENTS`), GitHub
  (`HIVE_GITHUB_*`), Telegram (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`), sandbox
  (`HIVE_SANDBOX_IMAGE`), MCP (`HIVE_MCP_SERVERS`). Pricing overrides via
  `HIVE_PRICE_<MODEL>_{IN,OUT}`. Secrets may live in the 0o600 vault (`credentials.save`).
- **Deploy** (`deploy/`): systemd `hiveos-gateway` (`hive serve`), `hiveos-orchestrator`
  (`hive heartbeat`), `hiveos-keeper.{service,timer}` (`hive consolidate`), hardened
  (`ProtectSystem=strict`, non-root). See `deploy/README.md`.

## 11. Tests
`pytest` (210+); architecture DAG test; opt-in live smokes (`HIVE_LIVE_TEST=1`). CI
(`.github/workflows/ci.yml`) runs compile + pytest on 3.11/3.12.

---

# Part II — Design rationale (the "why")

## Execution runner: MiniMax (Token Plan)
Anthropic-compatible endpoint (`/anthropic`) for native interleaved thinking; model
strings pinned in `.env` (M2→M3 churn is one line). Token Plan is credit-based (rolling
windows) — the budgeter self-calibrates from `GET /v1/token_plan/remains` + a local daily
cap, never a hardcoded call count. PAYG overflow ~ $0.30/M in, $1.20/M out (M2).

## Planner/executor split
Big model plans, cheap model executes. ChatGPT Plus via Codex OAuth (`codex exec`) is the
planner — **thinking only, never execution**; MiniMax does the work. Route only
novel/high-stakes/gap work to the planner.

## Memory brain
Active layer = **Mnemosyne** (SQLite vec+FTS5, banks, hybrid search, `sleep`/`evolve`
consolidation); HiveOS ships a local SQLite fallback so it works before Mnemosyne is
wired. Long-term = **Obsidian vault** (markdown), the durable linkable "old memories".
The memory-keeper (cheap model) reflects → extracts → dedupes → promotes → prunes:
once learned, never re-researched.

## Self-improvement & safety core
Voyager (skill library) + Darwin-Gödel (self-edits with archive + sandbox + human
oversight) + Reflexion (write failures to memory, retry). Every self-mod runs in an
isolated git worktree, snapshots last-known-good, tests, and on success opens a PR
(never merges). SOUL.md + approval gate are human-only. **The human-merge gate is what
makes a self-modifying agent safe — never remove it.**

## Discovery-first reuse
Before building, search official sources (Anthropic Skills, MCP Registry,
modelcontextprotocol/servers, marketplaces, GitHub); **mandatory safety audit** before
adoption; pin versions; sandbox before granting credentials. Treat untrusted repo content
as hostile.

## Multi-agent, GitHub identity, 24/7, voice, language, tri-tool
Orchestrator-worker with leaf subagents (tool-restricted, can't nest, concurrency-capped).
Hive's own GitHub account (App or fine-grained PAT, no merge to main). systemd 24/7 on
Hetzner (Restart=always, non-root) + nightly consolidation timer. Voice (later):
openWakeWord + faster-whisper + Piper via Wyoming. Polish to Kamil / English in code.
`CLAUDE.md`+`AGENTS.md`+`.claude/` keep all three build tools self-verifying.

## Caveats
MiniMax names/plan change — verify the live console. MCP/skill supply-chain risk is real —
the audit step is mandatory. ChatGPT-Plus-via-OAuth has server-side limits. Self-modifying
agents are inherently risky; the human-merge gate is the safeguard.
