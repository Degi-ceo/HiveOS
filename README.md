# HiveOS — Autonomous Jarvis-like Agent (Hive)

HiveOS is the system; **Hive** is the agent. He runs 24/7 on a Hetzner VPS, talks to you in
**Polish**, builds in **English**, executes on **MiniMax** (Token Plan), reserves **ChatGPT Plus**
for heavy thinking, remembers everything (**Mnemosyne** active + **Obsidian** long-term),
reuses before building (**discovery-first**), and self-improves — changing his own code only
through **pull requests you merge**, never auto-merging to live.

## Start here
Read **`docs/BUILD_GUIDE.md`** — it has the secrets you need and one paste-ready prompt per phase
for Claude Code web, Claude Code on the VPS, or Codex. Architecture rationale + sources are in
**`docs/ARCHITECTURE.md`**.

## Quick local check
```bash
pip install -e .             # installable `hive` package (src/hive)
cp .env.example .env         # then edit .env with your keys
hive doctor --fix            # verify SOUL + env + state DB
hive ask "say hi"            # one-shot turn (needs MINIMAX_API_KEY)
hive serve                   # gateway: /chat /chat/stream /ws /approvals /budget
hive heartbeat               # 24/7 autonomy loop (cron + commitments + tasks)
hive consolidate             # one sleep-time memory consolidation pass
pytest -q                    # the test suite
```

## Layout
```
Config/SOUL.md            PROTECTED immutable identity+rules (+ goals.json)
Core/approval_gate.py     PROTECTED danger firewall
src/hive/                 the installable `hive` package
  core/      registry · events · types · config · doctor · credentials
             soul+approval (bridges to PROTECTED files) · self_mod · spec_search
             · budgeter · sandbox
  llm/       router · failover · credential_pool · model_catalog · pricing
             · rate_limit · sanitize · adapters/minimax
  agents/    base · orchestrator · executor · loop_guard · delegate · planner
  memory/    provider · mnemosyne_provider · local · keeper · vault
             · curator · skill_usage
  context/   session_store · compaction · prompt_builder
  tools/     base · registry · executor · discovery · file_safety
             · mcp/{client,server} · builtins
  gateway/   app (FastAPI) · protocol · auth · channels/{base,telegram}
  autonomy/  heartbeat · cron · tasks · commitments
  surfaces/  cli · voice
  observability/ telemetry · traces · audit
  runtime.py  HiveOS + HiveOS.build() (composition root)
tests/  docs/  deploy/ (systemd)  dashboard/ (Vite+React)
CLAUDE.md / AGENTS.md   tri-tool build configs
docs/     BUILD_GUIDE.md · ARCHITECTURE.md · references/{SYNTHESIS,*_REFERENCE}.md
```

## The invariants
SOUL.md + approval gate are human-only · Hive never merges to main (PR → you merge) ·
discovery-first reuse · once learned always remembered · Polish to you / English in code.
