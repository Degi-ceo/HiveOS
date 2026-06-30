# HiveOS Centre UI — Design History v1 (archived 2026-06-30)

> **Superseded by:**
> - [`../../UI_PLAN.md`](../../UI_PLAN.md) — canonical Centre surface plan (98-endpoint audit, 13 pages, locked SH1 holographic visual)
> - [`../../UI_MENU_V2.md`](../../UI_MENU_V2.md) — current menu spec (v2.1: Hub top-slot + WORK/RUN/WATCH/TUNE groups + 17 items)
>
> **Why archived:** Both v1 drafts were written during the 2026-06-30 design sprint before the locked IA crystallised. Preserved here as design history for future iterations — they are NOT source of truth.

## Contents

| File | v1 era | Superseded by |
|---|---|---|
| `UI_MENU_FINAL.md` | 2026-06-30 v1 spec, 9-item sidebar, flat divider | `UI_MENU_V2.md` (v2.1, Hub + 17 items) |
| `UI_SIDEBAR_FINAL.md` | 2026-06-30 v1 IA, 9-item sidebar post-research | `UI_MENU_V2.md` (v2.1 IA) + `UI_PLAN.md` §1 (canonical SH1 nav tree) |

## What changed v1 → v2.1 (high-level)

| Aspect | v1 (flat 9) | v2.1 (Hub + 4 groups, 17) |
|---|---|---|
| Sidebar items | 9 (1 divider) | 17 (Hub + 4 groups) |
| Default route | `/` = Chat | `/` = Hub |
| Sessions/Cron/Tasks | sub-routes / buried | grouped under Tasks / Commitments / Activity tabs |
| Settings location | footer-pinned | sidebar + tri-split panel |
| Scope for P-I (issue #77) | 9 items (matches P-I plan's Chat/Memory/Skills/Approvals/Activity) | 17 items + Hub (scope creep for P-I) |
| Scope for Sprint 7 backlog | n/a | `UI_PLAN.md` §6 (11 PRs sequence) |

## Still-relevant v1 design notes

Some patterns from v1 are LOCKED and reused in v2.1:

- **Footer status line** (`◐ Idle · 14 agents · 2.1k tok`) — v1 footer pattern, kept in v2.1 footer.
- **Settings as footer / tri-split panel** (Personal / Account / System) — v1 footer-pinned, kept in v2.1.
- **Voice button in Chat composer only** (not in sidebar) — v1 rejected sidebar-voice, v2.1 keeps that decision.
- **Mobile bottom peek-bar (5 icons)** — v1 mobile pattern, kept in v2.1 (different 5 icons).

## Note on the parallel locked visual style

Both v1 and v2.1 locked **SH1 holographic** (deep navy + cyan conic + glass cards) as the visual direction. The visual language never changed; only the IA scope evolved. See `[[hiveos-design-style]]` for the locked palette + glass rules.

## How to use this archive

- If you are reviewing an old design thread and someone links to `UI_MENU_FINAL.md` or `UI_SIDEBAR_FINAL.md`: they're seeing the v1 draft. Redirect to `UI_MENU_V2.md`.
- Do NOT cite these files in new code, docs, or PRs. Always cite the canonical pair (`UI_PLAN.md` + `UI_MENU_V2.md`).
- If you want to evolve the IA further, base the work on `UI_MENU_V2.md`, not on these v1 drafts.

## Why: / How to apply:

**Why:** Keep design history visible for context without polluting the live `docs/` with conflicting drafts. New contributors and future Hive runs can see the evolution.

**How to apply:** When linking to Centre IA in any new doc, use ONLY `UI_PLAN.md` and `UI_MENU_V2.md`. If a doc references a v1 path, fix the doc, not this archive.
