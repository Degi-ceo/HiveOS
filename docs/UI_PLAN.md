# HiveOS Centre — Full UI Plan

> **Status:** Canonical **page-inventory + endpoint-coverage** plan, derived from deep audit.
> **Locked sidebar style:** SH1 (full 260px holographic sidebar + mobile bottom peek).
> **Author:** Hive (audit + subagent pass on 2026-06-30).
> **Audience:** P-I implementation plan (Centre.jsx) and **post-P-I backlog** (anything not in P-I).
>
> **Reconciliation note:** UI_PLAN catalogs **which pages must exist and what endpoints they hit**. It does **not** lock the IA grouping (sidebar tree, keyboard shortcuts) — those are locked in **`docs/UI_MENU_V2.md`** (v2.1: Hub top-slot + WORK/RUN/WATCH/TUNE groups, ⌘K palette primary nav, ⌘H Hub / ⌘1–⌘9, ⌘0 shortcuts). When UI_PLAN §1 below differs from UI_MENU_V2 SIDEBAR / KEYBOARD, **UI_MENU_V2 wins**. UI_PLAN §2 (page inventory) and §3 (page specs) remain canonical for coverage.

## Inputs this plan was derived from

1. **Backend capability audit** — **113 routes registered** in `src/hive/gateway/app.py` (111 HTTP + 2 WS) across 20+ functional groups, plus 4 conditional channel webhooks (Telegram / Slack / Discord / Email — registered only when the channel is enabled in `runtime.py`). The 98-route number was an earlier audit snapshot; the current count is verifiable via `grep -cE "^\s*@app\.(get|post|put|patch|delete|websocket)\(" src/hive/gateway/app.py`. Sources enumerated: `docs/API.md`, `docs/ARCHITECTURE.md`, `docs/STATUS.md`, `docs/sprints/SPRINT_6_AUTONOMY_LIB.md`, `src/hive/gateway/app.py`, and `src/hive/runtime.py`.
2. **MissionControl.jsx (legacy)** — single-file React mount, 2 tabs (Agents, Ops), 7 panels. **Coverage today: ~25 % of backend capability has UI surface**.
3. **SPRINT_6 P-I research plan** (`docs/sprints/SPRINT_6_P_I_RESEARCH.md`) — 15 net-new frontend files (Centre.jsx + 12 components + 3 hooks + theme). **P-I adds 6 new components but covers ~50 % of capability surface**.
4. **SH1 mockup** (this session, `screenshots/frontend/mockups/new mockups/SH1-full-holo-sidebar.html`) — holographic design system + sidebar nav structure.

---

## 1. Canonical navigation tree (sidebar IA)

> **Canonical IA is in `docs/UI_MENU_V2.md` SIDEBAR + KEYBOARD sections.** The ASCII tree below is a legacy MAIN/LIVE/WORKSPACE sketch kept for reference — UI_MENU_V2 v2.1 (Hub top-slot + WORK/RUN/WATCH/TUNE groups, 17 items) is the **canonical sidebar lock**. Page numbers (1–14) refer to the **§2 page inventory** below, NOT to sidebar position. Each UI_MENU_V2 sidebar item maps to one or more pages from §2.

```
┌─────────────────────────────────────┐
│  [conic H]   HIVEOS                 │   brand
│              CENTRE                 │
├─────────────────────────────────────┤
│  ● gateway  · ok                    │   status block
│  ● model     · M3                   │   (read from
│  ● planner   · ok                   │    /health/summary)
├─────────────────────────────────────┤
│  MAIN                               │
│   ◉ Chat                  ⌘1        │   → page 1
│   ⊙ Memory                ⌘2  [8]   │   → page 2
│   ⊙ Skills                ⌘3        │   → page 3
│   ⊙ Kanban                ⌘4  [3]   │   → page 4 (preserved from P-G)
├─────────────────────────────────────┤
│  LIVE                               │
│   ◉ Activity              ⌘5  ●     │   → page 5 (pulsing live dot)
│   ⊙ Self-improve          ⌘6        │   → page 6
│   ⊙ Approvals             ⌘7  [1]   │   → page 7 (amber badge when pending)
├─────────────────────────────────────┤
│  WORKSPACE                          │
│   ⊙ Sessions              ⌘8        │   → page 8 (NEW — was missing entirely)
│   ⊙ Cron                  ⌘9        │   → page 9 (NEW)
│   ⊙ Commitments           ⌘0        │   → page 10 (NEW)
│   ⊙ Tasks                 ⌘T        │   → page 11 (NEW — regression: was in legacy)
│   ⊙ Docs                  ⌘D        │   → page 12 (NEW)
│   ⊙ Settings              ⌘,        │   → page 13 (NEW)
├─────────────────────────────────────┤
│  ⊙ Team                          ⌘⇧T│   → page 14 (NEW)
├─────────────────────────────────────┤
│  🎙 VOICE INPUT                     │   bottom action
└─────────────────────────────────────┘
```

