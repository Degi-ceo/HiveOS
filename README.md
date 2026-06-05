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
bash scripts/setup.sh        # then edit .env with your keys
source .venv/bin/activate
python -m scripts.ping       # confirms SOUL + MiniMax wiring
uvicorn gateway.app:app --port 8088   # gateway
python -m core.orchestrator           # 24/7 autonomy loop
```

## Layout
```
config/   SOUL.md (immutable identity+rules), goals.json
core/     settings · model_router · budgeter · planner · orchestrator · approval_gate · self_mod · session
memory/   brain (Mnemosyne+Obsidian) · memory_keeper (consolidation)
tools/    registry (audited) · discovery (discovery-first)
gateway/  app.py (FastAPI: /chat /ws /approvals /budget)
scripts/  setup · ping · chat · voice
dashboard/ Mission Control (Vite+React)
deploy/   systemd units (gateway, orchestrator, keeper timer)
.claude/  settings.json · agents/ · skills/ · setup.sh
CLAUDE.md / AGENTS.md   tri-tool build configs
docs/     BUILD_GUIDE.md · ARCHITECTURE.md
```

## The invariants
SOUL.md + approval gate are human-only · Hive never merges to main (PR → you merge) ·
discovery-first reuse · once learned always remembered · Polish to you / English in code.
