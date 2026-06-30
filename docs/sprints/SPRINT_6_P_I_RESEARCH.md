# SPRINT 6 — Phase I (Jarvis Front) Research & Sequencing

> **Status:** Research/sequencing reference. Not the implementation plan.
> **Authoritative plan** will be written after PR #88 (P-G) merges.
> **Issue:** #77
> **Author:** Hive (with structural input from full-Fala-3 P-G work)
> **Created:** 2026-06-29

This document maps the surface area of Phase I (Jarvis Front) so that when
PR #88 merges, the actual implementation plan can be drafted without
re-reading the issue and codebase. The plan itself will live at
`docs/superpowers/plans/YYYY-MM-DD-sprint6-pi-jarvis-front.md` and will
follow the writing-plans skill template (Global Constraints, File
Structure, bite-sized TDD tasks).

---

## 1. Scope analysis (issue #77 verbatim)

### 1.1 Frontend files (15 net-new + 1 swap)

| Path | Type | Purpose |
|---|---|---|
| `dashboard/src/Centre.jsx` | root | Replaces `MissionControl.jsx` as default mount |
| `dashboard/src/components/StatusOrb.jsx` | atom | Animated SVG reflecting Hive state |
| `dashboard/src/components/ChatCenter.jsx` | hero | Large immersive chat with tool-call chips + markdown |
| `dashboard/src/components/ActivityFeed.jsx` | panel | Real-time tool-call log |
| `dashboard/src/components/MemoryPeek.jsx` | panel | Collapsible memory panel |
| `dashboard/src/components/SkillLauncher.jsx` | panel | Pinned skills grid |
| `dashboard/src/components/SurfaceBar.jsx` | atom | Channel status pills (top bar) |
| `dashboard/src/components/ApprovalModal.jsx` | modal | Full arg/reason dialog |
| `dashboard/src/components/VoiceToggle.jsx` | atom | Mic button + waveform |
| `dashboard/src/components/SelfImprovementFeed.jsx` | panel | Recent self-improve verdicts |
| `dashboard/src/hooks/useWebSocket.js` | hook | `/ws/dashboard` with reconnect |
| `dashboard/src/hooks/useGateway.js` | hook | Fetch wrapper with token |
| `dashboard/src/hooks/useVoice.js` | hook | Mic permission + waveform |
| `dashboard/src/styles/theme.css` | tokens | Premium design tokens (replaces current theme) |
| `dashboard/src/__tests__/` | suite | Vitest setup + component tests |

Plus: delete `MissionControl.jsx` (or rename to `_legacy.jsx`).

### 1.2 Backend files (1 surgical change)

| Path | Change |
|---|---|
| `src/hive/gateway/app.py` | `GET /health/summary` gains `channels: {telegram, slack, discord, email}` booleans |