**Mobile (peek-bar pattern):** sidebar collapses into a 64px bottom peek-bar showing 5 icons (Chat / Memory / Approvals / Activity / Settings). Tap a peek icon → push drawer with full sidebar list. Voice button stays in the drawer.

**Active indicator:** 3px vertical bar on left of the active item, gradient cyan→violet with cyan glow (`box-shadow: 0 0 8px #22d3ee`). Selected text glows cyan.

**Badges:** numerically-pinned for unread (Memory count = `/memory/stats.unread`; Approvals count = `/approvals` length); "live" pulsing dot for Activity/Self-improve when WS emits events.

**Voice button location:** locked in **`docs/UI_MENU_V2.md` §"Voice button location"** decision — Hub card top-right + Chat composer only. **NEVER in the sidebar bottom** (the ASCII sketch above is the legacy location; final implementation follows UI_MENU_V2).

---

## 2. Page inventory (13 pages + 4 sub-pages)

| # | Page | Path in app | Primary endpoint(s) | WS events | P-I status |
|---|---|---|---|---|---|
| 1 | **Chat** | `/` | `POST /chat/stream/iterations` (SSE), `GET /sessions` | tokens, tool_call_*, approval_* | ✅ **P-I** ChatCenter |
| 2 | **Memory** | `/memory` | `GET /memory/stats`, `/memory/important`, `/memory/topics`, `POST /memory/{sid}/consolidate`, `GET /memory/export` | MEMORY_STORE, MEMORY_RETRIEVE | 🟡 partial — P-I has MemoryPeek (panel) only. Full page is **post-P-I gap** |
| 3 | **Skills** | `/skills` | `GET /skills`, `/skills/{name}`, `POST /skills/{name}/pin|state` | — | ✅ **P-I** SkillLauncher; ⚠️ archive action **missing from P-I** |
| 4 | **Kanban** | `/agents` | `GET /agents/board` | A2A_CALL_* | 🟡 **P-G preserved** — but Centre layout must keep tab/page accessible from sidebar |
| 5 | **Activity** | `/activity` | `GET /audit?limit=200`, `GET /traces`, `GET /events/history` | TOOL_CALL_END, AGENT_TICK_END | 🟡 **P-I** ActivityFeed covers tool-call stream; full audit/log/events page **missing from P-I** |
| 6 | **Self-improve** | `/self-improve` | `GET /self-improve/{status,history,pending,stages}`, `GET /learning/{status,history}`, `POST /run-tests`, `POST /self-diagnose` | SELFMOD_START/END | 🟡 **P-I** SelfImprovementFeed only. Full page with run-tests/diagnose/learning **missing from P-I** |
| 7 | **Approvals** | `/approvals` | `GET /approvals`, `/approvals/edits`, `POST /approvals/decide|cancel`, `DELETE /approvals/cancel-all` | APPROVAL_REQUESTED/RESOLVED | 🟡 **P-I** ApprovalModal (modal). Full inbox page with cancel-all + edits log **missing from P-I** |
| 8 | **Sessions** | `/sessions` | `GET /sessions`, `/sessions/{id}`, `/sessions/{id}/title`, `POST /sessions/{id}/{title,auto-title}`, `DELETE /sessions/{id}` | — | ❌ **MISSING from both legacy + P-I** — full gap |
| 9 | **Cron** | `/cron` | `GET /cron`, `/cron/stats`, `/cron/{id}`, full CRUD | — | ❌ **MISSING entirely** — full gap |
| 10 | **Commitments** | `/commitments` | `GET /commitments`, full CRUD + `/fulfill` | — | ❌ **MISSING entirely** — full gap |
| 11 | **Tasks** | `/tasks` | `GET /tasks`, `/tasks/{id}`, retry/cancel/bulk endpoints | AGENT_TICK_END | ⚠️ **legacy had read-only display; full CRUD missing from P-I** |
| 12 | **Docs** | `/docs` | Read-only files (markdown) served from `dashboard/docs/` (built into bundle) | — | ❌ **MISSING entirely** — would render CENTRE.md, LEARNING.md, etc. |
| 13 | **Settings** | `/settings` | `GET /config/{validate,summary,llm}`, `GET /llm/pool`, `GET /model/catalog`, `GET /health/summary` (channels), `GET /system-status` | — | ❌ **MISSING entirely** — full gap |
| 14 | **Team** | `/team` | (placeholder) | — | ❌ **MISSING** — multi-user feature is post-v1.0 |

