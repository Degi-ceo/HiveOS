# Memory docs — how they fit HiveOS

This folder holds the **authoritative Mnemosyne reference** for Hive. Read these instead of
re-fetching `docs.mnemosyne.site`.

## Files
- **`MNEMOSYNE.md`** — the master reference (~2,200 lines, 41 sections): BEAM + MEMORIA +
  Shared Surface architecture, all 17+ tools with every parameter, hybrid retrieval scoring,
  60+ config env vars, schema versioning, upgrade/rollback, multi-agent topology, failure
  catalog, cheat sheet. **Hive reads this once and treats it as the source of truth.**
- **`MNEMOSYNE_INTEGRATION_PHASES.md`** — 10 copy-paste Claude Code prompts (Phase 0–10) that
  install and wire Mnemosyne on the VPS. Run these on the Hetzner box (or Claude Code web with
  the VPS connected). These are the *Mnemosyne sub-build*; HiveOS Phase 2 calls them in.
- **`MNEMOSYNE_PACK_README.md`** — the original pack readme.

## "Hermes" vs "Hive" — read this once
The Mnemosyne pack was written against **Hermes Agent**, which is the **MCP host/runtime + CLI**
(`hermes ...`) that loads Mnemosyne as a memory plugin. In HiveOS, **Hive** is our agent and the
*orchestrator*; Hermes (if you choose to run it) is just the local runtime that exposes Mnemosyne
over MCP. They are not rivals:

- **Mnemosyne** = the memory engine (SQLite-backed: BEAM tiers + MEMORIA + triples). Always the core.
- **Hermes** = one way to host Mnemosyne's MCP tools locally (its CLI is what the pack's commands use).
- **Hive** = HiveOS's agent. It reaches Mnemosyne over MCP via `MNEMOSYNE_MCP_URL`, OR — until that
  is wired — via the local SQLite fallback already built into `memory/brain.py`.

So when the integration phases say `hermes ...`, that is configuring the **memory runtime**, not
replacing Hive. After Mnemosyne is up, set `MNEMOSYNE_MCP_URL` in `.env` and HiveOS's `brain.py`
uses it as the active layer; durable learnings are still promoted to the Obsidian vault.

## Order of operations
1. HiveOS Phase 0–1 (repo healthy, runner + gateway) — see `docs/BUILD_GUIDE.md`.
2. HiveOS **Phase 2** = stand up Mnemosyne using `MNEMOSYNE_INTEGRATION_PHASES.md`, then point
   `MNEMOSYNE_MCP_URL` at it. If you skip this, the local SQLite fallback keeps Hive working.
3. Continue HiveOS Phase 3+ (memory-keeper consolidation builds on whichever layer is active).
