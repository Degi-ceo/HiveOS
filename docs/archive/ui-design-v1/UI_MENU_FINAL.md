# HiveOS Centre — Final Menu & Submenus

> **Date:** 2026-06-30
> **Status:** Final specification
> **Locked:** SH1 holographic + 9-item sidebar + flat-divider
> **Source:** `docs/UI_SIDEBAR_FINAL.md` (post-research IA) + `docs/UI_PLAN.md` (audit)

---

## SIDEBAR — 9 items + footer

---

### 1. 💬 **Chat** — `⌘1`
*Default route (`/`)*

| Sub-view | Trigger | Endpoint |
|---|---|---|
| Session list (left rail) | always-on, collapsible | `GET /sessions` |
| Conversation (centre) | default | `POST /chat/stream/iterations` |
| MemoryPeek (right rail) | toggle | `GET /memory/important` |
| SkillLauncher (right rail) | toggle | `GET /skills` |
| Voice input | button in composer | `useVoice` hook |
| Session switcher | top-bar dropdown | `GET /sessions` |
| Rename session | inline | `POST /sessions/{id}/title` |
| Auto-title session | button | `POST /sessions/{id}/auto-title` |
| New chat | ⌘N | (local — clears session) |
| Tool-call chips | per-turn event | WS `tool_call_*` |
| Approval overlay | modal blocks | `POST /approvals/decide` |

---

### 2. 👥 **Kanban** — `⌘2`
*Multi-agent live board*

| Sub-view | Trigger | Endpoint |
|---|---|---|
| 5-column board | default | `GET /agents/board` |
| Card trace overlay | click card | `GET /traces/{session_id}` |
| A2A envelope RPC | power-user | `POST /a2a/rpc` |
| Filter by status | chip | client-side |
| Filter by method | chip | client-side |
| Filter by tool | chip | client-side |
| Pause live updates | toggle | (client) |

**Columns (fixed):**
- 🔍 researcher
- 💻 coder
- 👁 reviewer
- 🧠 memory-keeper
- 🛡 security-reviewer

---

### 3. 🧠 **Memory** — `⌘3`
*Knowledge base (Mnemosyne + Obsidian)*

| Tab | Content | Endpoint |
|---|---|---|
| **Recent** | latest facts | (computed from `/memory/stats`) |
| **Important** | high-importance | `GET /memory/important` |
| **Topics** | clustered | `GET /memory/topics` |
| **Sessions** | per-session count | `GET /memory/session/{sid}/count` |

**Header stats (always visible):**
- Total facts
- Unread (pulsing cyan badge)
- Last consolidated (timestamp)
- Obsidian notes (N)

**Action bar (top-right):**
- 🔄 **Consolidate now** → `POST /memory/{sid}/consolidate`
- 📤 **Export** → `GET /memory/export` (download JSONL)
- 🗑 **Wipe knowledge** → confirm modal → `DELETE /memory/wipe-knowledge`

**MemoryPeek (right rail, collapsible):** top-3 facts — same component as Chat right rail.

---

### 4. ⚡ **Skills** — `⌘4`
*Pinned + library*

| Tab / filter chip | Content | Endpoint |
|---|---|---|
| **📌 Pinned** | use_count > 0 OR user-pinned | `GET /skills` + filter |
| **🟢 Active** | used recently | filter `state=active` |
| **🟡 Stale** | unused 30+ days | `GET /skills/unused` |
| **📦 Archived** | archived | `GET /skills/archived` |
| **All** | every skill | (no filter) |

**Card actions (per skill):**
- 📌 Pin / Unpin → `POST /skills/{name}/pin`
- 📤 Archive → `POST /skills/{name}/state` `{state: archived}`
- 👁 View detail (modal) → `GET /skills/{name}`
- 📋 Copy name (tooltip)

**Discovery wizard (top-right):** "Add capability" → modal with search input → `tools/discovery.py` AST-first lookup → result card → pin to Skills grid.

---

### 5. ⚠️ **Approvals** — `⌘5`
*Danger firewall inbox (badge: pending count)*

