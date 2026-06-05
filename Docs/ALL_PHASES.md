# HiveOS — All Phases (build guide for Claude Code)

This is the master runbook. **HiveOS** is the system; **Hive** is the agent.
Brain: MiniMax M2.7 (reasoning) · MiniMax-highspeed (aux) · ChatGPT OAuth (fallback only).
Hive runs fully autonomously and asks for approval ONLY on dangerous actions.

---

## How to use this with Claude Code

### Option A — Claude Code web (recommended for iPad)
1. Push this whole repo to GitHub (private is fine).
2. Go to **claude.ai/code**, sign in, connect GitHub, pick this repo.
3. In the environment settings add secrets: `MINIMAX_API_KEY`, `OPENAI_API_KEY`,
   `HIVE_SECRET`. Enable network access for tasks that call the model APIs.
4. The VM auto-runs `.claude/setup.sh` on clone (installs deps, makes `.env`).
5. Paste the prompts below, one phase at a time. Review each pushed branch, then continue.

### Option B — Claude Code on the VPS
```bash
ssh your-vps
git clone <your-repo-url> /opt/hiveos && cd /opt/hiveos
npm install -g @anthropic-ai/claude-code   # needs Node.js
claude
```
Then paste the same phase prompts.

---

## The phase prompts (paste these in order)

> Each prompt is self-contained. Run one, review the branch/diff, then run the next.
> Two rules apply to EVERY phase: never edit `config/SOUL.md`; never weaken
> `core/approval_gate.py`.

### START PHASE 1 — Gateway + terminal chat
```
Read CLAUDE.md, docs/ALL_PHASES.md, config/SOUL.md, and core/model_router.py first.
Then verify Phase 0: run `python -m scripts.ping` and confirm SOUL.md loads and the
model router returns a reply (use the env secrets). Fix env/model-id issues if any.
Next implement & verify Phase 1: start the gateway with
`uvicorn gateway.app:app --host 0.0.0.0 --port 8088`, confirm GET /health is ok,
POST /chat returns a reply, and the WebSocket /ws streams. Confirm scripts/chat.py
connects. Report results. Do NOT edit config/SOUL.md or core/approval_gate.py.
```

### START PHASE 2 — Memory (Mnemosyne)
```
Implement & verify Phase 2. Wire memory/mnemosyne.py into the gateway so every
turn is logged to episodic SQLite and recalled on the next message. Run a test:
send two messages, restart the gateway, confirm Hive still recalls facts from
data/hiveos.db. Qdrant is optional — confirm it degrades to episodic-only if
QDRANT_URL is unset. Report. Do NOT edit SOUL.md or the approval gate.
```

### START PHASE 3 — Tools + Approval Gate
```
Implement & verify Phase 3. Confirm tools/registry.py runs safe tools immediately
(read_file, write_file, shell, web_get) and that dangerous tools or dangerous args
return status "pending_approval" via core/approval_gate.py. Test: call the "deploy"
tool -> must be gated. Call a benign read_file -> must run. Confirm every call is
written to data/audit.log. Confirm GET /approvals lists pending and
POST /approvals/decide executes on approve. Report. Never weaken the gate.
```

### START PHASE 4 — Autonomy (heartbeat + subagents)
```
Implement & verify Phase 4. Run `python -m core.orchestrator` and confirm: the
heartbeat fires, core/planner.py returns a JSON task list from goals in
config/goals.json, subagents execute via the registry with max 3 concurrent, any
dangerous task surfaces as an approval instead of executing, and memory.consolidate
runs each cycle. Lower HIVE_HEARTBEAT_SEC for the test. Report. Don't touch SOUL.md.
```

### START PHASE 5 — Voice
```
Implement & verify Phase 5 (skip if no audio device on this VM — then just lint it).
scripts/voice.py should: detect the wake word "hive", transcribe via faster-whisper,
send to the gateway /chat, and speak the reply via piper TTS. Add install notes to
README for faster-whisper and piper. Keep audio deps lazy-imported so the core runs
without them. Report.
```

### START PHASE 6 — Mission Control dashboard
```
Implement & verify Phase 6. In dashboard/: `npm install` then `npm run build`.
Confirm dashboard/MissionControl.jsx talks to the gateway: live online status,
chat panel, and an approval inbox that approves/rejects pending actions. Set
VITE_HIVE_GATEWAY and VITE_HIVE_TOKEN via dashboard/.env. For iPad preview, enable
GitHub Pages on the pushed branch. Report.
```

---

## Verification table

| Phase | Command | Pass condition |
|---|---|---|
| 0 | `python -m scripts.ping` | SOUL loads + reply |
| 1 | `uvicorn gateway.app:app ...` | /health ok, /chat replies, /ws streams |
| 2 | restart gateway | facts persist in data/hiveos.db |
| 3 | call deploy tool | returns pending_approval; safe tools run; audit.log grows |
| 4 | `python -m core.orchestrator` | heartbeat plans + runs; dangerous tasks gated |
| 5 | `python -m scripts.voice` | wake word → reply spoken |
| 6 | `npm run dev` | dashboard shows status + approvals |

## Non-negotiable safety rules (all phases)
- `config/SOUL.md` is immutable at runtime. Never edited by Hive or Claude Code.
- `core/approval_gate.py` is the danger firewall. Never bypassed or weakened.
- Every tool call is audited to `data/audit.log`.
