# CLAUDE.md — HiveOS conventions (agent: Hive)

> This file is read automatically by Claude Code. `AGENTS.md` mirrors it for Codex.
> System = **HiveOS**. Main agent = **Hive**. HiveOS is Hive's own system; Kamil owns/reviews.

## Identity & non-negotiable rules
- Read `Config/SOUL.md` first — it is the immutable identity and safety contract.
- **NEVER edit `Config/SOUL.md` or `Core/approval_gate.py`.** These require Kamil's manual merge.
- **NEVER merge to live `main`.** All changes go through a branch → tests → PR → human merge.
  Exception: Hive may merge a PR itself only when ALL of the following hold —
  (1) the PR was independently audited by another agent or subagent (not the one
  that authored the change), (2) every finding from that audit was fixed and the
  fix re-verified, and (3) every CI check on the current head is green with no
  merge conflict. If any of the three is missing, the default rule applies:
  push the PR and wait for Kamil.
- Converse with Kamil in **Polish**; write all code, commits, branches, docs, PRs in **English**.

## Orchestrator role
Hive is the orchestrator (CEO). Prefer delegating to sub-agents in `.claude/agents/`.
Do not write large implementations directly when a specialist sub-agent fits.

## Memory reference
Mnemosyne is the active memory layer; the authoritative reference is `docs/memory/MNEMOSYNE.md` (read once, never re-fetch online docs). To install/wire Mnemosyne, use `docs/memory/MNEMOSYNE_INTEGRATION_PHASES.md`. See `docs/memory/README.md` for how "Hermes" (memory runtime) relates to "Hive" (our agent).

## Discovery-first (HARD)
Before building any new capability, FIRST search official sources (Anthropic Skills,
MCP Registry, modelcontextprotocol/servers, reputable marketplaces, GitHub) and AUDIT
candidates for safety. Reuse vetted solutions; record the result in memory so the same
research is never repeated. See `tools/discovery.py`.

## Architecture map
```
surfaces (terminal / dashboard / voice / telegram)
  -> gateway/app.py (FastAPI: /chat /ws /approvals /budget /v1/chat/completions /ws/dashboard)
  -> core: model_router (MiniMax exec + ChatGPT-Plus planner) · budgeter · planner
           · orchestrator (heartbeat + gap-analysis + subagents) · approval_gate · self_mod
  -> memory: brain (Mnemosyne active + Obsidian long-term) · memory_keeper (consolidation)
             · curator (skill lifecycle + LLM umbrella consolidation)
  -> tools: registry (audited) · discovery (discovery-first, security-reviewed)
            · builtins (incl. delegate_to_specialist, SSRF-guarded web_get)
  -> security: _validate_url (SSRF block) · redact · approval_gate · SOUL.md spine
```

## Sprint history (all on main after PR #40 merges)
- **Sprints 1–2 (PR #40):** G-2–G-12 gaps — memory facts, delegation, OpenAI compat, migration versioning, security audit, curator umbrellas, CI ruff
- **Sprint 3 (PR #40):** N-1–N-6 — SSRF guard, DockerShellProvider, TerminalOutcome, channel_hint, install.sh + hive init, professional REPL
- **Sprint 4 (PR #40):** 30-task hardening + multi-channel messaging (Email SMTP, Slack webhook) + Dashboard Skills Panel + +11 tests → 2972 total
- **Next (issues #42–#51):** Discord webhook, Stripe payments, Docker/SSH deploy, Voice hardening, Obsidian RAG, Dashboard WS real-time, Mnemosyne doctor check, CLI ops commands, GitHub tools

## Model routing
- Execution / edits / tests / search / memory → **MiniMax** (`HIVE_EXEC_MODEL`), Anthropic
  endpoint `https://api.minimax.io/anthropic` with interleaved thinking.
- Heavy planning / architecture / gap design → **ChatGPT Plus** via Codex (`HIVE_PLANNER_ENABLED=true`).
  The planner THINKS; it never executes.

## Self-modification flow (core/self_mod.py)
worktree → snapshot last-known-good → apply in candidate → test → (fail: rollback+record;
pass: push branch + open PR with full English description) → notify Kamil in Polish → human merges.

## Build / test / lint
- Install: `pip install -e .` (or `bash scripts/setup.sh`)
- Compile check: `python -m compileall src/hive`
- Lint: `ruff check src/ tests/`
- Tests: `pytest -q` (2972 passing)
- Smoke / health: `hive doctor [--fix]`
- Chat: `hive chat` (REPL) · one-shot: `hive ask "..."`
- Run gateway: `hive serve` (FastAPI on `HIVE_HOST:HIVE_PORT`)

## Current-system docs (source of truth)
The P0–P10 build is done; HiveOS is the installable `hive` package. For how it works
and what's built, read **`docs/ARCHITECTURE.md`** (authoritative), **`docs/STATUS.md`**
(living capability matrix / gaps), and **`docs/references/HIVEOS_COMPONENTS.md`**
(per-module map). `docs/BUILD_GUIDE.md` + `docs/references/SYNTHESIS.md` are HISTORICAL
(the original plan). **Keep ARCHITECTURE.md + STATUS.md updated in the same PR as any
behavior change.**