**Header action bar:**
- ✅ **Approve all safe** (heuristic: tier-LOW only)
- 🚫 **Cancel all** → confirm modal → `DELETE /approvals/cancel-all`
- 📜 **Edits log** → slide-over → `GET /approvals/edits`

**List:**
- Each card: tool name + args preview + reason + tier badge
- Click → ApprovalModal (P-I) full-screen overlay
- Approve / Deny buttons inline

**Modal (P-I ApprovalModal):**
- Full args JSON (highlighted)
- Diff preview if edit-type
- Approve / Deny / Cancel buttons

**WS push:** `APPROVAL_REQUESTED` → toast in any view + sidebar badge bump.

---

### 6. 📡 **Activity** — `⌘6`
*Live observability hub (5 tabs)*

| Tab | Content | Endpoint |
|---|---|---|
| **● Live** (default) | streaming tool-call log | WS `tool_call_*` + recent audit |
| **📋 Audit** | filterable table | `GET /audit?limit=200` + `/audit/search` |
| **🔍 Traces** | per-session drilldown | `GET /traces` + `/traces/{sid}` |
| **📨 Events** | EventBus history | `GET /events/history` + `/events/stats` |
| **🔁 Loop-guard** | degenerate-loop stats | `GET /loop-guard/stats` + `/loop-guard/top-tools` + `POST /loop-guard/reset` |

**Audit tab sub-filters:** tool name · status (ok/error) · approved (yes/no) · time range · free text search. Export CSV button.

**Traces tab:** list of recent sessions → click → slide-over with full tool-call chain for that turn.

**Live tab:** same as P-I ActivityFeed component. Pulsing red dot when receiving events.

---

### 7. 🔄 **Pipeline** — `⌘7`
*(was "Self-improve")*

| Tab | Content | Endpoint |
|---|---|---|
| **Verdicts** (default) | last 5 self-mod outcomes | `GET /self-improve/status` |
| **History** | paginated full history | `GET /self-improve/history` |
| **Pending edits** | diffs awaiting approve | `GET /self-improve/pending` + `GET /self-improve/stages` |
| **🧪 Run-tests** | isolated test suite | `POST /run-tests` |
| **📈 Learning loop** | eval-gated history | `GET /learning/status` + `/learning/history` + `POST /learning/run` |

**Verdict card colors:**
- 🟢 AUTO (PR created)
- 🟡 REVIEW (pending approval)
- 🔵 MANUAL (logged)
- 🔴 PROTECTED (refused)

**Run-tests tab:** big "▶ Run full suite" button → modal with live pytest output streaming. Stats: passed/failed/errors/elapsed.

**Learning loop tab:** current state (idle/running) + history accordion + "Run evaluation" button (POST `/learning/run`).

---

### 8. 📚 **Docs** — `⌘8`
*Operator reference (bundled markdown)*

| File | Source | Purpose |
|---|---|---|
| CENTRE.md | `dashboard/CENTRE.md` | Dashboard operator manual |
| ARCHITECTURE.md | `docs/ARCHITECTURE.md` | HiveOS architecture |
| LEARNING.md | `docs/LEARNING.md` | Learning loop manual |
| STATUS.md | `docs/STATUS.md` | Capability matrix |
| API.md | `docs/API.md` | Gateway endpoints |
| SECURITY.md | `docs/SECURITY.md` | Safety contract |
| **SOUL.md** 🔒 | `Config/SOUL.md` | Read-only — explicit "signed by owner" banner |
| **DANGEROUS_TOOLS** 🔒 | `Core/approval_gate.py` | Read-only — what requires approval |

**Layout:**
- Left rail = file tree (collapsible sections by directory)
- Right pane = rendered markdown (custom prose theme matching holographic style)
- In-pane Ctrl-F search (KISS, no global search)
- Built into bundle via Vite glob import

---

### 9. 📌 **Commitments** — `⌘9`
*Everything that needs follow-up (3 tabs)*