**Sub-pages (modal/sheet overlays):**

| Sub-page | Opens from | Endpoint |
|---|---|---|
| Memory → Detail | Memory list row click | `GET /memory/session/{sid}/count` |
| Approval → Arg viewer | Approval card | `/approvals/{id}.args` (already in `/approvals` payload) |
| Trace → Session drilldown | Activity row → "open trace" | `GET /traces/{session_id}` (already opens in new tab today) |
| Settings → MCP servers | Settings card | (no endpoint — read via `HiveOS.load_mcp_servers` Python call → needs `GET /mcp/servers` backend hookup, **gap**) |
| Settings → DANGEROUS_TOOLS | Settings → Safety | (read-only — bundle import from `Core/approval_gate.py` constants) |
| Settings → SOUL.md | Settings → About | (read-only — bundle import from `Config/SOUL.md`) |

---

## 3. Page specs (1 paragraph each, locked to SH1 holographic style)

### Page 1 — Chat (already spec'd by P-I)
- Hero: full-width chat card. Left rail = session list (collapsible). Centre = conversation. Right rail = MemoryPeek + SkillLauncher (P-I). Composer at bottom.
- New for full coverage: session-title inline rename + auto-title button.

### Page 2 — Memory (P-I panel + page extension)
- Header strip: stats row (total facts / unread / last-consolidated / Obsidian N notes) with cyan pulse on unread.
- Main: tabs **Recent / Important / Topics / Sessions**. Each list row = fact + importance badge + "open session" link.
- Right rail: MemoryPeek (P-I panel) — top-3 facts.
- Action bar (top): "Consolidate now" button (POST `/memory/{sid}/consolidate`) + "Export" (GET `/memory/export` → download JSONL) + "Wipe" (DELETE `/memory/wipe-knowledge`, confirmation modal).

### Page 3 — Skills (P-I panel + extension)
- Tabs: **Pinned / Active / Stale / Archived / All** (P-I has 4; add Stale badge).
- Card grid: each card = skill name + description + use_count + pin/archive buttons.
- **Add for P-I:** archive button + "Open in detail" modal with `GET /skills/{name}` showing full content.

### Page 4 — Kanban (P-G preserved)
- 5 columns (researcher / coder / reviewer / memory-keeper / security-reviewer) with live cards. Live WS-driven.
- **Critical:** Centre.jsx layout must keep this as a top-level page reachable from sidebar, **not a tab inside another page**.

### Page 5 — Activity (P-I feed + full-page extension)
- P-I ActivityFeed component is the live tool-call stream.
- Add tabs: **Live / Audit log / Traces / Events / Loop-guard**.
  - Audit log: filterable table by tool/status/time/export (`GET /audit/search`).
  - Traces: list of recent sessions → click → opens trace overlay (`GET /traces/{sid}` + `/traces/stats`).
  - Events: EventBus history (`GET /events/history` + `/events/stats`).
  - Loop-guard: stats table + "Reset" button (`GET /loop-guard/stats` + `POST /loop-guard/reset`).

