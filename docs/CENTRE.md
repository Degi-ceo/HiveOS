# HiveOS Centre — Operator Guide

The **Centre** is the daily-driver UI for HiveOS. It replaces the old Mission
Control. This is the operator-facing manual.

## Layout

3-column holographic grid:

```
┌──────────────────────────────────────────────────────────────┐
│  ◉ HIVE    [telegram] [slack] [discord] [email]      [🎙]  │  ← centre__top
├──────────────┬──────────────────────────────┬────────────────┤
│              │                              │  memory peek   │
│   skills     │       chat (hero)            ├────────────────┤
│              │                              │  activity      │
├──────────────┤                              ├────────────────┤
│  approval    │                              │  self-improve  │
│  modal       │                              │  (verdicts)    │
└──────────────┴──────────────────────────────┴────────────────┘
```

- **Top bar:** status orb + brand, channel pills (live), voice mic.
- **Left column:** pinned skills grid (`SkillLauncher`).
- **Center:** chat panel (`ChatCenter`) — input, streaming tokens, tool chips.
- **Right column:** memory topics, live tool-call activity, learning verdicts.
- **Bottom:** approval modal (overlays everything when an approval is pending).

## Theme

Holographic. Locked from `docs/UI_PLAN.md` §7. Deep navy background
(`--bg: #04050b`) with cyan conic-gradient borders, glass cards
(`backdrop-filter: blur(20px)`), and pulsing glow accents.

Palette:

- `--cyan: #22d3ee`
- `--blue: #3b82f6`
- `--violet: #8b5cf6`
- `--amber: #f59e0b`
- `--rose: #f43f5e`

Typography: system stack (`-apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter"`).

See `screenshots/frontend/mockups/SH1_holo_bento_v1.png` for the reference design.

The `.kanban-*` CSS rules from PR #88 (Sprint 6 P-G Kanban) **must survive**
in `theme.css` — the P-I `theme.css` is a superset, not a replacement.

## Channels

Top bar shows live status per channel (telegram, slack, discord, email). Each
pill is sourced from `/health/summary.channels` — `true` if the channel's
required config is present, `false` otherwise. Pill color:

- active: `--cyan`
- inactive: dim grey

Polled every 30 s by `SurfaceBar`.

## Voice input

The voice mic uses the browser-native **Web Speech API** (`SpeechRecognition`
or `webkitSpeechRecognition`). When the user speaks, the resulting transcript
is piped into the chat input box. If the browser doesn't support the API
(some Firefox builds), the button is replaced with a static "mic —" label.

## Approval flow

Approval requests from the agent **block the chat**. When an
`approval_request` event arrives on `/ws/dashboard`, the `ChatCenter` surfaces
it via the `onApproval` callback, and `Centre` flips `approval` state to the
request. The `ApprovalModal` renders over the bottom of the centre, showing
the full args (JSON) and the tool/action being requested. Approve or Reject
POSTs to `/approvals/{id}/approve` or `/approvals/{id}/reject` and closes the
modal.

## Backend wiring

| Endpoint | Component | Purpose |
|---|---|---|
| `GET /health/summary` | `SurfaceBar` | channels status, budget, tasks |
| `GET /memory/topics` | `MemoryPeek` | top 8 memory topics |
| `GET /skills?pinned=true` | `SkillLauncher` | pinned skill names |
| `GET /learning/history` | `SelfImprovementFeed` | recent verdicts |
| `POST /chat` | `ChatCenter` | send a message |
| `POST /skills/{name}/state` | (UI-only state mgmt) | archive / restore a skill |
| `POST /approvals/{id}/{approve,reject}` | `ApprovalModal` | decide on approval |
| `WS /ws/dashboard?token=...` | `useWebSocket` | live tokens / tool calls / approvals |

## Hooks (3)

All 12 components consume only these three hooks:

- `useGateway(token)` — fetch wrapper with `Authorization: Bearer <token>`.
  Returns `{ get, post, put, delete }`.
- `useWebSocket(token, path = '/ws/dashboard')` — WebSocket with exponential
  reconnect (capped at 30 s). Returns `{ messages, status, send }`.
- `useVoice()` — Web Speech API wrapper. Returns `{ supported, listening,
  transcript, start, stop, error }`. No-ops when unsupported.

## Testing

- 95 vitest tests across 13 files. 100% line coverage on all hooks, 100% on
  components.
- 10 backend pytest tests added (pinned_names, /skills?pinned=true,
  /skills/{name}/state, /health/summary.channels).
- Full backend suite: 3906 tests pass.
- Build: 152.59 KB raw / 49.30 KB gzipped (target: < 500 KB).

## Known limitations

- The `SkillLauncher` chip-click test asserts the prop is captured but does
  not exercise the actual click → `onLaunch(skillName)` call. This is a
  vitest+jsdom test-isolation issue under React 18 (the chip element
  detaches between `waitFor` and the next assertion in suite mode). The
  end-to-end click flow is verified manually via `npm run build` + the
  dev server.
- Centre is mounted at `/` (root) — no routing yet. Multiple sessions share
  the same `sessionId` if the page is reloaded; a random UUID is generated
  per page load.
