# HiveOS Centre — Final Sidebar IA (post-research)

> **Date:** 2026-06-30
> **Status:** Locked recommendation
> **Inputs:** Audit (98 endpoints / 13 pages), SH1 holographic mockup, research agent (Linear/Raycast/Stripe/Datadog/Vercel/Sentry/Temporal/Retool), Hive review

---

## TL;DR — the locked IA

```
HiveOS                       ⌘K
─────────────────────────────
💬 Chat                  ⌘1
👥 Kanban                ⌘2
🧠 Memory                ⌘3
⚡ Skills                ⌘4
⚠️  Approvals    [3]      ⌘5
📡 Activity              ⌘6
─────────────────────────────  ← divider (no label)
🔄 Pipeline              ⌘7
📚 Docs                  ⌘8
📌 Commitments           ⌘9
─────────────────────────────
◐ Idle · 14 agents · 2.1k tok  ← footer status
⚙ Settings          ⌘,        ← footer-pinned panel
```

**9 sidebar items.** No section labels — one divider line between "verbs on the AI" and "verbs on the system". Footer carries status + Settings (tri-split panel). ⌘K is in the header. Voice button lives **inside Chat** (not in sidebar).

Mobile bottom bar (5): **Chat, Kanban, Memory, Approvals, Activity**. Hamburger drawer for the rest.

---

## Why 9, not 13

Research landed on 13 (full audit) → pushed back to 9. Reasoning:

- **Linear:** 8 items + 1 settings → power-user ops console sweet spot
- **Vercel:** 6 items + avatar dropdown → too sparse for an operator tool
- **Datadog:** 20+ items → product switcher, not primary nav (wrong frame)
- **NN/g vertical nav + Miller's Law:** 7±2 items for primary nav, 13+ acceptable only for category browsers (where users scan, not act)
- **HiveOS use case:** one operator, one product, daily-driver workflow. Not a SaaS the user is browsing.

13 items caused: visual clutter, no hierarchy, no scan path, ~80px more chrome per render.

**What got cut from sidebar (and where it went):**

| Cut from sidebar | Went to |
|---|---|
| **Sessions** | Inside Activity page (sub-route `/activity/sessions`) — sessions are reference, not a destination |
| **Cron** | Inside Settings → System tab (cron is configuration, not daily ops) |
| **Tasks** | Inside Commitments page (sub-route `/commitments/tasks`) — operator naturally goes to "what's queued" once, then stays in commitments |
| **Team** | Avatar dropdown (post-v1.0) — single-tenant now |

