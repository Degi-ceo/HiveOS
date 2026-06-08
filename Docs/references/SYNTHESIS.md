# SYNTHESIS — master architecture plan for HiveOS

> The blueprint that turns the four reference audits + the HiveOS self-audit into a
> single, coherent, **Python-first** re-architecture. Read order:
> `OPENJARVIS_REFERENCE.md` (the skeleton donor) · `HERMES_REFERENCE.md` (runtime
> resilience + self-improvement) · `MNEMOSYNE_REFERENCE.md` (memory, near-drop-in) ·
> `OPENCLAW_REFERENCE.md` (architectural rulebook + protocol/algorithm specs) ·
> `HIVEOS_AUDIT.md` (what we have). This file = **Part A** target architecture,
> **Part B** component-sourcing table, **Part C** phased build plan, **Part D**
> sequencing.

## Guiding decisions (confirmed with Kamil)
- **Python-first.** Reuse Python directly from Mnemosyne / Hermes / OpenJarvis;
  mine OpenClaw (TS) for patterns/specs only.
- **Consume, don't compete.** HiveOS is 1.3k LOC; the references solve in mature
  Python nearly everything HiveOS needs. The rebuild is mostly *wiring + thin
  HiveOS-specific glue*, not new engines.
- **Three donors, one rulebook:** OpenJarvis = primitive skeleton (registry +
  EventBus + types + engine ABC + system builder); Hermes = runtime resilience
  (failover, credential pool, compaction, curator, cron) + memory-provider ABC;
  Mnemosyne = the memory engine; OpenClaw = the design rules (plugin boundary,
  SQLite-first + doctor migrations, transport-only typed channels, gateway protocol
  versioning, single-slot memory contract).
- **Preserve the HiveOS DNA:** SOUL.md, approval_gate, self_mod safe-flow,
  discovery-first, planner/executor split.

## Hard constraints (hold through Phases 7–8)
1. `Config/SOUL.md` and `Core/approval_gate.py` are **never edited, moved, or
   renamed** — verbatim, in place. The new package *references* them (mechanism in
   Part A.4). Moving them later is a separate, Kamil-approved step.
2. No PR / no merge to `main`. Work stays on `claude/hiveos-deep-audit-JxukC`.
3. Old code stays until its replacement is in place and verified.

---

# PART A — Target architecture

## A.1 Stack & conventions
- **Language:** Python 3.11+ (Hermes/Mnemosyne baseline; 3.11 for `tomllib`,
  `asyncio.TaskGroup`). **Async-first** core (OpenJarvis/OpenClaw are async; HiveOS
  gateway already is) with sync bridges where libraries are sync (Hermes
  `_run_async` pattern).
- **Packaging:** a real installable package under **`src/hiveos/`** with
  `pyproject.toml` (hatchling + uv, per OpenJarvis design-principles §6/§8). This
  **fixes the case-mismatch showstopper by construction** — lowercase package,
  importable on Linux, `python -m hiveos.*` works.
- **Storage:** **SQLite-first** (OpenClaw rule; Mnemosyne/Hermes both SQLite). One
  state DB + Mnemosyne's memory DB. No JSON sidecars for runtime state. Schema
  changes get a `hive doctor --fix` migration (OpenClaw doctor pattern).
- **Config:** canonical-only runtime reads; env + typed schema; doctor migrates old
  shapes (OpenClaw VISION). Actually load `.env`.
- **Extensibility:** decorator **registry** + **EventBus** as the spine
  (OpenJarvis); a narrow **plugin/provider boundary** (OpenClaw) so providers,
  channels, tools, memory are pluggable without touching core.
- **Identity/safety untouched:** SOUL + approval_gate + self_mod stay the spine.