| Tab | Content | Endpoint |
|---|---|---|
| **📋 Tasks** (default) | durable queue | `GET /tasks` + `/tasks/by-kind` + `/tasks/{id}/retry|cancel` |
| **⏰ Cron** | scheduled jobs | `GET /cron` + full CRUD |
| **🎯 Promises** | recurring commitments | `GET /commitments` + full CRUD + `/fulfill` |

**Tasks tab sub-tabs:**
- **All** · **Pending** · **Running** · **Failed** (count badge)

**Bulk actions (Tasks):**
- 🔄 Retry all failed → `POST /tasks/retry-failed`
- ❌ Cancel running → `POST /tasks/bulk-cancel`
- 🔁 Requeue running → `POST /tasks/requeue-running`

**Cron tab:** schedule picker (cron expression input + visual preview) + handler select + enable/disable toggle.

**Promises tab:** title + recurrence + next due + status. Fulfill button → `POST /commitments/{id}/fulfill`.

**Sidebar badge:** count of overdue items across all 3 tabs (pulsing amber).

---

## FOOTER

### Status line
```
◐ Idle · 14 agents · 2.1k tok
```

| Field | Source | Refresh |
|---|---|---|
| State (Idle/Working/Blocked) | `/health/summary` + `/heartbeat` | 5s |
| Agent count | `/agents/board` total | 10s |
| Token rate (last 1m) | `/telemetry` delta | 10s |

**Click → drawer with full status detail (per-component health, error rate, last incident).**

### Settings link — `⌘,`
*Footer-pinned, NOT in main nav.*

Tri-split panel:

#### Personal tab
- Display name
- Timezone
- Voice language
- Notification preferences (which events ping)

#### Account tab
- API token (show + rotate + copy)
- OpenAI-compat connection info (for editor integrations)
- MCP server list (post-B2 endpoint)

#### System tab
- LLM pool (read-only, from `/llm/pool`)
- Model catalog (from `/model/catalog`)
- Channel pills (from `/health/summary.channels`) — show + configure each
- Config summary + validate button (from `/config/summary` + `/config/validate`)
- 🛡 Safety: DANGEROUS_TOOLS list (read-only import from `Core/approval_gate.py`)
- 🔒 About: SOUL.md (read-only banner — "signed by owner")

---

## HEADER — single line

```
[◈ HiveOS]            [⌘K search…]        [🔔 2]  [H]
```

### ⌘K palette (Raycast pattern)
**Indexed:**
- 9 routes (jump to page)
- All sessions (jump to conversation by title)
- All pinned skills (launch)
- All pending approvals (jump)
- All overdue commitments (jump)
- Commands:
  - "run tests" → opens Pipeline → Run-tests tab → triggers
  - "consolidate memory" → opens Memory → triggers consolidate
  - "open trace {session_id}" → opens Activity → Traces
  - "show docs {query}" → opens Docs
  - "new chat" → opens Chat (clears)
  - "approve all" → opens Approvals → approves tier-LOW
  - "wipe memory" → confirm modal
  - "settings" → jumps to Settings tab

### 🔔 Notifications bell
**Sources:**
- Approval pending (count)
- Budget warning (count)
- Self-improve complete (count)
- Cron job failed (count)
- Test failure (count)

**Click → slide-over with grouped list + "Mark all read" + "Go to source" link per item.**

### [H] Avatar dropdown (operator profile — NOT settings)
- Profile name + role
- Theme: holographic only (no toggle in v1.0)
- "About Hive" → Docs → SOUL.md
- "Sign out" (post-multi-user)

---

## MOBILE BOTTOM PEEK-BAR (5 icons, 64px)

| Pos | Icon | Route |
|---|---|---|
| 1 | 💬 Chat | `/` |
| 2 | 👥 Kanban | `/agents` |
| 3 | 🧠 Memory | `/memory` |
| 4 | ⚠️ Approvals | `/approvals` (amber badge) |
| 5 | 📡 Activity | `/activity` (pulse if live) |

**Hamburger (top-left):** opens full sidebar drawer with all 9 + status + settings link.