### Page 6 — Self-improve (P-I feed + full-page extension)
- P-I SelfImprovementFeed component shows recent verdicts.
- Add tabs: **Verdicts / History / Pending edits / Run-tests / Learning loop**.
  - Verdicts: full list (P-I shows 5).
  - History: `GET /self-improve/history` paginated.
  - Pending edits: `/self-improve/pending` — code diffs awaiting approve.
  - Run-tests: button → `POST /run-tests` → modal with pytest output.
  - Learning loop: `GET /learning/status` (current state) + history + "Run evaluation" button (`POST /learning/run`).

### Page 7 — Approvals (P-I modal + full-page extension)
- P-I ApprovalModal opens as toast.
- Full inbox page: list of pending (`GET /approvals`), batch "Cancel all" (DELETE `/approvals/cancel-all`), edits log (`GET /approvals/edits`).
- Row click → ApprovalModal (P-I) for full args/reason.

### Page 8 — Sessions (NEW)
- List view: `GET /sessions` — title, last activity, message count.
- Click → open Chat page with `session_id` pre-loaded.
- Row actions: rename (POST `/sessions/{sid}/title`), auto-title (POST `/sessions/{sid}/auto-title`), delete (DELETE).
- Search bar (P-I omits): `GET /sessions/search?q=`.

### Page 9 — Cron (NEW)
- Table: schedule / job name / next run / last status / enabled toggle.
- Add row button → modal with cron expression picker + handler select.
- Full CRUD via `/cron` endpoints.

### Page 10 — Commitments (NEW)
- Card list: title / recurrence / next due / status.
- Toggle: fulfill / delete / edit.

### Page 11 — Tasks (extend legacy)
- Table: id / kind / state (color-coded) / source / attempts / actions.
- Actions: retry, cancel, view details.
- Bulk: "Retry all failed" + "Cancel running".
- Sub-tabs: **All / Pending / Running / Failed** (`GET /tasks/by-kind` etc.).

### Page 12 — Docs (NEW)
- Sidebar of markdown files. Rendered via `react-markdown`. Built into bundle at build time (Vite glob import).
- Files: `CENTRE.md`, `ARCHITECTURE.md`, `LEARNING.md`, `SOUL.md` (read-only banner), `STATUS.md`, `API.md`.

### Page 13 — Settings (NEW)
- Cards: **LLM pool** (from `/llm/pool` + `/model/catalog`), **Config** (`/config/summary` + validate), **Channels** (from `/health/summary.channels`), **MCP servers** (read list, see gap), **Safety** (DANGEROUS_TOOLS, read-only import), **About** (SOUL.md, read-only banner).

---

## 4. Backend gaps that block UI

These are backend capabilities that need wiring **before** the corresponding UI page can render real data:

| # | Gap | Effort | Owner | Sprint / Date | Notes |
|---|---|---|---|---|---|
| B1 | `POST /eval/run` and `GET /eval/latest` | small | Hive | Sprint 7 (7J) | SPEC_P_B mentions but deferred from app.py; UI for Evals tab cannot work until wired |
| B2 | `GET /mcp/servers` | small | Hive | Sprint 7 (7I) | Today only loaded server-side via `HiveOS.load_mcp_servers`; needs gateway route |
| B3 | `/learning/verdicts` vs `/learning/history` | check | Hive (P-I coder) | 2026-07-01 (P-I T0) | P-F added `/learning/{status,history,run}`; verdict-tab needs either sub-route or rolls verdicts into history. **Verify at P-I T0:** verdicts are rolled into `/learning/history` payload (acceptable for v1.0). |
| B4 | `GET /health/summary.channels` (P-I spec) | small | Hive (P-I coder) | P-I T0 (2026-07-01) | One-line addition; per SPRINT_6_P_I_RESEARCH §5. **Verified spec'd in P-I plan T0.1.** |
| B5 | `GET /sessions/search` exists? | check | Hive (P-I coder) | 2026-07-01 (P-I T0) | **Verified exists:** `/sessions/search` registered at `src/hive/gateway/app.py:565` (verified 2026-06-30). ✅ No gap. Marked resolved. |
| B6 | `POST /skills/{name}/state` for archive | check | Hive (P-I coder) | 2026-07-01 (P-I T0) | **Verified MissionControl uses `/skills/{name}` (read) + pin/unpin only** — no archive endpoint today. P-I T0 will add `POST /skills/{name}/archive` (one-line in `app.py`). |