## A.2 Target directory tree
```
hiveos/                              # repo root
  pyproject.toml                     # NEW: packaging, deps, entry points, tool config
  .env.example                       # NEW: documented env (fixes missing file)
  README.md  AGENTS.md  CLAUDE.md
  Config/                            # PROTECTED location — unchanged
    SOUL.md                          #   ← never moved/edited
    goals.json                       #   (may evolve into a recipe later)
  Core/                              # PROTECTED location — unchanged
    approval_gate.py                 #   ← never moved/edited (canonical gate)
  src/hiveos/
    __init__.py                      # version, lazy facade
    core/
      registry.py                    # RegistryBase[T]  (OpenJarvis)
      events.py                      # EventBus + EventType  (OpenJarvis)
      types.py                       # Message/Conversation/ToolCall/ToolResult/ModelSpec
      config.py                      # settings + schema + .env load  (OpenClaw doctor)
      doctor.py                      # `hive doctor --fix` migrations  (OpenClaw)
      credentials.py                 # 0o600 cred store + env inject  (OpenJarvis)
      approval.py                    # thin re-export/bridge to Core/approval_gate.py (A.4)
      soul.py                        # loads Config/SOUL.md verbatim (A.4)
      self_mod.py                    # KEEP+extend: worktree/PR flow + spec_search + curator
      system.py                      # HiveSystem dataclass + SystemBuilder DI (OpenJarvis)
    llm/
      router.py                      # KEEP shape: planner/executor split + resilience
      failover.py                    # FailoverReason taxonomy + decision tree (Hermes)
      credential_pool.py             # multi-key failover + cooldown (Hermes)
      model_catalog.py               # per-model compat config (OpenClaw spec)
      adapters/
        minimax.py                   # MiniMax Anthropic endpoint (from current router)
        anthropic.py  codex.py       # planner (Codex) + extra exec providers (Hermes)
    agents/
      base.py                        # BaseAgent / ToolUsingAgent + Context/Result (OpenJarvis)
      orchestrator.py                # QueryOrchestrator turn loop (OpenJarvis+Hermes)
      executor.py                    # tick lifecycle, retries, terminal-outcome (OpenJarvis/OpenClaw)
      loop_guard.py                  # degenerate-loop detection (OpenJarvis/Hermes)
      delegate.py                    # isolated parallel subagents (Hermes delegate_task)
      planner.py                     # KEEP: goals+state -> task list
    memory/
      provider.py                    # MemoryProvider ABC, single active slot (Hermes+OpenClaw)
      mnemosyne_provider.py          # wires real mnemosyne-memory (Mnemosyne §6)
      keeper.py                      # consolidation -> Mnemosyne sleep() + curator (Hermes)
      vault.py                       # Obsidian long-term promotion (HiveOS-owned; Mnemosyne lacks)
    context/
      session_store.py               # SQLite + FTS5 sessions (Hermes SessionDB)
      compaction.py                  # head/tail-protected LLM summary (Hermes)
      prompt_builder.py              # prefix-cache byte-exact restore (Hermes/OpenClaw)
    tools/
      base.py                        # ToolDescriptor + availability signals (OpenClaw spec)
      registry.py                    # KEEP+upgrade: audited registry on RegistryBase
      executor.py                    # dispatch + guardrails + audit (Hermes/OpenJarvis)
      discovery.py                   # KEEP: discovery-first engine (HiveOS DNA)
      mcp/{client.py,server.py}      # MCP client+server (OpenJarvis/Hermes/Mnemosyne)
      builtins/                      # read_file/write_file/shell/web_get + gated tools
    gateway/
      app.py                         # KEEP+improve: FastAPI /chat /ws /approvals /budget
      protocol.py                    # typed, versioned protocol (OpenClaw gateway-protocol)
      auth.py                        # constant-time bearer (OpenClaw)
      channels/{base.py,telegram.py} # transport-only typed actions (OpenClaw/Hermes)
    autonomy/
      heartbeat.py                   # KEEP: never-idle gap loop (HiveOS)
      cron.py  tasks.py  commitments.py  # three-layer autonomy (Hermes §9) [deferred depth]
    surfaces/
      cli.py  voice.py               # KEEP scripts, promoted into package
    observability/
      telemetry.py  traces.py  audit.py  # cost/energy + trace collector (OpenJarvis)
  tests/                             # NEW: pytest suite (currently zero)
  scripts/  Deploy/  Dashboard/  Docs/
  data/  vault/                      # runtime (gitignored)
```

