# HiveOS UI mockup generation guide

Use this guide with the final mockup package. One prompt file generates one image.
Never combine several screens into a collage.

## Locked visual prompt

```text
Create one high-fidelity HiveOS operator-console UI mockup.

Canvas: desktop 1440 × 1024 unless the prompt explicitly says mobile 390 × 844.
Output: exactly one complete UI view; no browser chrome, device frame, presentation
board, split-screen comparison or collage.

Product: HiveOS is a private autonomous AI operating system. Hive can chat, use
tools, delegate to named specialist agents, retain memory, create durable tasks,
run schedules and promises, request human approval, manage integrations, monitor
runtime state and propose reviewable self-improvements.

Visual system: mature matte dark UI inspired by the restraint of Codex, ChatGPT,
Linear and polished macOS software without cloning them. Near-black charcoal base,
subtle surface steps, one-pixel neutral borders, off-white text and muted grey
metadata. One signature accent only: warm amber/honey. Green, amber, red and blue
are semantic status colours. Inter/Geist/SF-Pro-like typography. Monospace only for
IDs, models, paths and commands. 8–12 px radii, minimal shadow, generous spacing,
precise alignment and clear hierarchy.

Sidebar: HiveOS mark; New task; Hub; WORK: Chat, Memory, Skills, Files; RUN: Agents,
Tasks, Channels, MCP, Logs; WATCH: Activity, Sessions, Approvals; TUNE:
Self-improve, Analytics, Docs, Settings. Bottom: Hive online, current model and
operator. The active row uses a quiet darker fill and a 2 px amber indicator.

Avoid: purple, neon, cyberpunk, glassmorphism, holographic borders, giant gradient
blobs, glowing cards, sci-fi HUD decoration, illustrations, excessive KPI tiles,
meaningless charts, random pills, cards inside cards, huge typography, decorative
terminal output, cramped density and visual effects without information value.

Quality: production-ready and technically credible. Every visible element must help
the operator understand state, make a decision or perform an action. Render all UI
copy legibly.
```

## Composition rules

- Hub uses a restrained bento layout because it summarizes several domains. It merges
  the former Overview and Hub concepts; there is no separate Overview route.
- Domain pages use one dominant work surface plus, when useful, one supporting inspector.
- Memory is intentionally quiet: two compact summaries, one list and one inspector.
- Analytics uses at most one primary chart per view.
- Tables use row separators and generous padding rather than a grid of nested cards.
- Large visual effects are limited to a subtle ambient edge light, selected-row warmth
  and layered depth. Effects must be consistent across every screen.
- Desktop details appear in a right inspector. Mobile details become a push page or sheet.

## Agent workflow

1. Read `docs/UI_RELATIONS_AND_API.md` and the relevant prompt file completely.
2. Generate exactly one view.
3. Verify route title, active navigation, primary action, hierarchy, spelling and API-realistic data.
4. Reject any result with a second page, collage, illegible copy, extra nav item,
   purple/neon styling or unnecessary dashboard density.
5. Save using the manifest filename and update the release log for every version.
6. Do not implement the mockup as production UI until the corresponding API row is
   Implemented or the screen has explicit fixture/placeholder status.

## Placeholder preview

Run the dashboard and open `/?ui-preview=1`. The preview is isolated from the live
Mission Control component, requires no token, uses no gateway calls and exposes the
current UI/API relationship next to each fixture. All 29 approved mockup states are
reachable from the catalog. It is a design-development aid, not a production route;
the current validation ledger is `docs/UI_AUDIT_2026-08-22.md`.