---

## 5. Frontend component inventory (post-P-I)

P-I delivers 12 components. To cover all 13 pages, **15 more components** are needed (post-P-I). Combined with SH1 sidebar = a complete Centre.jsx.

**Atoms (5):**
- `<StatusPill>` — green/amber/rose dot with label (for status block, channels, sessions)
- `<KeyHint>` — `<kbd>`-style key chip with optional Cmd/Shift modifier
- `<MarkdownView>` — wraps react-markdown with our prose theme
- `<ConfirmDialog>` — destructive-action confirmation (wipe memory, delete session, cancel-all)
- `<Drawer>` — mobile sidebar overlay

**Molecules (5):**
- `<StatCard>` — title + value + delta + sparkline (used by telemetry, memory stats, budget)
- `<DataTable>` — sortable/filterable table (audit, tasks, cron, sessions)
- `<TabBar>` — secondary tab strip (used on Memory, Skills, Activity, Self-improve, Tasks pages)
- `<Modal>` — generic modal with backdrop blur (used by ApprovalModal, Cron create, etc.)
- `<Toast>` — non-blocking notification (used by approval_requested push, budget warnings)

**Pages (10 new page components, on top of P-I Chat/Kanban):**
- `<MemoryPage>`, `<SessionsPage>`, `<CronPage>`, `<CommitmentsPage>`, `<TasksPage>`, `<DocsPage>`, `<SettingsPage>`, `<ApprovalsPage>`, `<SelfImprovePage>`, `<ActivityPage>`

**Hooks (additions, P-I delivers 3):**
- `useEventStream(url)` — generic SSE/WS subscription with reconnect (extracted from P-I `useWebSocket`)
- `usePagination(endpoint, pageSize)` — for traces, audit, sessions lists
- `useDebouncedValue(value, ms)` — for search inputs

**Total delta post-P-I:** ~20 components + 3 hooks + 13 page mounts ≈ ~1800-2200 LOC + ~1500 LOC tests.

---

## 6. Implementation phasing (after P-I ships)

**Sprint 7 candidate scope** — each phase is a single PR with its own coverage gate:

| Phase | Pages | Effort | Backend prereq | Notes |
|---|---|---|---|---|
| 7A | Sessions (#8) | small (new) | none | just GET/POST/DELETE wrappers; needed for Chat sidebar |
| 7B | Tasks (#11) + Self-improve full (#6) | medium | none | extends legacy pattern; biggest ROI |
| 7C | Activity full (#5) — audit + traces + events + loop-guard tabs | medium | none | 4 sub-views; reuses DataTable |
| 7D | Memory full (#2) — 4 tabs + consolidate/export/wipe | small | none | mostly UI; consolidate action already exists |
| 7E | Cron (#9) + Commitments (#10) | medium | none | 2 small CRUD pages |
| 7F | Approvals full (#7) + cancel-all + edits log | small | none | extends P-I modal |
| 7G | Skills full (#3) — archive action + detail modal | small | none | extends P-I panel |
| 7H | Docs (#12) | small | none | bundles markdown at build time |
| 7I | Settings (#13) — LLM pool / config / channels / MCP / safety / about | medium | B2 (MCP list endpoint) | 5 cards |
| 7J | Backend gap B1 (evals endpoint) + Evals page | medium | B1 | unblocks 1 more page |
| 7K | Discovery wizard modal (search + add capability) | medium | none | chat-driven today, but UI affordance helps |

Total post-P-I: **11 PRs / ~7-9 weeks**. Sequenced by ROI: 7A→7B→7C first (operator daily-use), then 7D/7E/7F/7G (operator infrequent), then 7H/7I/7J/7K (settings & growth).

**Out-of-scope (post-v1.0, beyond Sprint 7):**
- Team page (#14) — multi-user is a v2.0 architecture change
- Voice input — P-I delivers, but full conversation model is TBD
- Full Kanban with drag-and-drop card moves (currently snapshot-only via `/agents/board`)
- Native mobile app (current mobile is responsive web)

---

## 7. Visual rules (locked from SH1 mockup + memory file)

Every new page MUST follow:
- **Palette:** `--bg:#04050b`, `--cyan:#22d3ee`, `--blue:#3b82f6`, `--violet:#8b5cf6`, `--amber:#f59e0b`, `--rose:#f43f5e`.
- **Cards:** `backdrop-filter: blur(20px)` glass with conic-gradient border (cyan→blue→violet).
- **Active indicators:** cyan glow with drop-shadow filter; 3px gradient bar on left of selected nav item.
- **Buttons:** three styles — primary (conic gradient with shadow), secondary (glass + cyan border), danger (rose glow).
- **Type:** system stack (`-apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter"`); no Fraunces, no Space Mono.
- **Motion:** staggered reveal on first mount (`animation-delay` 30-50 ms per row); pulsing dots for live state; no global spin.
- **Density:** 13 px base, 11 px labels, 10 px micro-labels, all caps with `.2em` letter-spacing.

---

## 8. Acceptance criteria (for the full Centre, after Sprint 7)

- All 13 sidebar pages render without error
- Every backend endpoint listed in §2 has at least one UI affordance (page, button, link)
- WS `/ws/dashboard` subscribes exactly once (singleton hook in Centre.jsx)
- Bundle size <500 KB gzipped (per P-I gate; should hold since components are list-heavy)
- 100 % coverage on hooks, 80 %+ on components
- All destructive actions require `<ConfirmDialog>` (wipe memory, delete session, cancel-all)
- All settings show read-only banner where source is immutable (`Core/approval_gate.py`, `Config/SOUL.md`)
- Mobile bottom peek-bar exposes Chat / Memory / Approvals / Activity / Settings minimum

---

## 9. Decision points for Kamil

Before Sprint 7 kicks off, these are open decisions:

1. **Sessions-as-sidebar?** — Should Sessions be a top-level page (#8) or just a left rail in Chat? The former is more discoverable; the latter saves a sidebar slot.
2. **Tasks in sidebar?** — Legacy had Tasks in Ops tab; do you want it promoted to a sidebar slot (current plan) or kept under a "Workspace" sub-menu?
3. **Settings vs separate Admin?** — Settings at root level (current plan) or behind an "Admin" gate?
4. **Docs scope** — just bundled markdown, or also live `/docs` search across all `docs/*.md`?
5. **Evals in Sprint 7?** — needs backend hookup (B1); could be deferred to Sprint 8 if backend cost is high.
6. **Mobile peek-bar minimums** — confirm the 5 icons (Chat / Memory / Approvals / Activity / Settings) are right, or adjust.

---

## 10. Sources

- `docs/API.md` (1001 lines, all HTTP routes)
- `docs/ARCHITECTURE.md` (361 lines, surfaces)
- `docs/STATUS.md` (669 lines, built vs planned)
- `docs/sprints/SPRINT_6_AUTONOMY_LIB.md` (449 lines, sprint surface additions)
- `docs/sprints/SPRINT_6_P_I_RESEARCH.md` (267 lines, P-I plan)
- `docs/references/HIVEOS_COMPONENTS.md` (103 lines, module list)
- `dashboard/MissionControl.jsx` (700 lines, legacy UI)
- `src/hive/gateway/app.py` (**1414 lines, 113 routes registered**: 111 HTTP + 2 WS — verified `grep -cE "^\s*@app\.(get|post|put|patch|delete|websocket)\(" src/hive/gateway/app.py` returns 113)
- `src/hive/runtime.py` (composition root, registers 4 conditional channel webhooks: Telegram / Slack / Discord / Email)
- **Canonical IA: `docs/UI_MENU_V2.md`** (Hub top-slot + WORK/RUN/WATCH/TUNE groups, 17 items, ⌘K palette primary nav — supersedes UI_PLAN §1 sidebar ASCII for IA decisions)
- SH1 holographic mockup — `screenshots/frontend/mockups/new mockups/SH1-full-holo-sidebar.html`
- Memory: [[hiveos-design-style]], [[session-handoff-2026-06-30-layout-mockups]]