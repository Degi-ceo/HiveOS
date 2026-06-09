# HiveOS — Architecture & Rationale

This is the condensed, source-backed design behind HiveOS. It explains *why* each piece is built
the way it is, so any future change (by Hive or a human) respects the original intent.

## 1. Execution runner: MiniMax (Token Plan)
- Global endpoints: OpenAI-compatible `https://api.minimax.io/v1`; **Anthropic-compatible
  `https://api.minimax.io/anthropic`** (recommended by MiniMax — native interleaved thinking).
- Model strings move fast: `MiniMax-M2` → `M2.1/2.5/2.7` → `MiniMax-M3`. **Pinned in `.env`**, never
  hardcoded, so migration is one line. A `*-highspeed` variant is faster (not cheaper).
- Tool calling + interleaved thinking supported; reasoning must be preserved across turns or
  performance drops. We use the Anthropic Messages shape with `thinking` enabled.
- Token Plan is **credit-based** with rolling 5h + weekly windows (tiers ~Plus $20 / Max $50 /
  Ultra $120). The "4500 calls/5h" figure matches the historical M2.7-era $20 tier; current docs
  publish no fixed count. So the **budgeter self-calibrates** from `GET /v1/token_plan/remains`
  plus a local daily cap — it does not hardcode a call count.
- Pay-as-you-go overflow is cheap (~$0.30/M in, $1.20/M out for M2).

## 2. Planner/executor split
- Big model plans, cheap model executes (5–10× cheaper). ChatGPT Plus (via Codex OAuth, headless
  `codex exec`) is the **planner — thinking only, never execution**. MiniMax does all the work.
- Treat the Plus planner as a scarce, rate-limited resource; route only novel/high-stakes/gap work to it.

## 3. Memory brain
- **Active layer: Mnemosyne** (AxDSan/mnemosyne, `pip install mnemosyne-memory`): local,
  SQLite-backed (sqlite-vec + FTS5), working/episodic/scratchpad + temporal knowledge triples +
  memory banks + hybrid search; `mnemosyne sleep`/`evolve` does consolidation. ~800KB/1k memories —
  tiny on a 120GB disk. Runs as MCP (`mnemosyne mcp`). HiveOS ships a local SQLite fallback so it
  works before Mnemosyne is wired.
- **Long-term layer: Obsidian vault** (markdown), via the Local REST API plugin's built-in MCP or
  direct file writes. Durable learnings are promoted here, classified and linked — the "old memories".
- **Memory-keeper sub-agent** (cheap model) is the only writer to long-term storage: reflect →
  extract durable facts/skills → dedupe → promote → prune. This is the generative-agents memory
  stream + reflection pattern combined with Letta-style sleep-time compute. Result: once learned,
  never re-researched.

## 4. Self-improvement (Voyager + Darwin-Gödel + Reflexion)
- Voyager: automatic curriculum + ever-growing **skill library** of executable code + iterative
  prompting with self-verification.
- Darwin-Gödel Machine (Sakana): a self-improving coding agent that edits its own code and keeps an
  archive/lineage of variants — done with **sandboxing + modification limits + human oversight**.
- Reflexion: on failure, write "what went wrong" to memory and retry.
- "Never idle": when no user task is queued, the orchestrator runs a gap-analysis step to find one
  safe improvement — bounded by the budgeter.

## 5. Safe self-modification (the safety core)
- Every self-mod runs in an isolated **git worktree** on a branch (never live `main`).
- **Snapshot** last-known-good; **test** in the candidate; on failure **roll back + record + retry**.
- On success: push branch, **open a PR** on Hive's own GitHub with full English description
  (gaps, changes, tests, rollback), **notify Kamil in Polish**, and **wait for human merge**.
- `config/SOUL.md` and `core/approval_gate.py` are **human-only** — refused by the self-mod engine.
- Branch protection on `main` enforces "agent pushes branches + opens PRs; humans merge".
- A "config surgeon" skill governs config edits (validate → snapshot → test → rollback).

## 6. Discovery-first reuse
- Before building, search: Anthropic Agent Skills (`anthropics/skills`, agentskills.io, `SKILL.md`),
  official MCP Registry (`registry.modelcontextprotocol.io`), `modelcontextprotocol/servers`,
  reputable marketplaces (Smithery, mcp.so, Glama, PulseMCP), GitHub.
- **Mandatory safety audit before adoption** — large fractions of public MCP servers have SSRF /
  unsafe exec / no-auth issues, and a malicious MCP package has already appeared. Use
  `mcpserver-audit` (Cloud Security Alliance). Pin versions; sandbox before granting credentials.
- Anthropic's own security-review tooling is not injection-hardened — treat untrusted repo content
  as hostile.

## 7. Multi-agent spawning
- Orchestrator-worker (Anthropic multi-agent pattern; Claude Code subagents). Leaf agents have
  **tool restrictions** (a read-only reviewer physically cannot write), `model:` routing, and
  **cannot spawn further subagents**. Concurrency capped (default 3, ≤10).
- Cost: agents use ~4× tokens, multi-agent ~15× — so caps + budgeter are essential.
- Agent-creating-agents: new specialists are authored as `.claude/agents/*.md` via the PR flow.

## 8. Hive's GitHub identity
- Dedicated account; prefer a **GitHub App** (least-privilege, rotating tokens) or fine-grained PAT
  scoped to Contents + Pull requests, **no merge to main**. Use `github/github-mcp-server`.
- Never commit the token; inject via env/secret manager.

## 9. 24/7 on Hetzner
- **systemd** services (Restart=always, non-root `hive` user) for gateway + orchestrator; a timer
  for nightly consolidation. Docker Compose is the alternative for isolation.
- Heartbeat loop wakes Hive to check messages, advance tasks, run gap-analysis, consolidate memory.
- Secrets via `.env` (or sops). Storage local on the 120GB disk is fine; add retention/cleanup
  (prune worktrees, archive memory, rotate logs). Budgeter respects the MiniMax window.

## 10. Voice (later)
- Local/cheap: openWakeWord ("hej hive") + faster-whisper/whisper.cpp STT + Piper TTS via Wyoming.
  Gated behind the wake word to save CPU.

## 11. Language split
- Polish for all conversation/notifications to Kamil; English for all code/commits/branches/docs/PRs.
  Enforced at the top of SOUL.md.

## 12. Tri-tool buildability
- `CLAUDE.md` (Claude Code), `AGENTS.md` (Codex/open standard, mirror of CLAUDE.md), `.claude/`
  (settings, agents, skills, setup.sh). Build/test commands live in both so all three tools self-verify.

## Caveats
- MiniMax model names and plan structure change frequently — verify the live console before coding
  the budgeter; treat any model beyond officially documented current ones as unverified.
- MCP/skill supply-chain risk is real — the audit step is mandatory.
- ChatGPT-Plus-via-OAuth for programmatic planning is a personal-use path with server-side limits;
  for heavy planning volume, budget for the OpenAI API.
- Self-modifying agents are inherently risky; the human-merge gate is what makes HiveOS safe — never remove it.