## A.3 Module boundaries (the dependency rule)
`core` depends on nothing internal (registry/events/types/config are leaves) →
`llm`, `memory`, `tools`, `context` depend on `core` → `agents` depend on
`llm`+`tools`+`memory`+`context` → `gateway`/`autonomy`/`surfaces` depend on
`agents`+`system`. `observability` subscribes to the EventBus only (no reverse
deps). Mirrors OpenJarvis's directed-dependency graph (OPENJARVIS §0 EventBus is
the connective tissue) and OpenClaw's "core stays plugin-agnostic."

## A.4 Honoring the PROTECTED files in the new layout
`Config/SOUL.md` and `Core/approval_gate.py` stay exactly where they are. The new
package reaches them without touching them:
- `src/hiveos/core/soul.py` reads `<repo>/Config/SOUL.md` by path (verbatim) and
  exposes `SOUL` — same content the old `settings.SOUL` loaded, now case-correct.
- `src/hiveos/core/approval.py` imports the existing gate from the in-place file via
  an `importlib`/path bridge and re-exports `gate`, `is_dangerous`,
  `PROTECTED_PATHS`. The canonical logic remains the untouched `Core/approval_gate.py`.
- A future, Kamil-approved cleanup may relocate them into the package; until then
  the bridge keeps the hard limit intact.

---

# PART B — Component-sourcing table

Verdict: **TAKE** (port ~as-is), **ADAPT** (port + reshape), **PATTERN** (reimplement
from spec), **KEEP** (HiveOS-original), **DISCARD**.

