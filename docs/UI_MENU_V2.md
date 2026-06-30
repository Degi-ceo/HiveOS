# HiveOS Centre — UI Plan v2 (own architecture)

> **Date:** 2026-06-30
> **Status:** Final v2.1 — added Hub/Home as default `/`
> **Locked style:** SH1 holographic (deep navy + cyan conic + glass cards)
> **Replaces:** `docs/UI_MENU_FINAL.md` (v1, 9 items)
> **Inputs:** OpenClaw audit (`research-openclaw.md`), OpenJarvis audit, Hermes reference, `docs/UI_PLAN.md` (98-endpoint backend audit)
> **v2.1 changes:** Hub added as default `/`; Chat moved to `/chat`; 17 sidebar items in 4 groups + Hub top-slot.

---

## Relationship to `docs/UI_PLAN.md`

UI_MENU_V2 and UI_PLAN are **two views of the same surface**, not competing specs:

- **`docs/UI_MENU_V2.md` (this file) — canonical for IA decisions:**
  - Sidebar tree (Hub top-slot + WORK / RUN / WATCH / TUNE groups, 17 items)
  - Keyboard shortcuts (§KEYBOARD — ⌘H, ⌘1–⌘9, ⌘0, ⌘I, ⌘A, ⌘D, ⌘⇧S, ⌘⇧A, ⌘, + ⌘K palette)
  - URL map (§URL MAP — final)
  - Visual lock (SH1 holographic, conic-gradient borders, system stack)
