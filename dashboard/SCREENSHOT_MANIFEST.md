# HiveOS UI Preview — Screenshot Manifest

Release: v0.8.5

Generator: `dashboard/screenshot-all.mjs`

Machine manifest: `dashboard/screenshots-output/manifest.json`

## Capture contract

- 29 default screen mockups, one PNG per standalone UI state.
- 50 additional PNGs, one per non-default tab/subview.
- 79 captures total; routed tabs retain their source screen/tab in the filename and
  record the actual destination in `manifest.json`.
- Desktop CSS viewport: 1440 × 900. Mobile presentation CSS viewport: 390 × 844.
- Device scale factor: 2. Output is therefore 2880 × 1800 or 780 × 1688 pixels.
- Output is deleted before every run, so stale files cannot count as passing captures.
- Every PNG records byte size, SHA-256, route, source/target screen and selected tab.
- A capture fails on the wrong screen/title/tab, horizontal layout overflow,
  console/page errors, backend requests or a suspiciously small PNG.

## Default screens (29)

| # | Screen | Concept route | Layout |
|---:|---|---|---|
| 01 | Hub | `/` | Tile system overview |
| 02 | Chat | `/chat/:sessionId?` | Conversation / execution detail |
| 03 | Memory | `/memory` | Low-density browser and inspector |
| 04 | Skills | `/skills` | Capability library |
| 05 | Files | `/files` | Tree, list and preview |
| 06 | Agents | `/agents` | Specialist cards |
| 07 | Tasks | `/tasks` | Four-column Kanban |
| 08 | Channels | `/channels/:platform?` | Connection workspace |
| 09 | MCP | `/mcp` | Server/tool cards |
| 10 | Logs | `/logs` | Live terminal stream |
| 11 | Activity | `/activity` | Event timeline |
| 12 | Sessions | `/sessions` | Session cards |
| 13 | Approvals | `/approvals` | Safety review queue |
| 14 | Self-improve | `/self-improve` | Diagnose-to-release pipeline |
| 15 | Analytics | `/analytics` | Usage and budget visualization |
| 16 | Docs | `/docs/:filename?` | Documentation reader |
| 17 | Settings | `/settings` | Configuration panels |
| 18 | Agent detail | `/agents/:agentId` | Agent runtime workspace |
| 19 | Command palette | Global overlay | Search and command state |
| 20 | Approval review | `/approvals/:id` overlay | Sensitive action review |
| 21 | Trace detail | `/traces/:sessionId` overlay | Execution waterfall |
| 22 | New task | `/tasks` overlay | Task creation state |
| 23 | Notifications | Global panel | Operator awareness state |
| 24 | Release log | `/release-log` concept | Version history |
| 25 | Automations | `/tasks?tab=cron` | Scheduled work |
| 26 | Commitments | `/tasks?tab=promises` | Durable promises |
| 27 | Mobile Hub | `/` · 390 px | Mobile overview variant |
| 28 | Mobile Chat | `/chat/:sessionId` · 390 px | Mobile conversation variant |
| 29 | Mobile Navigation | Global drawer · 390 px | Complete mobile navigation |

## Reproduce

```bash
cd dashboard
npm ci
npx playwright install chromium
npm run build
npm run screenshots:preview
```

The generator starts its own loopback-only server on a dynamic port. It does not need
`npx vite preview`, a fixed port, `VITE_HIVE_TOKEN` or a running HiveOS backend.

The downloadable archive is `HiveOS_UI_v0.8.5.zip`. GitHub Actions also uploads it as
the `HiveOS-UI-v0.8.5-mockups` workflow artifact after every successful verification run.