| Target module | Source repo | Exact source files | Action |
|---|---|---|---|
| core/registry.py | OpenJarvis | `core/registry.py` | TAKE |
| core/events.py | OpenJarvis | `core/events.py` | TAKE |
| core/types.py | OpenJarvis | `core/types.py` | TAKE/ADAPT |
| core/credentials.py | OpenJarvis | `core/credentials.py` | TAKE |
| core/config.py + doctor.py | OpenClaw (spec) + HiveOS | OpenClaw `src/config/*`, `src/state/*`; HiveOS `Core/settings.py` | ADAPT/PATTERN |
| core/system.py | OpenJarvis | `system/core.py`, `system/builder.py`, `system/orchestrator.py` | ADAPT |
| core/self_mod.py | HiveOS + OpenJarvis + Hermes | HiveOS `Core/self_mod.py`; OJ `learning/spec_search/*`; Hermes `agent/curator.py` | KEEP+ADAPT |
| core/approval.py, soul.py | HiveOS (PROTECTED) | `Core/approval_gate.py`, `Config/SOUL.md` | KEEP (bridge only) |
| llm/router.py | HiveOS + OpenJarvis | HiveOS `Core/model_router.py`; OJ `engine/_stubs.py`,`_discovery.py` | KEEP+ADAPT |
| llm/failover.py | Hermes | `agent/error_classifier.py`, `agent/retry_utils.py` | TAKE |
| llm/credential_pool.py | Hermes | `agent/credential_pool.py` (+persistence/sources) | TAKE/ADAPT |
| llm/model_catalog.py | OpenClaw + OpenJarvis | OpenClaw `packages/model-catalog-core/*` (spec); OJ `intelligence/model_catalog.py` | PATTERN/ADAPT |
| llm/adapters/minimax.py | HiveOS | `Core/model_router.py` `_minimax` | KEEP |
| llm/adapters/{anthropic,codex}.py | Hermes | `agent/anthropic_adapter.py`, `agent/codex_responses_adapter.py` | ADAPT |
| agents/base.py | OpenJarvis | `agents/_stubs.py` | TAKE |
| agents/orchestrator.py | OpenJarvis + Hermes | OJ `system/orchestrator.py`, `agents/orchestrator.py`; Hermes `agent/conversation_loop.py` | ADAPT |
| agents/executor.py | OpenJarvis + OpenClaw | OJ `agents/executor.py`,`manager.py`; OpenClaw `agent-run-terminal-outcome.ts` (spec) | ADAPT/PATTERN |
| agents/loop_guard.py | OpenJarvis / Hermes | OJ `agents/loop_guard.py`; Hermes `agent/tool_guardrails.py` | TAKE |
| agents/delegate.py | Hermes | `tools/delegate_tool.py` | ADAPT |
| agents/planner.py | HiveOS | `Core/planner.py` | KEEP |
| memory/provider.py | Hermes + OpenClaw | Hermes `agent/memory_provider.py`; OpenClaw `packages/memory-host-sdk` (single-slot spec) | TAKE/ADAPT |
| memory/mnemosyne_provider.py | Mnemosyne | `hermes_memory_provider/__init__.py` | TAKE |
| memory/keeper.py | HiveOS + Mnemosyne + Hermes | HiveOS `Memory/memory_keeper.py`; Mnemosyne `core/beam.sleep`; Hermes `agent/curator.py` | ADAPT |
| memory/vault.py | HiveOS | `Memory/brain.py` (`_promote_to_vault`) | KEEP |
| context/session_store.py | Hermes | `hermes_state.py` (SessionDB) | TAKE/ADAPT |
| context/compaction.py | Hermes | `agent/context_compressor.py` | ADAPT |
| context/prompt_builder.py | Hermes + OpenJarvis | Hermes `agent/prompt_builder.py`,`system_prompt.py`,`prompt_caching.py`; OJ `prompt/builder.py` | ADAPT |
| tools/base.py | OpenClaw (spec) | `src/tools/types.ts` | PATTERN |
| tools/registry.py | HiveOS + OpenJarvis + Hermes | HiveOS `Tools/registry.py`; OJ `tools/_stubs.py`; Hermes `tools/registry.py` | KEEP+ADAPT |
| tools/executor.py | OpenJarvis + Hermes | OJ `tools/_stubs.py` (ToolExecutor); Hermes `agent/tool_executor.py`,`file_safety.py` | ADAPT |
| tools/discovery.py | HiveOS | `Tools/discovery.py` | KEEP |
| tools/mcp/* | OpenJarvis (+Hermes/Mnemosyne) | OJ `mcp/*.py`,`tools/mcp_adapter.py`; Mnemosyne `mnemosyne/mcp_server.py` | TAKE/ADAPT |
| tools/builtins/* | HiveOS | `Tools/registry.py` builtins | KEEP |
| gateway/app.py | HiveOS | `Gateway/app.py` | KEEP+IMPROVE |
| gateway/protocol.py | OpenClaw (spec) | `packages/gateway-protocol/*` | PATTERN |
| gateway/auth.py | OpenClaw/Hermes | OpenClaw `src/gateway/credentials.ts`; Hermes `gateway` auth | PATTERN |
| gateway/channels/* | OpenClaw + Hermes | OpenClaw `src/channels/message/types.ts` (typed actions); Hermes `gateway/platforms/{base,telegram}.py` | PATTERN/ADAPT |
| autonomy/heartbeat.py | HiveOS | `Core/orchestrator.py` | KEEP |
| autonomy/{cron,tasks,commitments}.py | Hermes | `cron/*`, `src/tasks` analog; Hermes `agent/curator`? | ADAPT (deferred) |
| surfaces/cli.py, voice.py | HiveOS | `Scripts/chat.py`,`voice.py` | KEEP |
| observability/{telemetry,traces,audit}.py | OpenJarvis | `telemetry/*`, `traces/*` | ADAPT |
| budgeter | HiveOS + Hermes | HiveOS `Core/budgeter.py`; Hermes `usage_pricing`,`account_usage` | KEEP+ADAPT |
| — | HiveOS | `Memory/mnemosyne.py` | **DISCARD** |

---

# PART C — Phased build plan
Each phase has a single **verify** step. "py_compile + import" means
`python -m py_compile` AND a real `python -c "import hiveos.<mod>"` (the latter is
what would have caught the casing bug). Complexity: S/M/L.

| P | Goal | Builds | Replaces/Deletes | Verify | Cx |
|---|------|--------|------------------|--------|----|
| **P0** | Package skeleton | `pyproject.toml`, `src/hiveos/` tree (stubs), `.env.example`, `core/soul.py`, `core/approval.py` bridges, `tests/` scaffold | — (old code stays) | `pip install -e .`; `python -c "import hiveos; from hiveos.core import soul, approval"`; SOUL+gate byte-identical | S |
| **P1** | Core primitives | `core/registry.py`, `events.py`, `types.py`, `config.py`(+.env), `doctor.py`, `credentials.py` | — | import + unit tests for registry/eventbus/types | M |
| **P2** | LLM + resilience | `llm/router.py` (port current), `failover.py`, `credential_pool.py`, `model_catalog.py`, `adapters/minimax.py` | improves `Core/model_router.py` | `hive ping` (router smoke) + failover unit tests | M |
| **P3** | Memory (the big win) | `memory/provider.py`, `mnemosyne_provider.py`, `keeper.py`, `vault.py` | **replaces `Memory/brain.py` engine; DELETE `Memory/mnemosyne.py`** | remember→recall round-trip via real Mnemosyne; vault note written | M |
| **P4** | Context | `context/session_store.py`, `compaction.py`, `prompt_builder.py` | replaces `Core/session.py` | session persist+FTS recall; prefix-cache restore test | M |
| **P5** | Tools + MCP | `tools/base.py`, `registry.py`, `executor.py`, `discovery.py`(port), `mcp/client.py`,`server.py`, `builtins/` | upgrades `Tools/registry.py`,`discovery.py` | tool dispatch + gate routing tests; MCP client lists a server | M/L |
| **P6** | Agents | `agents/base.py`, `orchestrator.py`, `executor.py`, `loop_guard.py`, `delegate.py`, `planner.py` | improves `Core/orchestrator.py`,`planner.py` | one full agent turn (tool loop) + subagent delegation test | L |
| **P7** | System wiring | `core/system.py` (HiveSystem + SystemBuilder), wire all subsystems + EventBus | — | `SystemBuilder().build()`; `system.ask()` end-to-end | M |
| **P8** | Gateway + self_mod + autonomy | `gateway/{app,protocol,auth}.py`, `self_mod.py` extend (spec_search+curator), `autonomy/heartbeat.py`, `observability/*` | improves `Gateway/app.py`,`Core/self_mod.py`; promotes orchestrator | gateway `/health /chat /ws /approvals /budget`; self_mod dry-run; heartbeat tick | L |
| **P9** | Cutover + cleanup | switch entrypoints/systemd/docs to `hiveos.*`; remove superseded `Core/*`,`Memory/*`,`Tools/*` old modules (keep PROTECTED) | deletes old top-level modules after parity | full `pytest`; `hive doctor`; gateway+orchestrator boot on Linux | M |

**Review checkpoint (agreed):** after **P0–P1** (skeleton + core primitives
committed) I surface the architecture for Kamil's review before the heavy porting
(P3+). Each phase commits separately; old code is removed only in P9 after parity.

---

# PART D — Sequencing

## Build first (high value / low risk)
1. **P0–P1 foundation** — unblocks everything and fixes the Linux import bug.
2. **P3 memory** — biggest leverage, lowest effort (Mnemosyne is ~drop-in; kills
   bugs #2/#3 from the audit).
3. **P2 resilience** — a solo-user credit-plan agent needs failover/budget now.
4. **P4 context + P6 agents + P7 system** — the actual brain.

## Defer (valuable, after the core runs green)
- Full **MCP server** surface, **channels/Telegram**, **voice** polish.
- **Autonomy depth** (cron/tasks/commitments three-layer) — start with the existing
  heartbeat, add layers later (Hermes §9).
- **Observability depth** (energy, Pareto), **spec_search** full loop in self_mod
  (start with curator-style + risk tiers).
- **Dashboard** rework (consider OpenJarvis Tauri vs current Vite later).
- **doctor migrations** beyond the first schema.

## Skip (not for HiveOS)
- OpenClaw's ~120 extensions, companion apps, ClawHub/i18n; OpenJarvis **mining**
  and the full **evals** harness; Hermes's 20+ channel long tail and Chinese
  platforms; Mnemosyne's OpenWebUI integration and local-GGUF path (use MiniMax via
  `llm_backends`); any TS code (pattern-only).

## Success definition (end state)
A lowercase, installable `hiveos` package that **boots on Linux**, wires **real
Mnemosyne** memory, routes MiniMax (exec) + Codex (plan) with **failover +
budget**, runs an **agent loop with tools + subagents + context compaction**,
exposes a **typed gateway** with approvals, keeps the **SOUL/approval_gate/self_mod
safety spine untouched and extended**, and ships a **pytest suite + `hive doctor`**
— i.e., HiveOS finally *is* the system its docs describe, built on the strongest
parts of the four references.