- **`docs/UI_PLAN.md` — canonical for page-coverage + endpoint-binding decisions:**
  - 14-page inventory (Chat, Memory, Skills, Kanban, Activity, Self-improve, Approvals, Sessions, Cron, Commitments, Tasks, Docs, Settings, Team)
  - Per-page endpoint mappings
  - Backend-gap table (what's missing before each page can render)
  - Sprint 7 backlog (11 PRs to cover all pages)

When in doubt about **what page should exist and what data it shows**: read UI_PLAN §2 / §3.
When in doubt about **where it sits in the sidebar or what its keyboard shortcut is**: read this file (UI_MENU_V2).

The UI_PLAN §1 ASCII sidebar tree is a legacy MAIN/LIVE/WORKSPACE sketch and is **NOT canonical** — when it disagrees with this file, this file wins.

---

---

## Design principles

Drawn from research, not copied:

1. **Operator-first, not user-first.** HiveOS is a single-operator console. Every sidebar item is a verb the operator performs, not a category the operator browses.
2. **4 sidebar groups** (Work / Run / Watch / Tune). Inspired by OpenClaw's grouping pattern (4 groups: chat/control/agent/settings) — but renamed for HiveOS semantics. Each group is **collapsible** with a chevron (OpenClaw pattern).
3. **17 items total: 1 Hub top-slot + 4 groups × 3–5 items.** Below the 7±2 per group ceiling.
4. **⌘K palette is primary navigation**, sidebar is for discovery. Both first-class.
5. **Settings is a tri-split panel** (Personal / Account / System), footer-pinned OR sidebar — one canonical entry.
6. **Hub IS `/`.** Hermes's glance layer is correct — operators want to see status cards, recent activity, quick actions on first paint. Hub complements (does not replace) Activity: Hub = snapshot now, Activity = stream of events.
7. **No "Profile Builder"** — single operator, single profile in v1.0.
8. **No "Models"** — LLM pool lives in Settings → System.
9. **No "Profiles" / "Agents" separate** — Agents is the right name (5 named sub-agents, not "Profiles").
10. **No "Webhooks" separate from Channels** — webhooks are a sub-tab of Channels.
11. **No "Pairing" in main nav** — companion device count in footer; full page accessible from avatar dropdown.
12. **No "Plugins"** — disabled for v1.0, deferred to v2.0.
13. **Footer carries status, NOT settings.** Settings has a canonical sidebar entry; the footer just carries voice/state.

---

## SIDEBAR — 17 items: Hub top-slot + 4 groups

```
[◈ HiveOS]                              [⌘K]   [🔔]  [H]
─────────────────────────────────────────────────────
🏠 Hub                       ⌘H        (default /)
─────────────────────────────────────────────────────
⌄ WORK                              (codzienne użycie)
   💬 Chat                ⌘1
   🧠 Memory              ⌘2    [8 unread]
   ⚡ Skills              ⌘3
   📂 Files               ⌘4

⌄ RUN                                (system/operations)
   🤖 Agents              ⌘5    [3 running]
   ✅ Tasks               ⌘6    [12 pending]
   📨 Channels            ⌘7
   🔌 MCP                 ⌘8
   🖥 Logs                ⌘9

⌄ WATCH                              (observability)
   📡 Activity            ⌘0    ● live
   📁 Sessions            ⌘⇧S
   ⚠️ Approvals    [3]    ⌘⇧A   [3 pending]

⌄ TUNE                                (meta/config)
   🔄 Self-improve        ⌘I
   📊 Analytics           ⌘A
   📚 Docs                ⌘D
   ⚙ Settings             ⌘,
─────────────────────────────────────────────────────
◐ Idle · 14 agents · 2.1k tok
🔗 3 paired    🎙
```

**Visual rule** (SH1 holographic):
- Active item: 3px vertical gradient bar (cyan→blue→violet) on left, cyan text glow
- **Hub top-slot** sits above the 4 groups, separated by full-width divider (its own visual band — "land here first")
- Group labels: uppercase 11px tracking, dividers between groups
- Chevrons rotate 90° on group collapse
- Badges: numeric amber for pending, pulsing cyan for unread, pulsing red for live

---

## Item-by-item spec

### TOP-SLOT — Hub (default `/`)

#### 🏠 Hub — `⌘H` (default `/`)
**Snapshot of the whole system on first paint.** Operators glance at it once and click through to what they need. Hermes pattern (their `/`), confirmed by Kamil's request.

**Status cards row (4-up, glass cards with gradient borders):**

| Card | Source | Refresh |
|---|---|---|
| 🟢 **Gateway** — uptime, restart count, active connections | `GET /health/summary` + `/heartbeat` | 5s |
| 🧠 **Memory** — facts count, unread, last consolidated | `GET /memory/stats` | 30s |
| 🤖 **Agents** — alive/zombie count, active tasks | `GET /agents/board` | 10s |
| ⚠️ **Approvals** — pending count, oldest waiting | `GET /approvals` | 10s |

**Quick actions row (5 chips):**
- ➕ New task → opens Tasks → Kanban with create dialog
- 💬 New chat → jumps to Chat (clears session)
- 🔄 Restart gateway → confirm modal → `POST /gateway/restart`
- 📋 Run tests → jumps to Self-improve → Run-tests → `POST /run-tests`
- 🧠 Consolidate memory → confirm modal → `POST /memory/{sid}/consolidate`

**Recent activity feed (last 20 events, mini-ActivityFeed component):**
- New session · task complete · approval requested · cron fired · error · webhook received
- Click row → opens detail (slide-over)
- Filter chips: All · Errors · Approvals · Tasks

**Platform connections row (5 pills):**
- Telegram · Discord · Slack · Email · Tailscale (each = connected/disconnected)
- Click → Channels page (auto-scroll to that platform tab)

**Active sessions preview (top 3):**
- Session ID · model · last activity timestamp · token sparkline (last 60s)
- Click → opens Chat with session loaded

**Voice button** (top-right corner of Hub card):
- Click → activates voice mode → routes to Chat with voice composer active
- Pulse animation when listening

**Header (Hub-specific):**
- 👋 greeting ("Good morning, Kamil" — uses Settings → Personal → display name)
- 🕐 current local time (from Settings → Personal → timezone)
- 🔄 "Refresh all" button (manual override of auto-refresh)

---

### GROUP 1 — WORK (codzienne użycie)

#### 💬 Chat — `⌘1` (`/chat`)
Threaded conversation + compose. The 80% use case.

**Layout (3-pane, all collapsible):**
- Left rail: conversation list (with rename inline + auto-title button)
- Centre: conversation thread with tool-call chips inline
- Right rail: MemoryPeek + SkillLauncher

**Header actions:** new chat · search sessions · auto-title · rename inline · voice

**Composer (bottom-sticky):**
- Multi-line input with placeholder cycle
- Voice button (in composer — never in sidebar)
- File attach (uses Files browser picker)
- Slash popover: `:memory`, `:skills`, `:agents`, `:tasks` (Hermes pattern)

---

#### 🧠 Memory — `⌘2` (badge: unread facts)
Knowledge base (Mnemosyne + Obsidian).

| Tab | Content | Endpoint |
|---|---|---|
| **Recent** | latest facts | `GET /memory/stats` |
| **Important** | high-importance | `GET /memory/important` |
| **Topics** | clustered | `GET /memory/topics` |
| **Sessions** | per-session count | `GET /memory/session/{sid}/count` |

**Header stats:** total facts · unread (pulsing cyan) · last consolidated · Obsidian notes (N)

**Action bar:** 🔄 Consolidate now · 📤 Export JSONL · 🗑 Wipe (confirm modal)

---

#### ⚡ Skills — `⌘3`
Pinned + library.

**Tabs:** 📌 Pinned · 🟢 Active · 🟡 Stale (30d unused) · 📦 Archived · All

**Card actions:** Pin/Unpin · Archive · View detail (modal) · Copy name

**Discovery wizard** (top-right): "Add capability" → modal → AST-first lookup → result card → pin to grid.

---

#### 📂 Files — `⌘4`
Workspace file browser.

**Layout:** tree (left) + preview (right)

**Root:** `~/.hiveos/` (configurable in Settings → Personal)

**Operations:** Upload (drag-drop) · Download · Preview (text/image/PDF/markdown) · Edit in-place · Copy path

**Why this is in WORK:** operators edit configs daily. This is operator-on-the-system, not config-of-the-system.

---

### GROUP 2 — RUN (system/operations)

#### 🤖 Agents — `⌘5` (badge: running count)
Named sub-agent registry (5 named agents: researcher / coder / reviewer / memory-keeper / security-reviewer).

**Views:**
- **Active Now** (default) — live cards with current task/goal
- **All Agents** — full list with state
- **By Type** — grouped

**Agent Detail (`/agents/:agentId`):**
- Current task / goal
- Tool call history
- Performance metrics (latency, success rate, step count)
- Resource usage
- Live log feed (stdout/stderr)
- Restart / Stop / Pause controls

**Cards move live via WS `A2A_CALL_*` events.**

---

#### ✅ Tasks — `⌘6` (badge: pending queue count)
Durable task queue + Kanban (Hermes-aligned naming — tasks are the canonical work items).

| Sub-tab | Content | Endpoint |
|---|---|---|
| **📋 Kanban** (default) | Backlog / In Progress / Review / Done / Blocked | `GET /tasks` + `/tasks/by-kind` |
| **⏰ Cron** | scheduled jobs | `GET /cron` + full CRUD |
| **🎯 Promises** | recurring commitments | `GET /commitments` + full CRUD |

**Why grouped:** all three are "scheduled + queued" things the operator waits on. Hermes splits these — I group them because operator intent is one ("what's outstanding").

**Bulk actions:** Retry all failed · Cancel running · Requeue

---

#### 📨 Channels — `⌘7` (pulse if webhook hit)
Per-channel management (Telegram · Discord · Slack · Email · Webhooks).

**Per channel:**
- Connection status (live)
- Last message timestamp
- Chat ID mapping
- 🔔 Test message button
- 🔧 Per-channel settings

**Webhooks sub-tab:** registered HTTP callbacks · create · delivery log · retry

**Why one page:** all communication ingress is one operator intent.

---

#### 🔌 MCP — `⌘8` (badge: server count)
Model Context Protocol servers.

**Tabs:** Servers · Tools · Health

**Per server:** name · command · status · tools · add/remove

---

#### 🖥 Logs — `⌘9`
Centralized log viewer (Hermes-aligned — separated from Activity).

**Source tabs:** Gateway · Agents · System · Self-improve

**Filters:** level (DEBUG/INFO/WARN/ERROR) · time range · free-text regex · source

**Controls:** Auto-scroll + pause · Download .log/.json · Search

**Tail mode:** real-time SSE stream with WS indicator.

**Why separate from Activity:** Activity = business events (tool calls, approvals). Logs = system events (errors, stack traces, gateway lifecycle). Different audiences.

---

### GROUP 3 — WATCH (observability)

#### 📡 Activity — `⌘0` (pulsing dot if live events)
Live business event stream.

| Tab | Content | Endpoint |
|---|---|---|
| **● Live** (default) | streaming tool-call log | WS `tool_call_*` |
| **📋 Audit** | filterable table | `GET /audit?limit=200` + `/audit/search` |
| **🔍 Traces** | per-session drilldown | `GET /traces/{sid}` |
| **📨 Events** | EventBus history | `GET /events/history` + `/events/stats` |
| **🔁 Loop-guard** | stats + reset | `/loop-guard/stats` + `POST /loop-guard/reset` |

**Audit filters:** tool name · status · approved · time range · free text. Export CSV.

---

#### 📁 Sessions — `⌘⇧S`
LLM session history (separated from Memory — different concept).

| Tab | Content |
|---|---|
| **All** | full paginated list |
| **By Model** | filter by model (`/v1/models`) |
| **By Date** | today / week / month |
| **Errors Only** | failed sessions |

**Columns:** session_id · timestamp · duration · model · input/output tokens · cost · provider · trace link

**Row actions:** open in Chat · view trace · delete · export

---

#### ⚠️ Approvals — `⌘⇧A` (badge: pending count — unique to HiveOS)
Danger firewall inbox.

**Header actions:**
- ✅ Approve all safe (tier-LOW only)
- 🚫 Cancel all (confirm)
- 📜 Edits log (slide-over)

**Cards:** tool + args preview + reason + tier badge · click → ApprovalModal full-screen overlay

**WS push:** `APPROVAL_REQUESTED` → toast in any view + sidebar badge bump.

---

### GROUP 4 — TUNE (meta/config)

#### 🔄 Self-improve — `⌘I` (pulse if live)
Code auto-modification pipeline.

| Tab | Content |
|---|---|
| **Verdicts** (default) | last 5 outcomes (AUTO/PR · REVIEW/pending · MANUAL/logged · PROTECTED/refused) |
| **History** | paginated full history |
| **Pending edits** | diffs awaiting approve |
| **🧪 Run-tests** | isolated test suite runner |
| **📈 Learning** | eval-gated loop |

---

#### 📊 Analytics — `⌘A`
Cost + usage dashboards (charts).

**Sub-tabs:** Cost · Tokens · Sessions · Skill usage · Error rate

**Charts (deferred to post-v1.0 — show simple tables in v1.0):**
- Cost over time (line)
- Cost per model (bar)
- Token distribution (pie)
- Top 10 most expensive sessions (table)

**Why separate from Activity:** Activity = events streaming. Analytics = aggregated retrospective. Different cadence.

---

#### 📚 Docs — `⌘D`
Bundled markdown reference.

**Files:** CENTRE · ARCHITECTURE · LEARNING · STATUS · API · SECURITY · SOUL🔒 · DANGEROUS_TOOLS🔒

**Layout:** file tree (left) + rendered markdown (right). In-pane Ctrl-F.

---

#### ⚙ Settings — `⌘,`
Tri-split panel (Personal / Account / System).

**Personal:** display name · timezone · voice language · notifications · Files root path
**Account:** API token · OpenAI-compat connection · MCP server list (read from MCP)
**System:** LLM pool · Model catalog · Channels · Config summary + validate · 🛡 DANGEROUS_TOOLS (read-only) · 🔒 SOUL.md (read-only) · 🌍 ENV editor

**Pattern borrowed from OpenClaw:** Quick Settings card grid (top of page) with explicit "go to" buttons that deep-link into Account/System tabs.

---

## FOOTER

```
◐ Idle · 14 agents · 2.1k tok
🔗 3 paired    🎙
```

- **Status line:** click → slide-over with per-component health
- **Pairing count:** click → Pairing page (companion device QR + revoke)
- **Voice button:** click → activates voice mode (route to Chat if not on Chat)

---

## HEADER

```
[◈ HiveOS]              [⌘K search…]           [🔔 2]  [H]
```

### ⌘K palette (Raycast-style, primary navigation)

**Indexed:**
- 17 sidebar routes (jump to page) — including Hub
- All sessions (jump by title)
- All pinned skills (launch)
- All pending approvals (jump to approval)
- All overdue tasks (jump to task)
- All unread memory facts (jump to memory)
- All paired devices (jump to Pairing)

**Commands:**
- `go home` → Hub
- `run tests` → Self-improve → Run-tests tab
- `consolidate memory` → triggers consolidate
- `open trace {session_id}` → Activity → Traces
- `restart gateway` → confirm modal
- `approve all` → Approvals → approve tier-LOW
- `wipe memory` → confirm modal
- `new chat` → Chat (clear)
- `settings {tab}` → Settings jump (personal/account/system)
- `pair device` → Pairing flow
- `clear logs` → confirm modal

### 🔔 Notifications bell
- Approval pending (count)
- Budget warning (count)
- Self-improve complete (count)
- Cron failed (count)
- Test failure (count)
- Webhook received (count)
- Pairing request (count)

### [H] Avatar dropdown
- Profile name + role
- Theme (holographic only — v1.0)
- About Hive → Docs · SOUL.md
- Pairing → Pairing page
- Sign out (post-multi-user)

---

## MODAL OVERLAYS (unified pattern)

All modals use `<dialog>` primitive + ESC + backdrop + focus trap (OpenClaw pattern).

- **Skill Editor** — edit built-in skill
- **Model Picker** — choose model dropdown
- **Confirm Dialog** — destructive actions (wipe, cancel-all, requeue)
- **Slash Popover** — in-chat commands
- **Cron Expression Builder** — visual cron picker
- **Task Quick-create wizard** — multi-step form
- **Trace Overlay** — slide-over from any page (when trace_id in URL)
- **Approval Modal** — full-screen arg viewer with diff
- **Session Detail** — slide-over from Sessions/Tasks
- **Pairing QR** — companion device QR
- **Pairing Request Confirm** — accept/revoke

---

## MOBILE BOTTOM PEEK-BAR (5 icons, 64px)

| Pos | Icon | Route |
|---|---|---|
| 1 | 🏠 Hub | `/` |
| 2 | 💬 Chat | `/chat` |
| 3 | ✅ Tasks | `/tasks` |
| 4 | 📡 Activity | `/activity` |
| 5 | ⚙ Settings | `/settings` |

Hamburger opens full sidebar drawer with all 17 items + Hub top-slot + 4 collapsible groups.
🔍 Search icon in header opens full-screen ⌘K palette.
Voice button visible only on Chat route (in composer).

---

## KEYBOARD

### Global
- `⌘H` — Hub (default `/`)
- `⌘1`–`⌘9`, `⌘0` — sidebar items in WORK + RUN groups
- `⌘⇧S` — Sessions
- `⌘⇧A` — Approvals
- `⌘I` — Self-improve
- `⌘A` — Analytics
- `⌘D` — Docs
- `⌘,` — Settings
- `⌘K` — palette
- `Esc` — close modal/palette/drawer

### Chat
- `⌘N` — new chat
- `⌘⇧T` — run tests (jump to Self-improve → Run-tests)
- `⌘⇧V` — toggle voice input

---

## URL MAP (final)

```
/                           → Hub (default — NEW v2.1)
/chat                       → Chat (moved from / — NEW v2.1)
/chat/:sessionId            → Chat with session loaded
/memory                     → Memory · Recent
/memory?tab=important       → Memory · Important
/memory?tab=topics          → Memory · Topics
/memory?tab=sessions        → Memory · Sessions
/skills                     → Skills · Pinned
/skills?tab=active          → Skills · Active
/skills?tab=stale           → Skills · Stale
/skills?tab=archived        → Skills · Archived
/files                      → Files (workspace)
/agents                     → Agents · Active Now
/agents?view=all            → Agents · All
/agents?view=type           → Agents · By Type
/agents/:agentId            → Agent Detail
/tasks                      → Tasks · Kanban
/tasks?tab=cron             → Tasks · Cron
/tasks?tab=promises         → Tasks · Promises
/channels                   → Channels · Telegram
/channels/:platform         → Channels · specific platform
/channels?view=webhooks     → Channels · Webhooks
/mcp                        → MCP · Servers
/mcp?view=tools             → MCP · Tools
/mcp?view=health            → MCP · Health
/logs                       → Logs · Gateway
/logs?src=agents            → Logs · Agents
/logs?src=system            → Logs · System
/logs?src=self              → Logs · Self-improve
/activity                   → Activity · Live
/activity?tab=audit         → Activity · Audit
/activity?tab=traces        → Activity · Traces
/activity?tab=events        → Activity · Events
/activity?tab=loop-guard    → Activity · Loop-guard
/sessions                   → Sessions · All
/sessions?tab=model         → Sessions · By Model
/sessions?tab=date          → Sessions · By Date
/sessions?tab=errors        → Sessions · Errors
/approvals                  → Approvals inbox
/approvals/:id              → Approval detail modal
/self-improve               → Self-improve · Verdicts
/self-improve?tab=history   → Self-improve · History
/self-improve?tab=pending   → Self-improve · Pending edits
/self-improve?tab=tests     → Self-improve · Run-tests
/self-improve?tab=learning  → Self-improve · Learning
/analytics                  → Analytics · Cost
/analytics?view=tokens      → Analytics · Tokens
/analytics?view=sessions    → Analytics · Sessions
/analytics?view=skills      → Analytics · Skills
/analytics?view=errors      → Analytics · Errors
/docs                       → Docs · file tree
/docs/:filename             → Docs · specific file
/settings                   → Settings · Personal
/settings?tab=account       → Settings · Account
/settings?tab=system        → Settings · System
/pairing                    → Pairing (from footer or avatar)
/traces/:sessionId          → Trace overlay (any page)
/a2a                        → A2A RPC console (power-user, ⌘⇧R)
/env                        → Settings → System → Env editor (deep link)
```

---

## KEY DIFFERENCES vs THE THREE REFERENCES

| Hermes (chat-first) | OpenClaw (operator-first) | OpenJarvis (flat desktop) | **HiveOS v2.1 (this)** |
|---|---|---|---|
| 22 routes | 4 groups · 23 items | 7 items flat | **Hub + 4 groups · 17 items** |
| Hub as `/` | Chat as `/` | Chat as `/` | **Hub as `/` (NEW v2.1)** |
| Models separate page | Models under ai-agents | Models in ⌘K palette | **Models under Settings → System** |
| Pairing as own nav | Nodes as own nav | No pairing | **Pairing in footer count + avatar** |
| Plugins marketplace | Plugins under automation | No plugins | **Plugins deferred to v2.0** |
| Webhooks separate | Webhooks scoped under automation | No webhooks | **Webhooks under Channels** |
| Analytics as own page | Usage as own page | No analytics | **Analytics as own page (deferred charts)** |
| Profile Builder wizard | Agent editor | No profiles | **Single profile, no wizard v1.0** |
| Logs as own page | Logs as own page | Logs as own page | **Logs as own page** |
| Sessions separate | Sessions separate | (conversations in nav) | **Sessions separate** |
| Approvals (own) | Exec-approvals (settings tab) | No approvals | **Approvals as own nav** |
| Settings = 10 split tabs | Settings = 10 split tabs | Settings = long scroll | **Settings = 3-tab panel + Quick/Advanced** |
| Mobile bottom peek (5) | No bottom bar (drawer <1100px) | Hamburger drawer | **Mobile bottom peek (5)** |

---

## WHY 4 GROUPS + HUB TOP-SLOT, NOT FLAT

A flat 17-item sidebar violates NN/g's "group related items" principle. Operators scanning 17 items waste cognitive load matching which one is which.

The **Hub top-slot + 4-group** structure matches **operator cognitive flow**:

- **Hub** = "What's the state of the world right now" (cognitive load: zero, every session-start)
- **WORK** = "I'm using the system" (cognitive load: low, frequent)
- **RUN** = "the system is doing things" (cognitive load: medium, frequent)
- **WATCH** = "what happened / is happening" (cognitive load: medium, when troubleshooting)
- **TUNE** = "I want to change how the system behaves" (cognitive load: high, infrequent)

Hub is visually separated from the 4 groups (full-width divider above) so the eye lands on it first. Hub is also where voice lives (top-right corner of the Hub card) — voice is a system-wide action that needs a global anchor.

Each group has its own color accent (cyan / blue / violet / emerald) to reinforce semantic separation without adding chrome.

---

## Open decisions for Kamil

1. **4 groups + Hub top-slot vs flat 17** — I chose grouped. OpenClaw confirms 4-group pattern works. Confirm.
2. **Files in WORK** — operators need file access daily. Move to TUNE if you see it as config-only.
3. **Analytics as own page** vs sub-tab of Self-improve — I split them. Activity ≠ Analytics (events vs aggregates).
4. **Tasks merges Kanban + Cron + Promises** — Hermes has them separate, I merge. Confirm.
5. **Channels merges Telegram/Discord/Slack/Email/Webhooks** — single operator intent (communication ingress).
6. **Mobile peek-bar 5 icons** — Hub / Chat / Tasks / Activity / Settings. Confirm.
7. **Voice button location** — Hub card top-right + Chat composer. Pick one canonical home.
8. **Hub content** — do you want all of: status cards + quick actions + recent feed + connections pills + active sessions? Or trim some for v1.0?

---

## Sources

- `docs/UI_PLAN.md` — page inventory + endpoint-coverage (113 routes verified in `src/hive/gateway/app.py`)
- `docs/UI_MENU_FINAL.md` — v1 (9 items, superseded)
- `screenshots/frontend/mockups/new mockups/SH1-full-holo-sidebar.html` — visual lock
- `screenshots/hermes examples/hermes-dashboard-ui-overview.md` — Hermes reference (Hub pattern source)
- `research-openclaw.md` — OpenClaw audit (operator-first, 4-group pattern, settings split-view, quick/advanced config)
- OpenJarvis `frontend/src/App.tsx`, `Sidebar.tsx` — flat nav, model badge, conversation list pattern
- [[hiveos-design-style]] — visual language

---

## Change log

- **v2.1 (2026-06-30)** — Added Hub as default `/`. 17-item sidebar: Hub top-slot + 4 groups. Chat moved from `/` to `/chat`. Hub includes status cards (Gateway/Memory/Agents/Approvals), 5 quick actions, recent activity feed, platform connection pills, top-3 active sessions, voice button top-right, greeting + local time header.
- **v2.0 (2026-06-30)** — Initial design after deep audit of Hermes + OpenClaw + OpenJarvis. 16 items in 4 groups. Operator-first cognitive flow (WORK/RUN/WATCH/TUNE). No Hermes copy-paste.