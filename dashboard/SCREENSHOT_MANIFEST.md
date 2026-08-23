# HiveOS UI Preview — Screenshot Manifest
# v0.8.3 — Captured 2026-08-23

All screenshots captured at: `dashboard/screenshots-output/`

Format: `{viewport}_{screenId}.png`

## Viewports
- 1440p — Desktop 1440 × 900
- 1280p — Desktop 1280 × 800
- 1024p — Compact 1024 × 768
- 768p  — Tablet 768 × 600
- 390p  — Mobile 390 × 844

## Screens (29 total)

| # | Screen | Route | Viewport | Notes |
|---|--------|-------|----------|-------|
| 01 | hub | `/` | all 5 | |
| 02 | chat | `/chat/:sessionId?` | all 5 | |
| 03 | memory | `/memory` | all 5 | |
| 04 | skills | `/skills` | all 5 | |
| 05 | files | `/files` | all 5 | |
| 06 | agents | `/agents` | all 5 | |
| 07 | tasks | `/tasks` | all 5 | |
| 08 | channels | `/channels/:platform?` | all 5 | |
| 09 | mcp | `/mcp` | all 5 | |
| 10 | logs | `/logs` | all 5 | |
| 11 | activity | `/activity` | all 5 | |
| 12 | sessions | `/sessions` | all 5 | |
| 13 | approvals | `/approvals` | all 5 | |
| 14 | self-improve | `/self-improve` | all 5 | |
| 15 | analytics | `/analytics` | all 5 | |
| 16 | docs | `/docs/:filename?` | all 5 | |
| 17 | settings | `/settings` | all 5 | |
| 18 | agent-detail | `/agents/:agentId` | all 5 | |
| 19 | command-palette | global overlay | all 5 | |
| 20 | approval-modal | `/approvals/:id overlay` | all 5 | |
| 21 | trace-detail | `/traces/:sessionId overlay` | all 5 | |
| 22 | new-task | `/tasks overlay` | all 5 | |
| 23 | notifications | global panel | all 5 | |
| 24 | release-log | `/release-log concept` | all 5 | |
| 25 | cron | `/tasks?tab=cron` | all 5 | |
| 26 | commitments | `/tasks?tab=promises` | all 5 | |
| 27 | mobile-hub | `/ · 390px` | all 5 | |
| 28 | mobile-chat | `/chat/:sessionId · 390px` | all 5 | |
| 29 | mobile-nav | `global drawer · 390px` | all 5 | |

## Verification

- Playwright e2e: **29/29 screens pass** — zero console errors
- All 145 screenshots: non-zero file sizes confirmed
- Screenshots captured via `screenshots.mjs` with playwright-core
- Server bound to loopback (127.0.0.1) — no external exposure
- Path traversal hardened — all paths validated inside `dist/`

## Totals
- Screenshots: 145 (29 screens × 5 viewports)
- File size range: ~40 kB (390p) to ~180 kB (1440p)
- All files: PNG format, device pixel ratio (HiDPI)
