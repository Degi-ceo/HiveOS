# Mnemosyne Pack

Final pack for integrating Mnemosyne v3.1.2 as the memory layer for Hermes Agent on Hetzner VPS. Two files, two purposes.

## What's in this pack

### 1. `MNEMOSYNE.md` — the master reference (~2200 lines, 81 KB)

**This is what your agent reads.** Drop it in your agent's context directory (e.g. `/opt/mnemosyne/MNEMOSYNE.md`) and reference it from SOUL.md. The agent reads it once and never needs to re-fetch `docs.mnemosyne.site` again.

Contains: 41 sections covering everything across the 50 audited pages of the docs — BEAM + MEMORIA + Shared Surface architectures, all 17+ tools with every parameter, hybrid retrieval scoring (full formula), all configuration knobs (60+ env vars), Hermes integration (the *correct* way), schema versioning, upgrade procedure, rollback strategies, multi-agent topology, 5 use case patterns, security model, encryption status (honest: not implemented), 12 catalogued common failures with recognition + fix, 7 doc-side inconsistencies to know about, glossary, cheat sheet.

**Three rules from §40 the agent must memorise:**
1. NEVER run `hermes tools disable memory` — removes ALL memory tools (built-in AND Mnemosyne)
2. NEVER use `hermes memory status` as verification — known display bug
3. NEVER assume `encryption_key` exists — encryption NOT implemented yet

### 2. `INTEGRATION_PHASES.md` — Claude Code prompts (~1300 lines, ~50 KB)

**This is what you send Claude Code to do the integration.** Ten copy-paste-ready prompts, one per phase, sent in order.

| Phase | What |
|---|---|
| 0 | Pre-flight check (read-only) |
| 1 | Install Mnemosyne v3.1.2 + sqlite-vec + Hermes plugin |
| 2 | DB initialisation + end-to-end smoke test |
| 3 | Environment configuration (env vars + Hermes YAML) |
| 4 | Multi-agent identity setup (6 identities, agent_factory.py) |
| 5 | Hermes plugin wiring + verification (using `hermes doctor`, NOT the broken `hermes memory status`) |
| 6 | Memory pre-loading (idempotent seed.py, foundational triples) |
| 7 | Cron jobs (sleep + backup + DR verify) |
| 7.5 | Schema versioning awareness + UPGRADE.md |
| 8 | Inject MNEMOSYNE.md as agent reference |
| 9 | Full-flow integration smoke test |
| 10 | (Optional) MCP SSE server for laptop access |

Plus a universal diagnostic prompt if anything goes wrong mid-build.

All corrections from the audit (v3.1.2 version, `hermes doctor` not `hermes memory status`, `memory_enabled` + `user_profile_enabled` independence, `hermes tools disable memory` warnings, both venv paths, MEMORIA/shared-surface awareness) applied throughout.

## How to use

**If you're integrating Mnemosyne fresh:**
1. SSH to your Hetzner VPS
2. Open `INTEGRATION_PHASES.md`
3. Copy Phase 0's prompt, paste into Claude Code session
4. Wait for completion, verify success criteria
5. Move to Phase 1, repeat
6. Continue through Phase 7 (mandatory) or Phase 10 (full)
7. At Phase 8, place `MNEMOSYNE.md` at `/opt/mnemosyne/MNEMOSYNE.md`

**If Mnemosyne is already running:**
- Skip the integration phases
- Place `MNEMOSYNE.md` in your agent's context directory
- Reference it from SOUL.md (or whatever top-level system prompt you use)

**For ongoing operations:**
- `MNEMOSYNE.md` is the canonical reference
- Section index in `MNEMOSYNE.md §0` (table of contents)
- §38 = failure catalog
- §39 = doc inconsistencies to know

## Versioning

- Mnemosyne version: **v3.1.2**
- Audit date: June 7, 2026
- Source: `docs.mnemosyne.site` (50 of 56 pages) + `github.com/AxDSan/mnemosyne`
- Skipped pages: 7 Comparisons (marketing positioning) + 3 per-provider Migration guides (non-operational for this use case)

## When to update this pack

- **Mnemosyne patch release** (e.g. v3.1.2 → v3.1.3): probably no changes needed. Verify via `hermes mnemosyne version`. If new features documented, add to MNEMOSYNE.md.
- **Mnemosyne minor release** (e.g. v3.1 → v3.2): re-audit the changed pages. New tools/env vars likely. Update both files.
- **Mnemosyne major release** (e.g. v3 → v4): full re-audit. Schema migrations likely. Treat as full rewrite.

To re-audit, send Claude a prompt like:
> "Re-fetch docs.mnemosyne.site, audit against `MNEMOSYNE.md` v3.1.2, produce a corrections diff."

---

*Built by deep research across the docs and source, with one round of audit to catch what was missed on the first pass.*