**Search icon (top-right):** opens full-screen ⌘K palette.

**Voice button:** only visible on Chat route, sits in composer (right of input).

---

## GLOBAL UI ELEMENTS

### Toast system
- Top-right corner
- Stacks max 3
- Auto-dismiss 6s (or until action taken)
- Sources: `APPROVAL_REQUESTED`, `BUDGET_BLOCK`, `MEMORY_STORE`, `SELFMOD_END`, test failure, channel receive

### ⌘K keybindings (global)
- `⌘1`–`⌘9` → jump to sidebar item
- `⌘K` → palette
- `⌘,` → settings
- `⌘N` → new chat (in Chat)
- `⌘⇧A` → approve all safe
- `⌘⇧T` → run tests
- `Esc` → close modal/palette/drawer

### Confirmation modals (destructive actions only)
- Wipe knowledge (Memory)
- Delete session (Sessions — when present as sub-route)
- Cancel all approvals (Approvals)
- Archive skill (Skills)
- Requeue running tasks (Commitments)
- Reset loop-guard stats (Activity → Loop-guard)

---

## SUB-ROUTES (URL map)

```
/                            → Chat (default)
/agents                      → Kanban
/memory                      → Memory (default tab: Recent)
/memory?tab=important        → Memory · Important
/memory?tab=topics           → Memory · Topics
/memory?tab=sessions         → Memory · Sessions
/skills                      → Skills (default tab: Pinned)
/skills?tab=active           → Skills · Active
/skills?tab=stale            → Skills · Stale
/skills?tab=archived         → Skills · Archived
/approvals                   → Approvals inbox
/approvals/{id}              → Approval detail modal (over inbox)
/activity                    → Activity (default tab: Live)
/activity/audit              → Activity · Audit
/activity/traces             → Activity · Traces
/activity/events             → Activity · Events
/activity/loop-guard         → Activity · Loop-guard
/pipeline                    → Pipeline (default tab: Verdicts)
/pipeline?tab=history        → Pipeline · History
/pipeline?tab=pending        → Pipeline · Pending edits
/pipeline?tab=tests          → Pipeline · Run-tests
/pipeline?tab=learning       → Pipeline · Learning loop
/docs                        → Docs (file tree default)
/docs/{filename}             → Docs · specific file
/commitments                 → Commitments (default tab: Tasks)
/commitments?tab=cron        → Commitments · Cron
/commitments?tab=promises    → Commitments · Promises
/settings                    → Settings (Personal tab)
/settings/account            → Settings · Account
/settings/system             → Settings · System
/traces/{session_id}         → Trace overlay (over any page)
/a2a                         → A2A RPC console (hidden — power user)
```

---

## Summary of changes vs UI_PLAN v1

| Before | Now | Why |
|---|---|---|
| 3 labeled sections (MAIN / LIVE / WORKSPACE) | 1 divider, no labels | 9 items don't justify 24px × 2 of header chrome |
| 13 sidebar entries | 9 entries | 7±2 ceiling for primary nav |
| Sessions as sidebar slot | Sub-route under Activity | Sessions = reference, not destination |
| Cron as sidebar slot | Tab under Commitments | Cron = "what's queued", groups with Tasks |
| Tasks as sidebar slot | Tab under Commitments | Tasks = "what's queued", groups with Cron |
| Team in sidebar | Avatar dropdown | Single-tenant, no nav slot needed |
| Voice in sidebar | In Chat composer | Voice always goes to chat |
| "Self-improve" label | "Pipeline" | Operators think in pipelines, not jargon |
| "Workspace" section | Footer + Commitments | Workbench items grouped by intent |

---

## Sources

- `docs/UI_SIDEBAR_FINAL.md` — IA locked
- `docs/UI_PLAN.md` — endpoint audit (98 routes)
- SH1 holographic mockup — visual lock
- Background research: Linear/Raycast/Stripe/Datadog/Sentry/Vercel patterns
- `[[hiveos-design-style]]` — visual language