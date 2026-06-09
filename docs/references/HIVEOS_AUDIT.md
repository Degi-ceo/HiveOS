# HIVEOS_AUDIT — honest audit of the current system vs the reference repos

> Full read of **every** HiveOS source file (17 real `.py` files, 1,352 LOC, plus
> Config/dashboard/deploy/Docs). This is the brutally honest verdict the plan asked
> for: per-component KEEP / IMPROVE / REPLACE / DELETE / MISSING, each pointing at
> the better version in OpenJarvis / OpenClaw / Hermes / Mnemosyne (see the other
> four files in this folder). The companion is `SYNTHESIS.md` (the rebuild
> blueprint). **Two of the files here must never be edited — `Config/SOUL.md` and
> `Core/approval_gate.py` — and this audit respects that.**

## TL;DR
HiveOS today is a **clean, well-commented v0 skeleton with the right intentions
and the wrong execution status**: it is small (1.3k LOC), the architecture
*documents* match the four reference systems conceptually, but **it does not
currently run on Linux**, its **designated memory layer (Mnemosyne) is declared
but never wired**, it carries a **dead duplicate memory module**, and it has
**zero tests**. The design instincts are good; the implementation is ~5% of what
the four references already solve in Python. The path forward is not "fix HiveOS"
— it is "re-skeleton HiveOS on OpenJarvis's primitives + Hermes's runtime
resilience + Mnemosyne as memory, governed by OpenClaw's architectural rules."

---

## 0. SHOWSTOPPER BUGS (verified, must fix before anything else)

1. **Case-mismatch: HiveOS cannot import on a case-sensitive filesystem (Linux).**
   Directories are capitalized (`Core/ Memory/ Tools/ Gateway/ Config/ scripts/`)
   but every import is lowercase (`from core import settings`,
   `from memory.brain import brain`, `from tools import registry`), and
   `settings.py` reads `ROOT/"config"/"SOUL.md"`. Verified:
   `python3 -c "import core.settings"` → `ModuleNotFoundError: No module named
   'core'`; `config/SOUL.md` does not resolve (only `Config/`). systemd units
   (`deploy/*`) and scripts use `gateway.app:app`, `core.orchestrator`,
   `scripts.ping` — all would fail on the VPS. It presumably "worked" only on a
   case-insensitive macOS/Windows dev box. **`python -m py_compile` passes (it does
   not resolve imports), so the CLAUDE.md "compile check" gives false confidence.**
   → Fix by adopting a proper lowercase package layout in the rebuild (Phase 7).
