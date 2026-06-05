# HiveOS — Build Guide (read me first)

**HiveOS** is the system. **Hive** is the agent — a Jarvis-like, autonomous, self-improving
assistant that runs 24/7 on your Hetzner VPS. He talks to you in **Polish**, builds in **English**,
runs on **MiniMax** (Token Plan) for execution, reserves **ChatGPT Plus** for heavy thinking,
remembers everything (Mnemosyne + Obsidian), reuses before building (discovery-first), and
changes his own code only via **pull requests you merge** — never auto-merging to live.

This repo is already a working skeleton. You build it up phase by phase with Claude Code or Codex.
Each phase is one paste-ready prompt. Run one, review the PR/branch it pushes, merge, then run the next.

> Full architecture rationale and sources are in `docs/ARCHITECTURE.md`.

---

## Before anything: secrets you need
- **`MINIMAX_API_KEY`** — your MiniMax Token Plan key. Verify the exact model string
  (`MiniMax-M2` vs `MiniMax-M3`) and base URL in the MiniMax console; set them in `.env`.
- **`HIVE_SECRET`** — a long random string (gateway auth).
- **`HIVE_GITHUB_TOKEN`** — a token for *Hive's own* GitHub account (fine-grained PAT or GitHub App),
  scoped to Contents + Pull requests on his repo, with **no merge rights to `main`**.
- (Later) **ChatGPT Plus** via Codex login for the planner; **Obsidian** REST token; **Mnemosyne** MCP URL.

Set MiniMax key as `ANTHROPIC_AUTH_TOKEN` in the Claude Code environment (not committed).

---

## Choose your builder

### A) Claude Code web (best for iPad)
1. Push this whole repo to Hive's GitHub account (private is fine).
2. Go to **claude.ai/code**, sign in, connect that repo.
3. In environment settings add secrets (`MINIMAX_API_KEY` as `ANTHROPIC_AUTH_TOKEN`, `HIVE_SECRET`,
   `HIVE_GITHUB_TOKEN`) and enable network access. The VM auto-runs `.claude/setup.sh`.
4. Paste the phase prompts below in order.

### B) Claude Code on the Hetzner VPS
```bash
ssh hive@your-vps
git clone <hive-repo-url> /opt/hiveos && cd /opt/hiveos
bash scripts/setup.sh           # installs deps, makes .env
# wire MiniMax for Claude Code itself:
export ANTHROPIC_BASE_URL=https://api.minimax.io/anthropic
export ANTHROPIC_AUTH_TOKEN=<your_minimax_key>
npm install -g @anthropic-ai/claude-code   # needs Node.js
claude
```
Then paste the phase prompts.

### C) OpenAI Codex
Codex reads `AGENTS.md` automatically. In Codex web, connect the repo; the setup script installs
deps. Paste the same phase prompts (they are tool-agnostic).

---

## The phase prompts — paste one at a time, in order

> Two rules apply to EVERY phase and are repeated in each prompt:
> never edit `config/SOUL.md` or `core/approval_gate.py`; never merge to `main` (open a PR).