**What stayed** (the 9):
- Chat, Kanban, Memory, Skills, Approvals — **daily operator verbs on the AI**
- Activity — **live observability** (used constantly when something goes wrong)
- Pipeline — **self-improve loop** (used once a day when reviewing what Hive did)
- Docs — **reference** (used when learning what's possible)
- Commitments — **everything that needs follow-up** (Tasks, Cron reminders, Promises — operator intent is "what needs me")

---

## Page specs (locked)

### Chat (`/`) — DEFAULT destination
- Full-width chat card. Composer bottom-sticky. Voice button in composer.
- Right rail (collapsible): MemoryPeek + SkillLauncher.
- Left rail (collapsible): session switcher (was missing from P-I).
- Open shortcut: ⌘1, or click anywhere on `/` when offline.

### Kanban (`/agents`)
- 5 columns (researcher / coder / reviewer / memory-keeper / security-reviewer).
- Live cards via WS `A2A_CALL_*` events.
- Card click → opens trace overlay (`/traces/{sid}`) in slide-over panel.
- Shortcut: ⌘2.

### Memory (`/memory`)
- Header stats: total / unread / last-consolidated / Obsidian N.
- 4 tabs: **Recent / Important / Topics / Sessions**.
- Action bar: Consolidate now, Export (JSONL), Wipe (confirm modal).
- MemoryPeek (P-I panel) collapses into this page when nav clicked.
- Shortcut: ⌘3.

### Skills (`/skills`)
- Filter chips: **Pinned / Active / Stale / Archived / All**.
- Card grid: name + description + use_count + pin/archive/detail.
- Add to P-I: archive action + detail modal.
- Shortcut: ⌘4.

### Approvals (`/approvals`) — Badge: count
- Inbox list. ApprovalModal opens as full-screen overlay (P-I) when card clicked.
- Top actions: **Approve all safe** (heuristic — only tier-low items), **Cancel all**, **Edits log** (slide-over).
- Pulsing badge when count > 0.
- Shortcut: ⌘5.

### Activity (`/activity`)
- Live tool-call stream (P-I ActivityFeed) as default tab.
- Sub-routes: `/activity/audit` (filterable table), `/activity/traces` (per-session drilldown), `/activity/events` (EventBus history), `/activity/loop-guard` (stats + reset button).
- Tab bar uses the same TabBar component as other multi-tab pages.
- Shortcut: ⌘6.

### Pipeline (`/pipeline`) — was "Self-improve"
- 5 tabs: **Verdicts / History / Pending edits / Run-tests / Learning loop**.
- Verdict list shows last 5 by default; History tab is paginated.
- Run-tests button → modal with pytest output (live as it streams).
- Learning loop tab: current state + history + "Run evaluation" button.
- Renamed from "Self-improve" to "Pipeline" — operators think in pipelines, not "self-improve" (Linear/Vercel never use internal-jargon labels).
- Shortcut: ⌘7.

### Docs (`/docs`)
- Left rail = file tree (CENTRE, ARCHITECTURE, LEARNING, SOUL, STATUS, API).
- Right pane = rendered markdown with prose theme.
- Search bar (in-file Ctrl-F pattern, not global search — KISS).
- Bundled at build time via Vite glob import.
- Shortcut: ⌘8.

### Commitments (`/commitments`) — combined surface
- 3 tabs: **Tasks / Cron / Promises**.
- Tasks tab: durability queue with retry/cancel/bulk (full CRUD).
- Cron tab: scheduler with cron expression picker.
- Promises tab: recurring commitments with fulfill/edit/delete.
- "What's overdue" pulsing badge in the sidebar entry.
- Shortcut: ⌘9.

---

## Footer (replaces section labels)

```
◐ Idle · 14 agents · 2.1k tok
⚙ Settings          ⌘,
```

- **Status line:** read from `/health/summary`. Idle/Working dot + agent count + token rate (from `/telemetry` last-minute delta).
- **Settings:** footer-pinned link to tri-split panel (`/settings/personal`, `/settings/account`, `/settings/system`). Not in main nav — it's structural, not a verb.

**Why footer not avatar dropdown:** Settings has 3 structural axes (Personal / Account / System) that need their own routes. Avatar dropdowns collapse Settings into one flat list. Footer-pinned panel beats dropdown when Settings has internal structure.

---

## Header (single line)

```
[◈ HiveOS]                              [⌘K search…]     [🔔 2]  [H]
```

- Brand mark + ⌘K palette trigger (Raycast pattern).
- Notification bell (amber badge when approvals pending + budget warnings).
- Avatar dropdown (H = Hive operator profile; settings lives in footer, NOT in dropdown).

**Why ⌘K in header:** Raycast made this canonical. It's how operators navigate when they know what they want — type → enter. Sidebar is for browsing; ⌘K is for going.

---

## Voice button — moved INTO Chat

Voice button is **not in sidebar** (my earlier proposal). Reasoning:

- Voice → chat. Always. No other voice interaction makes sense in this product.
- Putting it at the bottom of sidebar breaks the sidebar's job (navigation).
- Better: Voice button sits inside Chat composer, top-right of input. Always visible when in Chat.
- On mobile: Voice button is the rightmost icon in the bottom peek-bar (only when on Chat route). Otherwise hidden.

---

## Mobile bottom peek-bar (5 icons)

| Position | Icon | Route |
|---|---|---|
| 1 | 💬 Chat | `/` |
| 2 | 👥 Kanban | `/agents` |
| 3 | 🧠 Memory | `/memory` |
| 4 | ⚠️ Approvals | `/approvals` (amber badge if >0) |
| 5 | 📡 Activity | `/activity` (pulse dot if live events) |

Hamburger (top-left) opens full sidebar drawer with all 9 items + status + settings.

---

## ⌘K palette — search everything

Indexed:
- All 9 routes (jump to page)
- All sessions (jump to conversation by title)
- All pinned skills (launch skill)
- All recent approvals (jump to approval)
- All overdue commitments (jump to commitment)
- Commands: "run tests", "consolidate memory", "open trace {session_id}", etc.

Keyboard-first navigation. ⌘K from anywhere → palette → type → enter.

---

## What this IA explicitly does NOT have

1. **No Home dashboard page** — the footer status line IS the home. Operators glance at it once and click through to what they need.
2. **No "More" menu** — extra click was rejected. Everything reachable in ≤ 1 tap.
3. **No category labels in sidebar** — the divider line communicates grouping; items are already grouped by verb (operator intent).
4. **No Team nav slot** — single-tenant today, post-v1.0. Lives in avatar dropdown.
5. **No Settings in main nav** — footer-pinned. Settings is structural, not a verb.
6. **No Sessions as a destination** — sessions are reference, browse them from Activity → sessions tab.

---

## Open decisions for Kamil

1. **Pipeline rename** — "Self-improve" → "Pipeline" matches operator mental model. OK with you?
2. **Commitments merged surface** — Tasks + Cron + Promises under one tab. OK, or split Cron back out?
3. **Status line content** — "Idle · 14 agents · 2.1k tok". Is "14 agents" meaningful, or replace with something else (sessions today / approvals pending)?
4. **Footer tri-split Settings** — start with Personal/Account/System, or wait until System tab has content?

---

## Sources

- `/home/hive/hiveos/docs/UI_PLAN.md` — full audit
- `/home/hive/hiveos/screenshots/frontend/mockups/new mockups/SH1-full-holo-sidebar.html` — visual lock
- Background research: NN/g vertical nav + Miller's Law, Linear Method, Raycast manual, Stripe Dashboard docs, Sentry Explore, Vercel Accounts
- [[hiveos-design-style]] — visual language