The current `/health/summary` returns `{status, version, uptime, components, metrics}`.
The channels object joins as a sibling, sourced from the same `_channel_registry`
used to dispatch inbound (SPRINT_6 P-E, PR #87).

### 1.3 Dependencies (per issue #77)

- **P-G (Kanban) — PR #88** — activity feed consumes `a2a.call.*` events
- **P-C (streaming) — PR #82** — chat tokens stream per-iteration
- All other backend endpoints exist (94 in gateway today)

---

## 2. Component dependency graph

```
        StatusOrb   (no deps)
            |
            v
   +---- useGateway ----+
   |    useWebSocket    |  (3 leaf hooks — independent)
   |      useVoice      |
   +--------+-----------+
            |
   +--------+-----------+----------------+
   v        v           v                v
SurfaceBar  MemoryPeek  SkillLauncher  ActivityFeed  SelfImprovementFeed
                                                       (all 5 panels: data)
   |                                          |
   +-----+----------------------------------+
         |
         v
   +-- ApprovalModal (useWebSocket) --+
   +-- ChatCenter (useGateway + useWebSocket) --+
   +-- VoiceToggle (useVoice) --+
         |
         v
      Centre.jsx (mounts all in the layout)
```

**Topological order:**
1. `useGateway`, `useWebSocket`, `useVoice` (3 hooks — independent, can be parallel PRs but 1 coder is fine)
2. `StatusOrb` (no hooks; pure SVG)
3. 5 panels (`SurfaceBar`, `MemoryPeek`, `SkillLauncher`, `ActivityFeed`, `SelfImprovementFeed`)
4. `ApprovalModal`, `ChatCenter`, `VoiceToggle` (interactive components — depend on hooks)
5. `Centre.jsx` (the root)

**Cycle detection:** None. The hooks never import components; the components never import each other (each consumes a single hook). `Centre.jsx` is the only file that imports all the components.

---

## 3. Theme tokens (from issue #77, verbatim)

```css
:root {
  --ink: #05080a;
  --hud: #0a1410;
  --neon-cyan: #39ff14;
  --neon-amber: #ff9f0a;
  --neon-rose: #ff3b30;
  --neon-violet: #c77dff;
  --text: #cfe;
  --text-dim: #4a6a5a;
  --glass: rgba(8, 17, 13, 0.85);
  --border: #1c2b24;
}
```

Glass morphism, mono typography (`JetBrains Mono`), animated status orb, scan-line animations, responsive (768px / 480px breakpoints).

**Conflict check:** The current `dashboard/src/styles/theme.css` (extended in PR #88 to add `.kanban-*` rules) uses a similar palette. The P-I overhaul will *replace* the existing file — `.kanban-*` rules need to be preserved (merge, not blow away). The plan must call this out explicitly.

**CLI theme parity:** The CLI's NEON theme (PR #80, `src/hive/surfaces/cli/themes.py`) uses complementary tokens — `HIVE_THEME=neon` should produce a "dashboard looks like the terminal" look. The P-I plan should not import from the CLI side (Python → JS is a build-time no-go), but the *aesthetic* is intentionally aligned per the SPRINT_6 design language.

---

## 4. Risks & hard constraints

### 4.1 Vitest is a NEW dependency

`playwright` was deferred in P-G because it wasn't in `pyproject.toml`. **Vitest is the same situation** — the dashboard has no JS test runner today. The plan MUST add `vitest` + `@testing-library/react` + `jsdom` to `dashboard/package.json`, plus a `test` script.

Cost: ~30 seconds to install; one CI job. Not a blocker.

### 4.2 Bundle size budget: <500KB gzipped

Current `MissionControl.jsx` build is ~180KB gzipped. Adding 12 components + 3 hooks + theme will likely push it to **350-450KB** — within budget but tight. Mitigation:

- **No chart libraries.** The existing ActivityFeed is a list, not a graph. SelfImprovementFeed is also a list.
- **No animation library.** The status orb uses raw SVG + CSS keyframes. Scan-line uses CSS only.
- **Code-split via `React.lazy`** for the panels (SkillLauncher, ActivityFeed, SelfImprovementFeed, MemoryPeek) — they live behind a "show more" toggle. Initial mount is just the chat + status orb + surface bar.

The plan must verify bundle size at the end (`npm run build && du -h dist/*.js`) and report the gzipped size in the PR body.

### 4.3 Big-bang theme migration

There's no incremental path — every component must consume the new tokens, and the old `MissionControl.jsx` doesn't have a visual analogue. Mitigation: **Centre.jsx ships as the only mount; old file deleted in the same commit**. The P-G `.kanban-*` CSS rules from PR #88 survive because the P-I theme.css is a superset, not a replacement.

### 4.4 MissionControl.jsx deletion is a hard cut

The dashboard `main.jsx` mounts `MissionControl`. If the P-I plan renames `MissionControl` → `_legacy.jsx` and mounts `Centre` instead, any third-party script or test that points at `MissionControl` breaks. Mitigation: grep the repo for `MissionControl` references before the swap.

### 4.5 Streaming + Kanban events are runtime-coupled

`ChatCenter` consumes `/ws/dashboard` events with `event_type ∈ {token, tool_call, approval, a2a.call.*}`. P-C (streaming) emits `token` events. P-G (Kanban) emits `a2a.call.*` events. Both must be on main BEFORE Centre.jsx is wired to the WS. The plan must call out the merge ordering.

---

## 5. Sequencing recommendation (5 phases for the implementation plan)

The implementation plan (to be written post-#88-merge) should be sliced into
5 phases that a single coder can execute, each phase producing a coherent
set of commits that don't break the build:

### Phase 1 — Foundation (theme + hooks + orb)
- New `theme.css` with P-I tokens (merge of current + new; `.kanban-*` preserved)
- `useGateway.js` (fetch wrapper, ~40 LOC)
- `useWebSocket.js` (reconnect, ~60 LOC)
- `useVoice.js` (mic + waveform, ~50 LOC)
- `StatusOrb.jsx` (animated SVG, ~50 LOC)
- Vitest setup (config + jsdom env + 1 sample test)
- **Commits:** 4-5
- **Coverage gate:** 100% on the 3 hooks
- **Risk:** Vitest installation might fail in the worktree; same .pth workaround as Python. Plan must call it out.

### Phase 2 — Data panels (5 panels, no interactivity)
- `SurfaceBar.jsx` (channel status pills, ~40 LOC)
- `MemoryPeek.jsx` (collapsible, ~60 LOC)
- `SkillLauncher.jsx` (pinned skills grid, ~50 LOC)
- `ActivityFeed.jsx` (real-time list, ~70 LOC)
- `SelfImprovementFeed.jsx` (verdicts list, ~50 LOC)
- All consume `useGateway` for their data; `ActivityFeed` also consumes `useWebSocket` for live updates
- **Commits:** 5 (1 per component, with tests)
- **Coverage gate:** 80%+ per component (mounts + 1 happy path + 1 error path)
- **Risk:** The gateway might not have `/learning/verdicts` (P-F added `/learning/{status,history,run}` in PR #83). The plan must verify the endpoint exists or add a missing one. **Check in the plan-writing session.**

### Phase 3 — Interactive (chat + voice + approval)
- `ChatCenter.jsx` (hero — markdown rendering, tool-call chips, voice input, ~250 LOC) — biggest component
- `VoiceToggle.jsx` (mic button + waveform, ~80 LOC)
- `ApprovalModal.jsx` (full dialog, ~100 LOC)
- **Commits:** 3
- **Coverage gate:** 80%+ per component
- **Risk:** `react-markdown` is the likely dep; check if it's already in `package.json`. If not, the plan adds it.

### Phase 4 — Root + swap
- `Centre.jsx` (root layout, ~150 LOC)
- `main.jsx` swap (`import Centre from './Centre'`)
- Delete `MissionControl.jsx` (or rename to `_legacy.jsx`)
- **Commits:** 2-3
- **Coverage gate:** `Centre.jsx` integration test (renders all child components with mock data)

### Phase 5 — Backend tweak + smoke + docs
- `GET /health/summary` channels object
- 1 Python test (`test_health_channels.py`)
- `dashboard/MISSION_CONTROL.md` → rename/replace with `dashboard/CENTRE.md`
- Bundle size check (`npm run build && du -h dist/*.js`)
- Visual smoke (Playwright deferred per P-G precedent — document, don't run)
- **Commits:** 2-3
- **Coverage gate:** unchanged on existing modules; new test on `/health/summary`

### Total estimate
- **Commits:** 17-19
- **LOC:** ~1500-1800 (components + hooks + tests + theme) + ~30 (backend)
- **CI budget:** ~5-8 minutes added (Vitest suite)
- **Bundle delta:** +200-280KB gzipped (under 500KB cap)

---

## 6. Open questions for the plan-writing session

1. **Vitest or Jest?** Issue says Vitest; we trust the issue. If Vite is already the bundler (it is — `dashboard/package.json` has `vite`), Vitest is the path of least resistance. Plan confirms.
2. **`/learning/verdicts` endpoint exists?** Need to grep `src/hive/gateway/app.py` when the plan is written. PR #83 added `/learning/{status,history,run}` — `verdicts` may or may not be there. If not, the plan adds it as a sub-route or rolls verdicts into `/learning/history`.
3. **Voice input: Web Speech API or external?** Plan defaults to Web Speech API (free, browser-native, no extra deps). If Web Speech is unreliable in Kamil's browser, the plan defers `useVoice` to a follow-up and `VoiceToggle` becomes a no-op stub.
4. **Pinned skills data source?** `GET /skills` exists (PR #40). Does it have a "pinned" subset? Plan verifies and adds a query param if needed.
5. **Dark/light theme toggle?** Issue says dark only (sci-fi). Plan delivers dark only; toggle is out of scope.

---

## 7. Acceptance gates (from issue #77, verbatim)

- `npm run build` succeeds; `dist/` produced; bundle <500KB gzipped
- `dashboard/src/Centre.jsx` mounts cleanly via `main.jsx`
- Old `MissionControl.jsx` removed (or kept as `_legacy.jsx`)
- Vitest tests pass; 100% coverage on `hooks/`, 80%+ on `components/`
- Visual: dark sci-fi theme, animated orb, glass panels, no jank
- Manual: typing streams tokens live; tool calls show as chips; memory peek loads; approval modal blocks chat
- Existing CI green (ruff + pytest)
- `hive doctor` green

---

## 8. Coordination with P-J J3

P-J J3 (CLI help + completion) ships in parallel. No code overlap with P-I
(CLI is Python, dashboard is JS). The P-J J3 plan adds `docs/CLI.md`; the
P-I plan adds `docs/CENTRE.md`. Both docs reference `docs/STATUS.md` for
the "what's built" view; that file is updated in each PR (per CLAUDE.md:
"Keep ARCHITECTURE.md + STATUS.md updated in the same PR as any behavior change").

---

## 9. SPRINT 6 close-out

Issue #77 ends: "When this lands: HiveOS v1.0 ships. SPRINT 6 closes."

Post-P-I work (post-sprint) is out of scope for this document. The `docs/STATUS.md` "Next" section (issues #42-#51 from CLAUDE.md) is the starting point for SPRINT 7 scoping.

---

## 10. Sources

- Issue #77 — https://github.com/hiveOSagent/HiveOS/issues/77
- `docs/sprints/SPRINT_6_AUTONOMY_LIB.md` § P-I
- `dashboard/MissionControl.jsx` (current mount, ~560 LOC) — to be replaced
- `src/hive/gateway/app.py` — endpoints used by the new UI
- PR #88 (P-G, pending merge) — `.kanban-*` CSS rules that must survive
- PR #87 (P-E, merged) — `_channel_registry` source for `/health/summary.channels`
- PR #83 (P-F, merged) — `/learning/{status,history,run}` (verdicts endpoint TBD)
- PR #82 (P-C, merged) — `token` SSE events
- [[sprint6-session-handoff]] — overall SPRINT_6 plan
- [[session-handoff-2026-06-29-pg-done]] — P-G close-out, defines the unblock for P-I