### ▶ START PHASE 0 — Verify foundation
```
Read CLAUDE.md, config/SOUL.md, and docs/ARCHITECTURE.md. Confirm you understand:
HiveOS = system, Hive = agent; MiniMax executes, ChatGPT Plus only plans; discovery-first;
self-mod only via PR; Polish to user / English in code. Then run
`python -m py_compile core/*.py gateway/*.py tools/*.py memory/*.py scripts/*.py`
and report the repo is healthy. Do not change SOUL.md or the approval gate.
```

### ▶ START PHASE 1 — Runner + budgeter + chat
```
Implement & verify Phase 1. Confirm core/model_router.py talks to MiniMax on the
Anthropic-compatible endpoint with interleaved thinking, and core/budgeter.py polls the
Token Plan remains endpoint and enforces the daily cap. Run `python -m scripts.ping` and
confirm Hive replies. Start the gateway (`uvicorn gateway.app:app --port 8088`); confirm
/health, /budget, /chat, and /ws work and scripts/chat.py connects. Open a PR with results.
```

### ▶ START PHASE 2 — Memory brain (Mnemosyne + Obsidian)
```
Implement & verify Phase 2. Wire memory/brain.py so every turn is remembered and durable
learnings are saved and recalled. Confirm brain.recall() finds prior knowledge so Hive does
NOT re-research a known topic. If MNEMOSYNE_MCP_URL is set, integrate the Mnemosyne MCP server
(AxDSan/mnemosyne, `mnemosyne mcp`); else confirm the local SQLite fallback works. Confirm
durable learnings are promoted to the Obsidian vault folder as classified markdown. PR with results.
```

### ▶ START PHASE 3 — Memory-keeper + consolidation
```
Implement & verify Phase 3. Confirm memory/memory_keeper.py runs on the cheap AUX model,
reflects over recent memory, extracts durable skill/mcp/research/fix/fact items, dedupes
against already_known(), and promotes them to long-term storage. Wire the deploy/hiveos-keeper
timer for nightly consolidation (sleep-time compute). PR with results.
```

### ▶ START PHASE 4 — Discovery engine + safety audit
```
Implement & verify Phase 4. Confirm tools/discovery.py searches official sources (MCP Registry,
modelcontextprotocol/servers, Anthropic skills, GitHub) and caches results in memory so the same
search never repeats. Confirm audit_repo() flags dangerous patterns. As a real test, discover an
existing MCP/skill for a small need, audit it, and report REUSE vs BUILD. Then adopt mcpserver-audit
(CSA) as the deep-audit tool if vetted. PR with results.
```

### ▶ START PHASE 5 — Planner/executor split (ChatGPT Plus)
```
Implement & verify Phase 5. Set HIVE_PLANNER_ENABLED=true and wire the planner role to ChatGPT
Plus via headless Codex (`codex exec`) in core/model_router.py (_plan). Confirm heavy planning
(TaskKind.PLAN) routes to the planner and everything else stays on MiniMax. The planner must only
think/plan, never execute. Add a task classifier in core/planner.py. PR with results.
```

### ▶ START PHASE 6 — Self-modification via PR (safety core)
```
Implement & verify Phase 6 — the most important phase. Confirm core/self_mod.py: creates a git
worktree on a new branch, snapshots last-known-good, applies changes only in the candidate, runs
tests, rolls back + records on failure, and on success pushes the branch and opens a PR on Hive's
own GitHub (via gh or github-mcp-server) with full English description, then notifies Kamil in
Polish. Confirm it REFUSES changes touching config/SOUL.md or core/approval_gate.py. Set up branch
protection on main so Hive can push branches and open PRs but cannot merge. PR with results.
```

### ▶ START PHASE 7 — Multi-agent spawning
```
Implement & verify Phase 7. Confirm the orchestrator dispatches tasks to leaf sub-agents
(max HIVE_MAX_AGENTS concurrent) that cannot spawn further sub-agents. Wire the .claude/agents
specialists (discovery-scout read-only, code-reviewer read-only) and confirm tool restrictions
are enforced. Let Hive author a NEW specialist agent file as a self-mod PR (agent-creating-agents).
PR with results.
```

### ▶ START PHASE 8 — Self-improvement loop (never idle)
```
Implement & verify Phase 8. Confirm the orchestrator's gap-analysis step runs when no user task
is queued: scan capabilities + recent failures, pick ONE safe improvement, and queue it
(Voyager curriculum + Reflexion). Confirm improvements that change Hive's code go through the
Phase 6 PR flow. Keep it bounded by the budgeter so it never wastes the MiniMax window. PR with results.
```

### ▶ START PHASE 9 — 24/7 hardening on Hetzner
```
Implement & verify Phase 9. Install the systemd units in deploy/ (gateway, orchestrator, keeper
timer) under a non-root `hive` user with Restart=always. Add structured logging + rotation, a
health alert, and retention/cleanup (prune stale worktrees, archive low-value memory, rotate logs).
Confirm secrets load from .env (or sops). Confirm the stack survives a reboot. PR with results.
```

### ▶ START PHASE 10 — Voice (Jarvis feel)
```
Implement & verify Phase 10 (skip gracefully if no audio device). Wire scripts/voice.py to
openWakeWord ("hej hive"), faster-whisper STT, and Piper TTS via the Wyoming protocol. Gate STT
behind the wake word to save CPU. Add install notes to README. PR with results.
```

---

## Verification table

| Phase | Verify |
|---|---|
| 0 | `py_compile` clean; agent restates the rules |
| 1 | `scripts.ping` replies; /health /budget /chat /ws work |
| 2 | `brain.recall` finds prior knowledge; vault notes written |
| 3 | keeper extracts + dedupes durable learnings nightly |
| 4 | discovery returns candidates + audit verdict; cached |
| 5 | heavy planning hits ChatGPT Plus; execution stays MiniMax |
| 6 | worktree→test→PR; rollback on fail; SOUL/gate refused |
| 7 | ≤max leaf agents; restrictions enforced; new agent via PR |
| 8 | idle loop finds + queues one safe improvement, bounded |
| 9 | systemd survives reboot; logs rotate; cleanup runs |
| 10 | wake word → spoken reply |

## The invariants (true in every phase)
- `config/SOUL.md` and `core/approval_gate.py` are human-only. Never edited by Hive.
- Hive never merges to live `main`. Branch → tests → PR → **you merge**.
- Discovery-first: reuse vetted official solutions before building.
- Once learned, always remembered — no repeated research.
- Polish to Kamil; English in all code/docs/PRs.