2. **The active memory layer is not actually wired.** `Memory/brain.py` docstring
   claims "ACTIVE: Mnemosyne (over MCP if `MNEMOSYNE_MCP_URL` set; else local
   SQLite fallback)" but the code only ever stores `self._mnemosyne =
   settings.MNEMOSYNE_MCP` and **never calls Mnemosyne** — it's always the
   homegrown SQLite fallback. `requirements.txt` lists `mnemosyne-memory>=0.1` but
   nothing imports it. The single most "already-solved" component
   (MNEMOSYNE_REFERENCE §6: drop-in `MnemosyneMemoryProvider`) is unused.
3. **Dead duplicate module.** `Memory/mnemosyne.py` (a homegrown `Mnemosyne` class
   with its own episodic/facts SQLite + Qdrant) is **imported nowhere** (verified)
   and collides in name with the real Mnemosyne package. Pure confusion debt.
4. **Zero tests.** No `tests/`, no CI. All four reference repos ship large suites
   (Hermes ~17k tests, OpenJarvis/OpenClaw/Mnemosyne extensive). CLAUDE.md's only
   "verify" is a py_compile that can't catch bug #1.
5. **Missing `.env.example`.** `scripts/setup.sh` and docs reference it; it doesn't
   exist. `python-dotenv` is in requirements but nothing calls `load_dotenv()`.

---

## 1. Per-component verdicts

Legend: **KEEP** (good as-is), **IMPROVE** (keep shape, upgrade with a reference),
**REPLACE** (swap for a reference component), **DELETE** (remove), **PROTECTED**
(keep, never edit).

| Component | LOC | Verdict | Why / better source |
|-----------|----:|---------|---------------------|
| `Config/SOUL.md` | — | **PROTECTED / KEEP** | Strong immutable identity+safety contract. Mirrors Hermes "never-edit agent-created/identity" + OpenClaw safe-defaults ethos. Do not touch. |
| `Config/goals.json` | — | **KEEP** | Fine. Could become a recipe (OpenJarvis/Hermes recipe format) later. |
| `Core/approval_gate.py` | 73 | **PROTECTED / KEEP** (minor improve later, human-only) | Sound allowlist-of-danger + PROTECTED_PATHS. Regexes (`deploy|prod`, `secret|token`) will over-gate benign text — a future human-approved refinement could use OpenClaw's typed approval actions + Hermes per-session approval queue. Never auto-edit. |
| `Core/model_router.py` | 110 | **IMPROVE** | Good planner/executor split (MiniMax exec + Codex planner) and MiniMax Anthropic endpoint. But no resilience: add Hermes `error_classifier.FailoverReason` + `credential_pool` + provider-adapter pattern (HERMES §4), OpenJarvis engine ABC + discovery (OPENJARVIS §3.3), OpenClaw model-catalog compat + tool-call-repair (OPENCLAW §5). Thinking blocks are dropped from the return; no streaming. |
| `Core/budgeter.py` | 68 | **IMPROVE** | Works for MiniMax token plan. Fold in Hermes `usage_pricing`/`account_usage`/`iteration_budget` and OpenJarvis telemetry cost recording for real per-model cost + energy. |
| `Core/orchestrator.py` | 125 | **IMPROVE (significantly)** | Nice heartbeat + never-idle gap-analysis loop. But "subagents" just call tools (no real isolated subagents). Adopt OpenJarvis `QueryOrchestrator`+`AgentExecutor` tick lifecycle + EventBus (OPENJARVIS §3.4/3.7), Hermes `delegate_task` real subagents + cron/tasks/commitments three-layer autonomy (HERMES §9), OpenClaw terminal-outcome normalization. |
| `Core/planner.py` | 39 | **KEEP** (small) | Fine thin planner; will sit on the improved router. |
| `Core/session.py` | 26 | **REPLACE** | Homegrown context build. Replace with Hermes `SessionDB` (FTS5) + prefix-cache byte-exact system-prompt restore + context compaction (HERMES §5/§6) / OpenClaw context-engine. |
| `Core/settings.py` | 57 | **IMPROVE** | Env-only config is fine for v0 but: fix casing (bug #1), add a canonical config schema + `doctor --fix` migrations (OpenClaw VISION/§7), actually load `.env`. |
| `Core/self_mod.py` | 111 | **KEEP / IMPROVE (crown jewel)** | Genuinely good safe-self-mod flow (worktree → snapshot → test → PR, never merge, refuse PROTECTED). This is HiveOS's best original asset. Extend with OpenJarvis `spec_search` (diagnose→plan→gate→rollback, risk tiers; OPENJARVIS §3.6) + Hermes `Curator` (skill lifecycle, never-delete, backups; HERMES §6) + OpenClaw audit-collector/typed-approval. |
| `Memory/brain.py` | 127 | **REPLACE (wrap)** | Homegrown SQLite working+knowledge+FTS+Obsidian-promote. Claims Mnemosyne but never wires it (bug #2). Replace the engine with real Mnemosyne via `MnemosyneMemoryProvider` (MNEMOSYNE §6 shortest path); keep a thin `remember/recall/learn/already_known` facade + the Obsidian-vault promotion (which Mnemosyne does NOT provide — that part stays HiveOS-owned). |
| `Memory/mnemosyne.py` | 116 | **DELETE** | Orphaned (imported nowhere, verified), homegrown stub, Qdrant dep used nowhere else, name-collides with the real package. Remove after the brain replacement lands. |
| `Memory/memory_keeper.py` | 57 | **IMPROVE/REPLACE** | Good sleep-time-compute idea. Route consolidation to Mnemosyne `sleep()` + Hermes Curator pattern; keep as the scheduler-facing keeper wrapper. |
| `Tools/registry.py` | 117 | **IMPROVE** | Decent small audited registry + gate routing. Upgrade to OpenJarvis `RegistryBase[T]` (OPENJARVIS §3.1) + OpenClaw descriptor/planner/executor + availability signals (OPENCLAW §8) + Hermes AST self-discovery (HERMES §7). Dangerous-tool bodies are stubs (`spend_money` returns a string) — wire real implementations behind the gate. |
| `Tools/discovery.py` | 99 | **KEEP / IMPROVE** | Real, on-brand discovery-first engine (MCP registry + GitHub + red-flag audit). Keep; wire `audit_repo` to a real auditor, cache via Mnemosyne, and reuse OpenClaw/Hermes plugin/skill discovery patterns for adoption. |
| `Gateway/app.py` | 113 | **KEEP / IMPROVE** | Clean FastAPI (`/health /chat /ws /budget /approvals`). Improve: typed+versioned protocol (OpenClaw `gateway-protocol`; OPENCLAW §7), constant-time auth, SSE streaming (Hermes/OpenJarvis), multi-platform channel layer (Telegram) via Hermes/OpenClaw transport patterns. |
| `scripts/chat.py` | 28 | **KEEP** | Fine WS client (fix casing). |
| `scripts/ping.py` | 18 | **KEEP** | Fine smoke test (will pass once bug #1 is fixed). |
| `scripts/voice.py` | 68 | **KEEP** | Reasonable lazy-imported voice surface (whisper/piper). Out of core scope; defer. |
| `scripts/setup.sh` | 12 | **IMPROVE** | Pins py3.12 (docs say 3.11); references missing `.env.example`; should install the package + Mnemosyne. |
| `dashboard/*` (JSX/Vite) | — | **KEEP (defer)** | Out of the Python-first core scope. Later consider OpenJarvis's Tauri desktop pattern (OPENJARVIS §7) vs current React/Vite. |
| `deploy/*` (systemd) | — | **KEEP / IMPROVE** | Units are fine; ExecStart works once the package layout is fixed. Add the keeper timer target. |
| `requirements.txt` | — | **IMPROVE** | Declares `mnemosyne-memory` + `python-dotenv` that are unused; add real deps as components are wired; move to `pyproject.toml`. |
| `docs/memory/*` | — | **KEEP (excellent)** | The 2,182-line `MNEMOSYNE.md` + integration-phases doc are first-rate; my `MNEMOSYNE_REFERENCE.md` complements them with the code map. |
| `docs/{ARCHITECTURE,BUILD_GUIDE,ALL_PHASES}.md` | — | **KEEP / RECONCILE** | Good intent docs; reconcile with `SYNTHESIS.md` so the stated phases match the rebuilt architecture. |

---

## 2. MISSING (capabilities the references have and HiveOS lacks entirely)

Ranked by leverage for HiveOS's goals:

1. **Real memory wiring** — Mnemosyne is the designated layer and is ~drop-in
   (MNEMOSYNE §6). Highest-value, lowest-effort gap.
2. **Provider resilience** — no failover taxonomy, credential pool, fallback,
   rate-limit handling, or prompt caching (Hermes §4–5). A solo-user agent on a
   credit plan needs this.
3. **Foundation primitives** — no registry/EventBus/canonical types/single-source
   system object (OpenJarvis §3.1/3.7). Everything else hangs off these.
4. **Context management** — no compaction, no FTS5 session store, no prefix-cache
   restore, no tool-loop detection (Hermes §5/§7, OpenClaw §4).
5. **Real tool platform** — no MCP client/server, no ACP, no tool
   descriptor/availability model, no terminal-environment/sandbox abstraction
   (all four repos).
6. **Real subagents** — `delegate_task`-style isolated parallel workers (Hermes §7,
   OpenJarvis sandboxed agents).
7. **Self-improvement depth** — `self_mod` exists but lacks spec_search diagnose→
   plan→gate→rollback + Curator skill lifecycle (OpenJarvis §3.6, Hermes §6).
8. **Autonomy depth** — only a heartbeat; no cron/tasks/commitments, no scheduler
   (Hermes §9, OpenJarvis scheduler/operators).
9. **Observability** — no telemetry/traces/audit beyond a flat audit.log
   (OpenJarvis telemetry+traces, OpenClaw audit).
10. **Tests + CI** — none. Plus packaging (`pyproject.toml`), config schema +
    doctor migrations, channels (Telegram), streaming.

---

## 3. What HiveOS already gets RIGHT (keep the DNA)
- **The safety spine**: SOUL.md immutability + approval_gate + self_mod
  worktree/PR/never-merge flow is conceptually aligned with — and in places
  cleaner than — the references. This is the part to protect and build around.
- **Discovery-first** as a first-class engine (`Tools/discovery.py`) — a genuinely
  good idea most references treat only implicitly (ClawHub, skills hubs).
- **Planner/executor split** (MiniMax exec + ChatGPT-Plus planner) — a sound,
  cost-aware routing decision.
- **Small, readable, honestly-commented code** — easy to rebuild from, no cruft to
  untangle (except the two dead spots above).
- **Memory docs** are excellent and ahead of the code.

## 4. Honest sizing
- Current real Python: **1,352 LOC**, ~17 files. Of that: ~2 files are
  dead/duplicate (`Memory/mnemosyne.py`, half of `Memory/brain.py`'s premise),
  and 100% is currently non-running on Linux.
- The four references total **~27,000 files**; the Python-portable subset relevant
  to HiveOS (OpenJarvis core/engine/agents/system, Hermes resilience+curator,
  Mnemosyne whole) is large and mature. **HiveOS should consume, not compete.**

## 5. Recommended disposition (feeds SYNTHESIS.md / Phase 7-8)
- **DELETE**: `Memory/mnemosyne.py`.
- **REPLACE**: `Memory/brain.py` engine → Mnemosyne provider; `Core/session.py` →
  Hermes session store + compaction.
- **PROTECTED/KEEP untouched**: `Config/SOUL.md`, `Core/approval_gate.py`.
- **KEEP & extend (HiveOS-original DNA)**: `Core/self_mod.py`,
  `Tools/discovery.py`, the planner/executor split, the safety docs.
- **IMPROVE on reference primitives**: router, budgeter, orchestrator, registry,
  gateway, settings.
- **FOUNDATION FIRST (the rebuild)**: introduce a proper lowercase Python package
  (`src/hiveos/...` with `pyproject.toml`), a registry + EventBus + canonical types
  (OpenJarvis), wire Mnemosyne, then layer resilience (Hermes) and the
  architectural rules (OpenClaw). Fixing the casing bug is subsumed by this — the
  new package is lowercase by construction.

> Next: `SYNTHESIS.md` turns these verdicts into the target directory tree, a
> component-sourcing table (component → source repo → exact files → take/adapt/
> discard), and the phased build plan.
