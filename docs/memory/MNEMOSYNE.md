# MNEMOSYNE — Master Reference

**Version:** v3.1.2 (current as of audit, June 2026)
**Source:** docs.mnemosyne.site (50 of 56 pages fetched, comparisons + per-provider migration guides excluded as non-operational) + github.com/AxDSan/mnemosyne
**Purpose:** Single authoritative reference for an autonomous agent using Mnemosyne. Read this once; do not re-fetch docs. When in doubt, this file is the source of truth.

---

## How to use this file

This is a flat, dense reference. There are no tutorials — every section is "what exists, what it does, when to reach for it". Sections are independent; jump by table of contents.

**When something here contradicts the live docs:** the agent should still trust this file by default (the docs themselves are internally inconsistent — see §29). If a behaviour produces an unexpected error, verify against the installed version with `from mnemosyne import __version__; print(__version__)` and check `mem.get_stats()` for actual table state.

**Version markers:** `(v3.0+)`, `(v3.1+)`, `(v3.1.2+)` tag features by introduction. `[NOT IMPLEMENTED]` flags planned-but-absent features. `[LEGACY]` flags surfaces being phased out.

---

## Table of contents

1. Identity & invariants
2. Architecture — three coexisting layers (BEAM, MEMORIA, Shared Surface)
3. The five BEAM memory tiers
4. MEMORIA (v3.0+) — five new tables
5. Shared Surface (v3.1+)
6. Complete tool reference (17+ tools)
7. CLI command reference (mnemosyne + Hermes)
8. Hybrid retrieval — the full scoring formula
9. All recall filter parameters
10. Strict vs lenient fact matching (v3.1.2)
11. Sleep consolidation
12. Tiered episodic degradation
13. AAAK compression
14. Identity & isolation (banks / sessions / scope / author / channel)
15. Profile isolation (Hermes-specific)
16. Per-memory metadata fields
17. Configuration — full env-var matrix
18. Configuration — YAML structure (Hermes)
19. Configuration — constructor parameters
20. Hermes integration (the correct way)
21. Installation paths & extras
22. Schema versioning & detection
23. Upgrade procedure
24. Rollback strategies
25. Backups & disaster recovery
26. Monitoring & health
27. Performance characteristics & tuning
28. Use case patterns (5 official)
29. Multi-agent topology
30. Long-running agent patterns
31. Decision log pattern
32. Knowledge base pattern
33. Security model
34. Encryption [NOT IMPLEMENTED]
35. Data privacy
36. Cross-provider import (from Mem0/Letta/Zep/etc)
37. Things that do NOT exist
38. Common failures & how to recognise them
39. Documentation inconsistencies (defensive notes)
40. Quick reference cheat sheet
41. Glossary

---

## 1. Identity & invariants

**Name:** Mnemosyne. **Author:** Abdias Moya (@axdsan). **License:** MIT. **Language:** Python 3.9+. **Storage:** SQLite (single file per bank). **Embeddings:** local-first via fastembed (`BAAI/bge-small-en-v1.5`, 384-dim, int8).

**Truths that hold across every version:**

- Local-first. No network calls required for core operation. No telemetry.
- Single-node. No clustered/distributed mode. HA via SQLite replication (Litestream).
- SQLite single-writer, many-reader. WAL mode is mandatory for production concurrency.
- All "memories" carry: content (text), an importance score (0.0–1.0), a veracity label, a source string, timestamps, and (post-v2.1) author/channel identity.
- Working Memory is hot and capped. Episodic Memory is permanent unless explicitly deleted. Triples are permanent but auto-supersede when newer triples target the same `(subject, predicate)`.
- `sleep()` is the heartbeat that moves Working → Episodic. Without it, Working fills up and eventually evicts under capacity policy.
- Schema migrations are **automatic** on first connection after an upgrade. There is no `mnemosyne migrate` command.
- Versions move fast. Always verify with `print(mnemosyne.__version__)` before trusting any version-specific claim.

---

## 2. Architecture — three coexisting layers

Mnemosyne v3.1.2 layers **three architectures inside the same SQLite file**:

| Layer | Introduced | Tables | Purpose |
|---|---|---|---|
| **BEAM** | v2.0 | `working_memory`, `episodic_memory`, `triples`, `annotations` (v2.8+), `scratchpad`, `vec_episodes`, `fts_episodes`, `fts_working`, `consolidation_log` | Biological-inspired Episodic-Associative Memory — four tiers, hybrid retrieval, sleep consolidation |
| **MEMORIA** | v3.0 | `memoria_facts`, `memoria_timelines`, `memoria_instructions`, `memoria_preferences`, `memoria_kg` | Structured, category-typed memory (atomic facts, timelines, behavioural instructions, preferences, knowledge graph nodes) |
| **Shared Surface** | v3.1 (opt-in) | `shared_surface_*` (activated by `hermes memory surface`) | Cross-agent shared persistence — each agent has an isolated shared surface, but the surface lets fleets share without `channel_id` plumbing |

**The agent must understand:** all three layers coexist. `mem.remember()` writes BEAM (working_memory). `mem.add_triple()` writes the TripleStore portion of BEAM. MEMORIA accessors (when present in the installed version) write MEMORIA tables. The shared surface is its own tool family (`mnemosyne_shared_*`).

This is why the tool count grew from ~10 (v2.0) → 15 (v2.5) → 17+ (v3.1).

---

## 3. The five BEAM memory tiers

### 3.1 Working Memory

Hot, recent observations and conversation. Fast reads, capped capacity, time-to-live.

| Property | Value |
|---|---|
| Default capacity | 10,000 entries per session |
| Default TTL | 24h |
| Read latency | <100ms typical |
| Persistence | SQLite (survives restarts) |
| Eviction order when full | composite `importance × recency × frequency` |
| FTS5 index | `fts_working` (auto-maintained via triggers) |
| Written by | `remember()` |
| Consumed by | `recall()` (always queries WM + Episodic), `sleep()` (consolidates and evicts) |

Tunables: `MNEMOSYNE_WM_MAX_ITEMS`, `MNEMOSYNE_WM_TTL_HOURS`.

### 3.2 Episodic Memory

Long-term store. Hybrid-searchable. Tiered. Never written directly — only via `sleep()`.

| Property | Value |
|---|---|
| Capacity | Bounded by disk |
| Lifetime | Permanent unless `forget()` or `valid_until` expiry |
| Tiers | 1 (hot, 0–30d, full text), 2 (warm, 30–180d, ~400-char LLM summary), 3 (cold, 180+d, ~250-char entity-extracted signal) |
| Vector index | `vec_episodes` (sqlite-vec int8[384]) |
| FTS5 index | `fts_episodes` |
| Written by | `sleep()` only |
| Consumed by | `recall()` |

Tunables: `MNEMOSYNE_EP_LIMIT` (scan limit per recall), `MNEMOSYNE_TIER2_DAYS`, `MNEMOSYNE_TIER3_DAYS`.

### 3.3 Semantic Memory / TripleStore

Structured `(subject, predicate, object)` facts with temporal validity.

| Property | Value |
|---|---|
| Schema | `triples` table + `annotations` (v2.8+ E6 split) |
| Validity | `valid_from`, `valid_until` (NULL = current) |
| Ontology | None — free-form strings. Consistency is the author's responsibility |
| Auto-invalidation | Adding `(S, P, O_new)` sets `valid_until=now()` on any prior `(S, P, *)` |
| Confidence | 0.0–1.0 per triple |
| Lookup | Direct (no fuzzy search), via `triple_query()` |
| Class | `TripleStore` (separate from `Mnemosyne`) or `mem.add_triple()` shortcut |

### 3.4 Scratchpad

Session-bound transient workspace.

| Property | Value |
|---|---|
| Capacity | 1,000 entries (`MNEMOSYNE_SP_MAX`) |
| Lifetime | Cleared by `scratchpad_clear()` or session change |
| Persistence | SQLite-backed but session-scoped |
| Use case | Chain-of-thought, planning notes, draft outputs |
| **Anti-pattern** | Storing anything that should survive the session here |

### 3.5 Temporal Graph

Not a separate store — the time-aware view on the TripleStore. Same data, queried with `triple_query(..., as_of="2026-03-01T...")`.

**Limitations:** no multi-hop graph traversal. Filter only by direct `(s, p, o)` combinations.

---

## 4. MEMORIA — five new tables (v3.0+)

Structured beside BEAM, populated by extraction routines during writes that have `extract=True` or MEMORIA-aware operations.

| Table | Purpose |
|---|---|
| `memoria_facts` | Atomic, query-able factual statements |
| `memoria_timelines` | Time-anchored events and sequences |
| `memoria_instructions` | Behavioural instructions / how-to knowledge |
| `memoria_preferences` | User preferences (separate from working-memory prefs) |
| `memoria_kg` | Knowledge-graph nodes and edges (supplements the TripleStore) |

**Why MEMORIA exists:** BEAM tiers and the TripleStore answer "what happened" and "what is true". MEMORIA tables additionally tag *category* (fact vs preference vs instruction vs event-with-time vs graph node) so the agent can ask category-typed questions without losing flexibility.

**Multilingual MEMORIA (v3.1+):** language auto-detection for German, Russian, Chinese. Extraction applies language-specific patterns. **Polish is not in the supported list** — falls back to English-style extraction.

**MEMORIA accessors:** the docs don't enumerate exact Python method names; check `dir(mem)` or the `mnemosyne.memoria` module in your installed version.

---

## 5. Shared Surface (v3.1+, opt-in)

Cross-agent shared persistence. Each agent gets an isolated shared surface.

**Activation (Hermes side):**
```bash
hermes memory surface          # activate
hermes memory surface --list   # inspect
```

**Tools:** `mnemosyne_shared_*` family. Exact tool names not enumerated in docs as of v3.1.2 — discover via `hermes tools list | grep mnemosyne_shared`.

**When to use it:** as an alternative to `channel_id` for cross-agent collaboration. Cleaner because the surface is intrinsically shared rather than relying on filter conventions.

---

## 6. Complete tool reference

All tools prefixed `mnemosyne_`. Total 17+ depending on shared-surface activation.

### 6.1 Write tools

#### `mnemosyne_remember`
Store information in Working Memory. The most-used tool.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `content` | string | **required** | Self-contained, specific. Avoid "we discussed X" — store the substance |
| `source` | string | `"conversation"` | Free-form: `"user"`, `"tool"`, `"inference"`, `"slack"`. Grouping key during sleep consolidation |
| `importance` | float | `0.5` | 0.0–1.0. ≥0.7 strongly favoured for retention. Affects recall ranking |
| `scope` | string | `"session"` | `"session"` (current session only) or `"global"` (visible across sessions in this bank) |
| `valid_until` | ISO 8601 string | `None` | TTL expiry — memory disappears at recall after this |
| `extract_entities` | bool | `false` | Cheap regex + Levenshtein detection of named entities → stored as triples |
| `extract` | bool | `false` | LLM extracts 2–5 structured facts → stored as triples. Falls back: remote LLM → local GGUF → skip |
| `trust_tier` | string | `None` | Provenance label |
| `metadata` | dict | `None` | Arbitrary JSON sidecar |
| `author_id` | string | `None` | Multi-agent identity (v2.1+) |
| `author_type` | string | `None` | `"human"` / `"agent"` / `"system"` |
| `channel_id` | string | `None` | Cross-session shared channel |
| `veracity` | string | `None` | `"stated"` / `"inferred"` / `"tool"` / `"imported"` / `"unknown"` (v2.3+) |

**Returns:** memory ID (string).

**Importance heuristic:**
- 0.9–1.0: hard constraint, security-critical, user explicit preference
- 0.7–0.8: significant decision, deadline, identity fact
- 0.4–0.6: typical observation, conversation fact
- 0.1–0.3: ephemeral observation, expected to fade

**When `extract_entities=true` vs `extract=true`:**
- `extract_entities` is cheap, regex-based, surfaces named things ("PostgreSQL", "@alice", "#urgent"). Use almost always.
- `extract` calls an LLM — only use when content is fact-dense.

#### `mnemosyne_triple_add`
Store a structured `(subject, predicate, object)` fact in the temporal knowledge graph.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `subject` | string | **required** | Stable canonical form |
| `predicate` | string | **required** | **Be consistent** across calls |
| `object` | string | **required** | Value or target entity |
| `valid_from` | ISO 8601 | `None` | When fact became true. Defaults to now |
| `source` | string | `"inferred"` | Where it came from |
| `confidence` | float | `1.0` | 0–1 |

**Returns:** triple ID (int).

**Auto-invalidation:** new `(S, P, *)` sets `valid_until=now()` on any prior matching triples.

**Predicate consistency matters:** `reports_to` ≠ `reports to` ≠ `manager_is`. Pick one canonical form per relationship type and stick to it.

#### `mnemosyne_scratchpad_write`
Append a note to session scratchpad. Use for planning, intermediate reasoning, draft outputs.

| Parameter | Type | Notes |
|---|---|---|
| `content` | string | **required** |

**Returns:** note ID. **Lifetime:** session-bound.

#### `mnemosyne_shared_*` (v3.1+, Hermes-activated)
Cross-agent shared persistence tools. Family includes shared_remember / shared_recall etc; exact list depends on installation. Discover via `hermes tools list | grep shared`.

### 6.2 Read tools

#### `mnemosyne_recall`
Hybrid (vector + FTS5 + importance) search across Working + Episodic. The single most powerful retrieval tool.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `query` | string | **required** | Natural language |
| `top_k` | int | `5` | 1–50 |
| `from_date` | ISO 8601 | `None` | Lower bound |
| `to_date` | ISO 8601 | `None` | Upper bound |
| `source` | string | `None` | Filter by source label |
| `topic` | string | `None` | Filter by topic keyword |
| `author_id` | string | `None` | v2.1+ |
| `author_type` | string | `None` | v2.1+ |
| `channel_id` | string | `None` | v2.1+ |
| `veracity` | string | `None` | `stated`/`inferred`/`tool`/`imported`/`unknown` |
| `memory_type` | string | `None` | `FACT`/`PREFERENCE`/`DECISION`/etc |
| `temporal_weight` | float | `0.0` | 0–1. How much recency decay affects score |
| `query_time` | ISO 8601 | `None` | "Now" for temporal calculations — enables point-in-time queries |
| `temporal_halflife` | float | env (24h) | Hours |
| `vec_weight` | float | env (0.5) | Per-query override |
| `fts_weight` | float | env (0.3) | Per-query override |
| `importance_weight` | float | env (0.2) | Per-query override |

**Returns:** `list[dict]` with `id`, `content`, `score`, `source`, `importance`, `timestamp`, `tier`, plus filtered fields.

#### `mnemosyne_triple_query`
Graph lookup. Precise, no fuzziness.

| Parameter | Type | Notes |
|---|---|---|
| `subject` | string | Filter |
| `predicate` | string | Filter |
| `object` | string | Filter |
| `as_of` | ISO 8601 | Point-in-time — only triples with `valid_from ≤ as_of` |

Any combination works. **Returns:** `list[dict]`.

#### `mnemosyne_scratchpad_read`
Read all scratchpad entries for the current session. No parameters.

#### `mnemosyne_get_stats`
System inventory: counts per tier, DB size, banks, BEAM substats.

| Parameter | Type | Notes |
|---|---|---|
| `author_id` | string | Optional identity filter |
| `author_type` | string | Optional |
| `channel_id` | string | Optional |
| `bank` | string | Defaults to active bank |

**Returns:** `{total_memories, total_sessions, sources, last_memory, database, mode, banks, beam: {working_memory, episodic_memory, triples}}`.

#### `mnemosyne_diagnose`
DB integrity, index health, storage stats. PII-safe (excludes content).

No parameters.

#### `mem.get(memory_id)` — Python SDK method (v3.1+)
Deterministic retrieval by ID. No vector search, no ranking, no scoring. Cheap precise fetch when you already have the ID.

**Returns:** memory dict or raises if not found.

### 6.3 Mutate tools

#### `mnemosyne_update`
Modify existing memory's content or importance.

| Parameter | Type | Notes |
|---|---|---|
| `memory_id` | string | **required** |
| `content` | string | Optional |
| `importance` | float | Optional |

**Returns:** `True`/`False`. Only provided fields are touched.

#### `mnemosyne_invalidate`
Soft-supersede a memory. Marks it as no longer canonical, optionally pointing to replacement.

| Parameter | Type | Notes |
|---|---|---|
| `memory_id` | string | **required** — the outdated one |
| `replacement_id` | string | Optional — newer canonical version |

**Use for:** outdated-but-historically-true information.

#### `mnemosyne_forget`
Permanent hard delete by ID.

| Parameter | Type | Notes |
|---|---|---|
| `memory_id` | string | **required** |

**Returns:** `True`/`False`. **No undo.** Cascades to annotation triples (v2.5+).

**Use for:** wrong / sensitive / never-should-have-been-stored content.

#### `mnemosyne_scratchpad_clear`
Wipe entire session scratchpad. No parameters.

### 6.4 Lifecycle tools

#### `mnemosyne_sleep`
Run consolidation cycle. Selects WM entries older than TTL/2, groups by source, summarises (LLM → AAAK fallback), promotes to Episodic, evicts originals. Auto-runs tiered degradation at end.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `dry_run` | bool | `false` | Preview without mutation |
| `bank` | string | active | Target a specific bank |

**Returns:** `{consolidated, promoted, degradation: {tier1_to_tier2, tier2_to_tier3}}`.

**Auto-sleep is OFF by default** (`MNEMOSYNE_AUTO_SLEEP_ENABLED=false`). Drive sleep from cron, agent loop, or explicit call.

#### `mnemosyne_export`
Dump everything to JSON. For backup or cross-instance migration.

| Parameter | Type | Notes |
|---|---|---|
| `output_path` | string | **required** |

#### `mnemosyne_import`
Restore from JSON export, or import from another memory provider.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `input_path` | string | one of these | JSON file |
| `from` | string | one of these | `mem0` / `letta` / `zep` / `cognee` / `honcho` / `supermemory` / `hindsight` |
| `api_key` | string | conditional | For SDK/REST providers |
| `agent_file_path` | string | conditional | For Letta `.af` files |
| `force` | bool | `false` | Overwrite on ID conflicts |
| `dry_run` | bool | `false` | Validate without writing |
| `channel_id` | string | None | Override channel for imported memories |
| `generate_script` | bool | `false` | Generate Python migration script instead of running |
| `agentic` | bool | `false` | Generate AI agent instructions instead of running |
| `output_script` | string | None | Path to save generated script |
| `list_providers` | bool | `false` | List all supported source providers |

### 6.5 Tool selection matrix

| Intent | Tool |
|---|---|
| Save a fact / preference / decision | `mnemosyne_remember` |
| Save a structured (S, P, O) fact | `mnemosyne_triple_add` |
| Both narrative AND extract triples | `mnemosyne_remember(extract=true)` |
| Capture mentioned named entities cheaply | `mnemosyne_remember(extract_entities=true)` |
| Plan / chain-of-thought workspace | `mnemosyne_scratchpad_write` |
| Search by meaning | `mnemosyne_recall` |
| Look up specific relationship | `mnemosyne_triple_query` |
| Point-in-time fact ("what was true on date X") | `mnemosyne_triple_query(..., as_of=...)` |
| Recent activity ("today/this week") | `mnemosyne_recall(temporal_weight=0.6, temporal_halflife=24)` |
| Only user-stated info | `mnemosyne_recall(..., veracity="stated")` |
| Cheap retrieval by ID (no scoring) | `mem.get(id)` |
| Memory outdated (was true, now isn't) | `mnemosyne_invalidate(memory_id, replacement_id=new_id)` |
| Memory wrong (factually false) | `mnemosyne_forget(memory_id)` |
| Memory's importance changed | `mnemosyne_update(memory_id, importance=...)` |
| End of session / daily cron | `mnemosyne_sleep()` |
| Preview consolidation impact | `mnemosyne_sleep(dry_run=true)` |
| Health check | `mnemosyne_get_stats()` |
| DB integrity check | `mnemosyne_diagnose()` |
| Backup | `mnemosyne_export(output_path=...)` |
| Restore | `mnemosyne_import(input_path=...)` |
| Cross-agent shared write | `mnemosyne_shared_*` (v3.1+, Hermes-activated) |

### 6.6 MCP-native vs Hermes-plugin tool surface

| Tool | MCP-native | Hermes plugin | Python SDK |
|---|:---:|:---:|:---:|
| `mnemosyne_remember` | ✓ | ✓ | ✓ |
| `mnemosyne_recall` | ✓ | ✓ | ✓ |
| `mnemosyne_sleep` | ✓ | ✓ | ✓ |
| `mnemosyne_get_stats` | ✓ | ✓ | ✓ |
| `mnemosyne_scratchpad_*` (3) | ✓ | ✓ | ✓ |
| `mnemosyne_triple_add/query` | — | ✓ | ✓ |
| `mnemosyne_forget` | — | ✓ | ✓ |
| `mnemosyne_update` | — | ✓ | ✓ |
| `mnemosyne_invalidate` | — | ✓ | ✓ |
| `mnemosyne_export/import` | — | ✓ | ✓ |
| `mnemosyne_diagnose` | — | ✓ | ✓ |
| `mnemosyne_shared_*` (v3.1+) | — | ✓ when activated | ✓ |

---

## 7. CLI command reference

### 7.1 Mnemosyne CLI (`mnemosyne ...` or `python -m mnemosyne...`)

```bash
mnemosyne remember "<content>"                  # Store from CLI
mnemosyne recall "<query>"                      # Search
mnemosyne update <id> "<content>"               # Update
mnemosyne delete <id>                           # Delete (alias for forget)
mnemosyne stats                                 # Stats
mnemosyne sleep                                 # Manual consolidation
mnemosyne export --output backup.json           # JSON export
mnemosyne import --input backup.json            # JSON import
mnemosyne mcp                                   # MCP server (stdio)
mnemosyne mcp --transport sse --port 8080       # MCP SSE
mnemosyne mcp --bank <name>                     # Per-bank MCP
mnemosyne mcp --transport sse --port 8080 --bank <name>  # Combined

# REST server
python -m mnemosyne --host 0.0.0.0 --port 8090

# Also valid (used in Fly.io deploy example)
python -m mnemosyne.mcp_server --transport sse --host 0.0.0.0 --port 8080

# DR module
python -m mnemosyne.dr backup
python -m mnemosyne.dr restore <file>
python -m mnemosyne.dr health
python -m mnemosyne.dr emergency

# Benchmark
python -m mnemosyne.benchmark

# Hermes plugin registration
python -m mnemosyne.install

# Migration scripts (manual mode only — auto-migration is on by default)
python scripts/migrate_from_legacy.py --db mnemosyne.db
python scripts/migrate_triplestore_split.py --dry-run
python scripts/migrate_triplestore_split.py
```

### 7.2 Hermes-side commands (`hermes ...`)

```bash
# Configuration
hermes config get memory.provider
hermes config set memory.provider mnemosyne
hermes gateway restart                          # Reload after config changes

# Verification — the right way
hermes doctor | grep -i memory                  # Real health check
hermes tools list | grep mnemosyne              # Tool registration check
hermes plugins list                             # Plugin loaded?

# ⚠️ DO NOT use `hermes memory status` — known display bug, always says
# "Built-in: always active" regardless of whether Mnemosyne is loaded.

# Memory operations from Hermes CLI
hermes mnemosyne version                        # Installed version
hermes mnemosyne stats                          # Bank-local stats
hermes mnemosyne stats --global                 # Cross-bank stats
hermes mnemosyne sleep                          # Consolidation trigger
hermes mnemosyne export --output ~/backup.json
hermes mnemosyne import --input ~/backup.json

# Cross-provider import
hermes mnemosyne import --list-providers
hermes mnemosyne import --from mem0 --api-key sk-xxx
hermes mnemosyne import --from mem0 --api-key sk-xxx --dry-run
hermes mnemosyne import --from letta --agent-file-path ./agent.af
hermes mnemosyne import --from zep --api-key sk-xxx --channel-id team
hermes mnemosyne import --from hindsight --file ./export.json
hermes mnemosyne import --from supermemory --api-key sk-xxx
hermes mnemosyne import --from cognee
hermes mnemosyne import --from honcho

# Agentic fallback (for providers without clean API)
hermes mnemosyne import --from zep --generate-script
hermes mnemosyne import --from zep --agentic
hermes mnemosyne import --from zep --output-script ./migrate_zep.py

# Single-tool invocation (test pattern)
hermes --tool mnemosyne_remember content="Test" veracity="stated"
hermes --tool mnemosyne_recall query="Test"

# Toolset control
hermes tools enable memory                      # Re-enable if disabled
# ⚠️ hermes tools disable memory                # NEVER — see §38

# Shared surface
hermes memory surface                           # Activate cross-agent surface
hermes memory surface --list                    # Inspect
```

---

## 8. Hybrid retrieval — the full scoring formula

### 8.1 Formula (v3.1.2)

```
base = (vec_score × vec_weight + fts_score × fts_weight + importance × importance_weight)
       / (vec_weight + fts_weight + importance_weight)

final = base
      × (1.0 - temporal_weight + temporal_weight × recency_decay)
      × tier_weight
      × veracity_weight

where:
  recency_decay   = 0.5 ^ (age_hours / temporal_halflife)
  tier_weight     = 1.0 (hot, 0–30d) / 0.5 (warm, 30–180d) / 0.25 (cold, 180+d)
  veracity_weight = 1.0 stated / 0.8 unknown / 0.7 inferred / 0.6 imported / 0.5 tool
```

### 8.2 Default weights (env-driven)

| Knob | Default | Env var |
|---|---|---|
| `vec_weight` | 0.5 | `MNEMOSYNE_VEC_WEIGHT` |
| `fts_weight` | 0.3 | `MNEMOSYNE_FTS_WEIGHT` |
| `importance_weight` | 0.2 | `MNEMOSYNE_IMPORTANCE_WEIGHT` |
| `temporal_weight` | 0.0 (off) | passed per-query |
| `temporal_halflife` | 24h | `MNEMOSYNE_TEMPORAL_HALFLIFE_HOURS` |

Weights auto-normalise — relative ratios matter, not absolute values.

### 8.3 Patterns to use

**Default — balanced semantic + text:**
```python
recall("what database do we use?", top_k=5)
```

**Exact match (error codes, IDs, version strings) — boost FTS, drop vector:**
```python
recall("error code E501", vec_weight=20, fts_weight=60, importance_weight=20)
```

**Conceptual query — boost vector:**
```python
recall("how does authentication work?", vec_weight=70, fts_weight=20, importance_weight=10)
```

**Recent / lately:**
```python
recall("what did we discuss this week?", temporal_weight=0.6, temporal_halflife=48)
```

**Point-in-time:**
```python
recall("active decisions", query_time="2026-01-31T23:59:59Z", temporal_weight=0.1)
```

**Only user-stated info:**
```python
recall("user preferences", veracity="stated")
```

### 8.4 Performance (with `sqlite-vec` installed)

| Query type | p50 | p99 |
|---|---|---|
| Vector only | 35ms | 120ms |
| FTS5 only | 15ms | 45ms |
| Hybrid (default) | 65ms | 180ms |
| At 2K episodics | 7ms avg | 8.6ms p95 |

---

## 9. All recall filter parameters

Combinable in any combination. All optional:

| Filter | Purpose |
|---|---|
| `source=...` | Match exact source label |
| `topic=...` | Match topic keyword |
| `from_date=ISO`, `to_date=ISO` | Time window |
| `author_id=...` | Specific author |
| `author_type="human"/"agent"/"system"` | Author category |
| `channel_id=...` | Cross-session channel |
| `veracity="stated"/"inferred"/"tool"/"imported"/"unknown"` | Trust tier |
| `memory_type="FACT"/"PREFERENCE"/"DECISION"/...` | Category |

Plus the scoring tunables in §8.

---

## 10. Strict vs lenient fact matching (v3.1.2)

**Default behaviour as of v3.1.2:** **strict matching**. Per docs:
- Single-token strict fact queries must be 5+ chars and non-stopword to match (previously silently rejected)
- Entity prefix similarity requires 30% min length ratio
- Generic any-word-matches-any-fact ("permissive") matching is OFF

**Opt back into permissive:**
```bash
export MNEMOSYNE_LENIENT_FACT_MATCH=1
```

**When recall suddenly returns fewer/narrower results after a routine upgrade:** this is why. Either tighten queries to be more specific, or opt back in to lenient.

---

## 11. Sleep consolidation

The heartbeat. Without `sleep()` Working Memory never becomes Episodic.

### 11.1 What happens

1. **Select**: WM entries older than `TTL/2` (default 12h)
2. **Group**: cluster candidates by `source`
3. **Summarise**: for each group, generate a summary via:
   - Remote LLM (if `MNEMOSYNE_LLM_BASE_URL` set), OR
   - Local GGUF (TinyLlama default), OR
   - AAAK text substitution (final fallback)
4. **Promote**: insert summary into `episodic_memory` with `summary_of` referencing original IDs
5. **Evict**: delete originals from `working_memory`
6. **Log**: `consolidation_log` table records the operation
7. **Degrade**: tier 1→2 and tier 2→3 transitions run automatically

### 11.2 Triggers

| Trigger | Default |
|---|---|
| Explicit call: `mnemosyne_sleep()` | always available |
| Auto-sleep | **OFF** (`MNEMOSYNE_AUTO_SLEEP_ENABLED=false`) |
| Auto-sleep threshold (when enabled) | 50 writes |
| Graceful shutdown | depends on integration |

### 11.3 Performance

~1,000 entries in <5s. Runs in background thread; doesn't block agent operations.

### 11.4 When to call

- End of long working session
- After ingesting a large batch
- Before low-activity periods
- Daily (cron) if running 24/7
- Inspect: `mnemosyne_sleep(dry_run=true)` to preview

---

## 12. Tiered episodic degradation

Old episodics never get deleted — they degrade. Runs automatically at end of every `sleep()`.

| Tier | Age | Content | Recall weight |
|---|---|---|:---:|
| 1 | 0–30 days | Full original text | 1.00× |
| 2 | 30–180 days | LLM summary, ~400 chars | 0.50× |
| 3 | 180+ days | Entity-extracted signal, ~250 chars | 0.25× |

### 12.1 Smart compression (tier 2→3)

`MNEMOSYNE_SMART_COMPRESS=true` default. Doesn't just truncate — scores each sentence by signal density and keeps the highest-scoring until char budget filled.

**Signal scoring:**

| Signal | Score boost |
|---|:---:|
| Acronyms (API, AWS) | +3 |
| Tech terms (Docker, Kubernetes) | +4 |
| Security terms (token, auth) | +3 |
| Infrastructure (deploy, scale) | +2 |
| Urgency (critical, blocker) | +3 |
| Preference markers (prefers, hates) | +2 |

Budget: `MNEMOSYNE_TIER3_MAX_CHARS=300` default.

**Implication:** a critical fact in the last paragraph of an old log survives instead of getting truncated. Don't assume "180+ days = gone". Tier 3 memories are still queryable; they just need stronger semantic matches to surface.

---

## 13. AAAK compression

**A**daptive **A**ssociative **A**bstraction **K**ernel. Standalone text-substitution utility. Fallback summariser during `sleep()` when no LLM is available.

| Property | Value |
|---|---|
| Direction | One-way (lossy, no decode) |
| Passes | Category prefix shortening (`conversation:` → `conv:`), common-phrase substitution, whitespace normalisation |
| Typical reduction | 30–50% |
| Import | `from mnemosyne.aaak import encode` |

**Implication:** critical facts that must remain verbatim should be stored as triples (uncompressed) or with very high importance. AAAK output may lose detail.

---

## 14. Identity & isolation

Five orthogonal isolation dimensions, from coarsest to finest:

| Dimension | Scope | Backing |
|---|---|---|
| **Bank** | Entirely separate SQLite DB | `bank=` constructor arg or `MNEMOSYNE_MCP_BANK` env |
| **Session** | Visibility scope within one DB | `session_id` constructor arg |
| **Scope** | Per-memory: visible only to current session, or globally | `scope="session"` or `"global"` on `remember()` |
| **Channel** | Cross-session shared context | `channel_id` |
| **Author** | Who created what | `author_id` + `author_type` |

### 14.1 Banks

- Fully separate SQLite database under `<data_dir>/banks/<name>/`
- Strong isolation — no cross-bank queries
- Create implicitly via `Mnemosyne(bank="work")` or run MCP server with `--bank project-a`
- Stats can be per-bank or across banks (default)

### 14.2 Sessions

- Default `"default"`
- Different sessions = different visibility for session-scoped memories
- Global-scoped memories are visible across sessions in the same bank

### 14.3 Scope

- `"session"` (default) — only visible in this session
- `"global"` — visible across all sessions in this bank

### 14.4 Channels & authors (v2.1+)

- `author_id` — free-form ID identifying who created the memory
- `author_type` — `"human"` / `"agent"` / `"system"`
- `channel_id` — shared context identifier
- All three are filter parameters on `recall()` and `get_stats()`

### 14.5 Pattern: multi-agent fleet on shared bank

```python
# Each subagent
mem = Mnemosyne(
    bank="hermes-prod",
    session_id="research-subagent-default",
    author_id="research-subagent",
    author_type="agent",
    channel_id="hermes-main",
)

# Orchestrator can query across the whole fleet
orch.recall("findings about postgres",
            author_type="agent",
            channel_id="hermes-main")

# Or filter to one subagent
orch.recall("research findings", author_id="research-subagent")
```

---

## 15. Profile isolation (Hermes-specific)

Hermes has the concept of "profiles" (language profiles like English vs Italian, project profiles, etc). Mnemosyne supports two isolation modes:

| `profile_isolation` | Behaviour |
|---|---|
| `false` (default) | **All profiles share one database.** Memories from English profile appear when using Italian profile and vice versa |
| `true` | Each profile gets its own SQLite file. Bank name resolution: profile name → basename of `$HERMES_HOME` → `"default"` |

**Important:** Mnemosyne tools do **not** have a `profile` parameter. Isolation is transparent at the DB level. You cannot tell which bank you're writing to from the tool call.

**Config:**
```yaml
memory:
  provider: mnemosyne
  mnemosyne:
    profile_isolation: true
```

---

## 16. Per-memory metadata fields

Every memory across `working_memory` and `episodic_memory`:

| Field | Purpose | Settable on `remember()` |
|---|---|:---:|
| `id` | UUIDv4 unique identifier | auto |
| `content` | The text | **required** |
| `embedding` | 384-dim int8 vector (lazy on first recall) | auto |
| `tags` | JSON array | via metadata |
| `importance` | 0.0–1.0 retention/ranking | ✓ |
| `source` | Origin label (free-form) | ✓ |
| `created_at` | Creation timestamp | auto |
| `accessed_at` | Last read timestamp | auto |
| `access_count` | Read frequency counter | auto |
| `timestamp` | App-level event time | auto |
| `session_id` | Session scope | via constructor |
| `metadata_json` | Arbitrary JSON sidecar | ✓ |
| `summary_of` | IDs of WM entries this summarises (Episodic only) | auto |
| `recall_count` | Times recalled | auto |
| `last_recalled` | Last recall timestamp | auto |
| `valid_until` | TTL expiry (ISO 8601) | ✓ |
| `superseded_by` | Pointer to newer canonical version | via `invalidate()` |
| `scope` | `"session"` or `"global"` | ✓ |
| `veracity` | Trust label (v2.3+) | ✓ |
| `memory_type` | `"FACT"`/`"PREFERENCE"`/etc (v2.3+) | ✓ |
| `tier` | 1/2/3 for Episodic | auto |
| `author_id` | Multi-agent (v2.1+) | ✓ |
| `author_type` | (v2.1+) | ✓ |
| `channel_id` | (v2.1+) | ✓ |
| `trust_tier` | Provenance | ✓ |

---

## 17. Configuration — full env-var matrix

### Storage & memory tiers

| Var | Default | Purpose |
|---|---|---|
| `MNEMOSYNE_DATA_DIR` | `~/.hermes/mnemosyne/data` | SQLite root |
| `MNEMOSYNE_DB_PATH` | — | Explicit DB file (overrides DATA_DIR) |
| `MNEMOSYNE_HOST` | — | REST bind host |
| `MNEMOSYNE_PORT` | — | REST bind port |
| `MNEMOSYNE_WM_MAX_ITEMS` | `10000` | Working Memory cap per session |
| `MNEMOSYNE_WM_TTL_HOURS` | `24` | Working Memory TTL |
| `MNEMOSYNE_EP_LIMIT` | `50000` | Episodic scan limit per recall |
| `MNEMOSYNE_SLEEP_BATCH` | `5000` | Sleep batch size |
| `MNEMOSYNE_SP_MAX` | `1000` | Scratchpad cap |
| `MNEMOSYNE_RECENCY_HALFLIFE` | `168` | Recency decay halflife (hours, 1 week) |

### Retrieval weights

| Var | Default | Purpose |
|---|---|---|
| `MNEMOSYNE_VEC_TYPE` | `int8` | `float32` / `int8` / `bit` |
| `MNEMOSYNE_VEC_WEIGHT` | `0.5` | Vector in hybrid score |
| `MNEMOSYNE_FTS_WEIGHT` | `0.3` | FTS5 in hybrid score |
| `MNEMOSYNE_IMPORTANCE_WEIGHT` | `0.2` | Importance in hybrid score |
| `MNEMOSYNE_TEMPORAL_HALFLIFE_HOURS` | `24` | Default temporal halflife |
| `MNEMOSYNE_BEAM_OPTIMIZATIONS` | `false` | Benchmark-mode (OR semantics, bigger scans) |

### Tiered degradation

| Var | Default | Purpose |
|---|---|---|
| `MNEMOSYNE_TIER2_DAYS` | `30` | Days before tier 1→2 |
| `MNEMOSYNE_TIER3_DAYS` | `180` | Days before tier 2→3 |
| `MNEMOSYNE_TIER1_WEIGHT` | `1.0` | Recall multiplier |
| `MNEMOSYNE_TIER2_WEIGHT` | `0.5` | Recall multiplier |
| `MNEMOSYNE_TIER3_WEIGHT` | `0.25` | Recall multiplier |
| `MNEMOSYNE_DEGRADE_BATCH` | `100` | Max rows per degradation cycle |
| `MNEMOSYNE_SMART_COMPRESS` | `true` | Entity-aware sentence extraction |
| `MNEMOSYNE_TIER3_MAX_CHARS` | `300` | Tier 3 char budget |

### Veracity weights

| Var | Default |
|---|---|
| `MNEMOSYNE_STATED_WEIGHT` | `1.0` |
| `MNEMOSYNE_INFERRED_WEIGHT` | `0.7` |
| `MNEMOSYNE_TOOL_WEIGHT` | `0.5` |
| `MNEMOSYNE_IMPORTED_WEIGHT` | `0.6` |
| `MNEMOSYNE_UNKNOWN_WEIGHT` | `0.8` |

### Embeddings (local)

| Var | Default |
|---|---|
| `MNEMOSYNE_EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` |

### Embeddings (remote API, v3.1+)

| Var | Default | Purpose |
|---|---|---|
| `MNEMOSYNE_EMBEDDING_API_URL` (v3.1.1+) | — | OpenAI-compatible endpoint URL |
| `MNEMOSYNE_EMBEDDING_API_KEY` (v3.1.1+) | — | API key |
| `MNEMOSYNE_EMBEDDINGS_VIA_API` | not set | Set to `true` to route all embedding through API |
| `OPENROUTER_BASE_URL` [LEGACY] | `https://openrouter.ai/api/v1` | Pre-3.1.1 name |
| `OPENROUTER_API_KEY` [LEGACY] | — | Pre-3.1.1 name |

Note: Jina model dimensions auto-detected when using API embeddings.

### Sleep summarisation LLM (local GGUF)

| Var | Default | Purpose |
|---|---|---|
| `MNEMOSYNE_LLM_ENABLED` | `true` | Enable LLM during sleep |
| `MNEMOSYNE_LLM_BASE_URL` | — | Remote OpenAI-compatible endpoint |
| `MNEMOSYNE_LLM_API_KEY` | — | API key for remote |
| `MNEMOSYNE_LLM_MODEL` | — | Remote model name |
| `MNEMOSYNE_LLM_MAX_TOKENS` | `2048` | Output cap |
| `MNEMOSYNE_LLM_N_THREADS` | `4` | Local LLM threads |
| `MNEMOSYNE_LLM_N_CTX` | `2048` | Local LLM context window |
| `MNEMOSYNE_LLM_REPO` | `TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF` | Local GGUF repo |
| `MNEMOSYNE_LLM_FILE` | `tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf` | Local GGUF file |

### Host-LLM passthrough (Hermes auth client)

| Var | Default | Purpose |
|---|---|---|
| `MNEMOSYNE_HOST_LLM_ENABLED` | `false` | Route LLM calls through Hermes |
| `MNEMOSYNE_HOST_LLM_PROVIDER` | — | Override provider |
| `MNEMOSYNE_HOST_LLM_MODEL` | — | Override model |
| `MNEMOSYNE_HOST_LLM_N_CTX` | `32000` | Prompt context budget |

### Multi-agent identity

| Var | Purpose |
|---|---|
| `MNEMOSYNE_AUTHOR_ID` | Default author |
| `MNEMOSYNE_AUTHOR_TYPE` | Default `human`/`agent`/`system` |
| `MNEMOSYNE_CHANNEL_ID` | Default channel |

### Lifecycle / migration / mode

| Var | Default | Purpose |
|---|---|---|
| `MNEMOSYNE_AUTO_SLEEP_ENABLED` | `false` | Fire sleep automatically after N writes |
| `MNEMOSYNE_AUTO_MIGRATE` | `1` | Set `0` to opt out of automatic schema migration |
| `MNEMOSYNE_LENIENT_FACT_MATCH` (v3.1.2+) | not set | Set `1` to opt back into permissive matching |

### SHMR (consolidation harmony pass)

| Var | Default |
|---|---|
| `MNEMOSYNE_SHMR_BATCH_SIZE` | `50` |
| `MNEMOSYNE_SHMR_MAX_ITERATIONS` | `3` |
| `MNEMOSYNE_SHMR_SIMILARITY_THRESHOLD` | `0.70` |
| `MNEMOSYNE_SHMR_HARMONY_THRESHOLD` | `0.60` |

### MCP / REST / logging

| Var | Default | Purpose |
|---|---|---|
| `MNEMOSYNE_MCP_BANK` | `default` | Default bank for MCP server |
| `MNEMOSYNE_LOG_TOOLS` | `false` | Log every tool call |
| `MNEMOSYNE_LOG_LEVEL` | — | Set `debug` for verbose SQL + embedding logs |
| `MNEMOSYNE_CORS_ORIGINS` | open in dev | Restrict in production REST |
| `MNEMOSYNE_BENCHMARK_PURE_RECALL` | — | BEAM benchmark mode toggle |

---

## 18. Configuration — YAML structure (Hermes)

The correct, production-grade Hermes `config.yaml` block:

```yaml
memory:
  # The built-in MemoryStore (MEMORY.md in system prompt).
  # Mnemosyne works INDEPENDENTLY of this — both true and false are valid.
  memory_enabled: true
  user_profile_enabled: true

  # Tell Hermes to use Mnemosyne as the provider.
  provider: mnemosyne

  mnemosyne:
    # Profile isolation:
    #   false (default) = all profiles share one DB
    #   true            = each profile gets its own SQLite file
    #                     Bank name resolution:
    #                     profile name → $HERMES_HOME basename → "default"
    profile_isolation: false

    # DB location. Mnemosyne also respects MNEMOSYNE_DATA_DIR env var.
    data_dir: /opt/mnemosyne/data

    # Active bank
    bank: hermes-main

    # Auto-sleep: keep OFF in production. Drive sleep from cron or agent loop.
    auto_sleep: false
    sleep_threshold: 50

    # Vector quantisation
    vector_type: int8                  # float32 / int8 / bit

    # Auto-inject relevant memories before every LLM call
    auto_context: true
    context_injection:
      enabled: true
      max_memories: 5
      min_relevance: 0.7

    # Skip on remember() — boilerplate not worth persisting
    ignore_patterns:
      - "be ACTIVE"
      - "nothing to change"
      - "skill.*refined"

    # Optional override of embedding model
    embedding_model: BAAI/bge-small-en-v1.5
```

**Critical:** `memory_enabled` and `provider` are **independent**. Setting `memory_enabled: false` removes only the built-in file-based MemoryStore; Mnemosyne tools still work. The toolset itself is gated separately via `hermes tools enable/disable memory`.

Config lookup order: `./config.yaml` → `~/.hermes/config.yaml` → env vars.

---

## 19. Configuration — constructor parameters (Python SDK)

```python
Mnemosyne(
    session_id: str = "default",
    db_path: Path | None = None,        # Explicit DB; overrides bank
    bank: str | None = None,            # Bank name; under data_dir/banks/<bank>/
    author_id: str | None = None,
    author_type: str | None = None,     # "human" / "agent" / "system"
    channel_id: str | None = None,
)
```

Module-level convenience functions operate on a default `Mnemosyne(session_id="default")` instance:

```python
from mnemosyne import (
    remember, recall, get_context, get_stats, forget, update,
    sleep, sleep_all_sessions,
    scratchpad_write, scratchpad_read, scratchpad_clear,
    add_triple, query_triples,
)
```

Lazy-loaded subsystems exposed as properties:

```python
mem.stream         # MemoryStream — push/pull events
mem.compressor     # MemoryCompressor — AAAK/dict/RLE/semantic
mem.patterns       # PatternDetector — temporal/content/sequence
mem.delta_sync     # DeltaSync — between Mnemosyne instances
mem.plugins        # PluginManager
mem.beam           # BeamMemory — lower-level (includes remember_batch)
```

---

## 20. Hermes integration — the correct way

This is the single most important page in the docs for Hermes users. Memorise it.

### 20.1 Install

```bash
# Inside Hermes's venv (recommended)
pip install mnemosyne-memory[all]

# If PEP 668 blocks you on Debian 13+ / Ubuntu 24.04+:
pip install mnemosyne-memory[all] --break-system-packages
# or
python3 -m venv mnemosyne-env && source mnemosyne-env/bin/activate
pip install mnemosyne-memory[all]
```

System Hermes venv typically lives at:
- `/usr/local/lib/hermes-agent/venv/` (system install)
- `~/.hermes/hermes-agent/venv/` (user install)

The MemoryProvider import path must resolve. If you install in the wrong venv, Hermes won't find Mnemosyne.

### 20.2 Configure

Edit `~/.hermes/config.yaml` with the §18 block.

### 20.3 The two controls (CRITICAL)

There are two **separate, independent** controls:

| Config | What it controls |
|---|---|
| `memory.memory_enabled` (YAML) | Whether the built-in file-based MemoryStore exists and MEMORY.md appears in the system prompt. When `false`, the built-in store is gone but **Mnemosyne tools still work** |
| `hermes tools enable/disable memory` | Whether ANY memory tools (built-in AND Mnemosyne) are given to the AI model |

**The right setup for using Mnemosyne:**
- Keep `memory_enabled: true` (or `false` — your choice)
- **Never run `hermes tools disable memory`** unless you want to remove all memory tools

If you *want* to hide the built-in `memory` tool but keep Mnemosyne tools — **you can't, currently.** Both are gated by the same toolset.

### 20.4 Verify (the right way)

```bash
# Real health check — DO use this
hermes doctor | grep -i memory

# Tool registration — should list 10+ mnemosyne_* tools
hermes tools list | grep mnemosyne

# Direct functional test
hermes --tool mnemosyne_remember content="Setup test" veracity="stated"
hermes --tool mnemosyne_recall query="Setup test"
```

**Do NOT use `hermes memory status`** — it's a known display bug, always prints "Built-in: always active" regardless of actual state.

### 20.5 If tools don't show up

1. `pip list | grep mnemosyne` — confirm installed
2. Confirm `provider: mnemosyne` in YAML
3. Confirm you haven't run `hermes tools disable memory` (re-enable: `hermes tools enable memory`)
4. Restart: `hermes gateway restart`

### 20.6 Three plugin hooks (automatic)

| Hook | Fires |
|---|---|
| `pre_llm_call` | Before every LLM call → injects relevant memories |
| `on_session_start` | On session start → stores conversation start |
| `post_tool_call` | After every tool call → stores tool results |

### 20.7 Auto-context format

When `auto_context: true`, injected memories appear in the prompt as:

```
═════════════════════════════════════════════════
MNEMOSYNE MEMORY (persistent local context)
[2026-04-05 10:23] PREF|Neovim>Vim
[2026-04-05 09:15] PROJ|FluxSpeak AI
[2026-04-05 08:42] LOC|America/New_York
═════════════════════════════════════════════════
```

Filtered by `min_relevance: 0.7` and capped by `max_memories: 5`.

---

## 21. Installation paths & extras

### 21.1 Extras matrix

```bash
pip install mnemosyne-memory                    # Core: FTS5 keyword only
pip install mnemosyne-memory[embeddings]        # + fastembed for semantic
pip install mnemosyne-memory[llm]               # + ctransformers for local LLM
pip install mnemosyne-memory[all]               # Everything (recommended)
pip install sqlite-vec                          # Native vector C ext (optional, recommended for >100K)
pip install mnemosyne-hermes                    # Hermes plugin wrapper
```

### 21.2 Three install strategies

| Strategy | Use when |
|---|---|
| `pip install mnemosyne-memory[all]` | Standard install in venv |
| `pip install --break-system-packages` | Debian 13+/Ubuntu 24.04+ PEP 668, personal machine |
| `pipx install mnemosyne-memory` | Standalone CLI tool, isolated |

### 21.3 Verify

```bash
python -c "from mnemosyne import __version__; print(__version__)"
# Expected: 3.1.2

python -c "import sqlite_vec; print(sqlite_vec.__version__)"
python -c "import fastembed; print(fastembed.__version__)"
```

Pre-download the embedding model (avoid first-recall latency):
```bash
python -c "from fastembed import TextEmbedding; m = TextEmbedding('BAAI/bge-small-en-v1.5'); list(m.embed(['warmup']))"
```

---

## 22. Schema versioning & detection

Mnemosyne uses automatic schema migrations on first connection after upgrade.

### 22.1 Detect current schema version

```python
import sqlite3, pathlib
db = pathlib.Path.home() / '.hermes' / 'mnemosyne' / 'data' / 'mnemosyne.db'
conn = sqlite3.connect(str(db))
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
names = [t[0] for t in tables]
if 'memoria_facts' in names:
    print('DB schema: v3.0+ (MEMORIA)')
elif 'annotations' in names:
    print('DB schema: v2.8+ (E6 TripleStore split)')
elif 'episodic_memory' in names:
    print('DB schema: v2.0+ (BEAM)')
else:
    print('DB schema: v1.x (legacy)')
conn.close()
```

### 22.2 Schema migration history

| Version | Migration | Description |
|---|---|---|
| 1.0 → 2.0 | Schema upgrade | Add BEAM tiered memory, FTS5 indices |
| 2.0 → 2.1 | Schema upgrade | Add `session_id` to episodic |
| 2.1 → 2.2 | Schema upgrade | Add temporal graph indexes |
| 2.2 → 2.8 (E6) | `e6_triplestore_split` | Split `triples` into `triples` + `annotations` |
| 2.8 → 3.0 (MEMORIA) | Auto-create | 5 new tables: `memoria_facts/timelines/instructions/preferences/kg` |

### 22.3 Manual migration scripts (when `MNEMOSYNE_AUTO_MIGRATE=0`)

```bash
python scripts/migrate_from_legacy.py --db mnemosyne.db          # v1.x sources
python scripts/migrate_triplestore_split.py --dry-run            # preview E6
python scripts/migrate_triplestore_split.py                      # apply E6
```

All migration scripts are idempotent — safe to re-run.

### 22.4 Snapshot baseline before upgrade

```python
import sqlite3, pathlib
db = pathlib.Path.home() / '.hermes' / 'mnemosyne' / 'data' / 'mnemosyne.db'
conn = sqlite3.connect(str(db))
schema = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
for row in schema:
    print(row[0] + ';')
conn.close()
```

Diff before/after to see what migration changed.

---

## 23. Upgrade procedure

### 23.1 Patch / minor upgrade (e.g. v3.1.2 → v3.1.3)

1. Backup: run your DR backup script — verify the `.gz` file
2. Snapshot schema: see §22.4
3. Activate the right venv
4. `pip install --upgrade mnemosyne-memory`
5. `hermes gateway restart`
6. Verify: `hermes mnemosyne version`, `hermes doctor | grep -i memory`
7. Diff schema: snapshot post-upgrade and compare to baseline
8. Tail logs for migration messages: `journalctl -u hermes -n 100 | grep -iE "migration|e6|memoria"`
9. Smoke-test recall on a known seeded memory

### 23.2 Major version upgrade (v2.7 → v3.0)

Same as patch, plus:
- BEFORE step 4: check `pre_e6_backup` files: `ls -la /opt/mnemosyne/data/banks/*/mnemosyne.db.pre_e6_backup`
- AFTER step 4: first init takes longer (auto-migration runs)
- AFTER step 7: expect new MEMORIA tables (`memoria_facts/timelines/instructions/preferences/kg`)
- Logs should show: `E6: auto-migrated N annotation rows from triples -> annotations.`
- Dashboard may show "thousands of memories to review" — that's normal post-migration re-indexing. Let it finish.

### 23.3 v2.x → v3.x specifics

What happens to your data on first run:
- v2.7 databases auto-migrate via E6 on first init. Backup written automatically to `{db}.pre_e6_backup`.
- v3.0 creates 5 new MEMORIA tables via `CREATE TABLE IF NOT EXISTS`. Existing tables untouched.
- All existing memories, triples, embeddings remain intact.

---

## 24. Rollback strategies

Three options, in order of preference:

### 24.1 Roll back the package

```bash
pip install 'mnemosyne-memory==3.0.0'    # or any pinned version
hermes gateway restart
```

Shared-surface tables (v3.1) remain in the DB but are ignored by v3.0.

### 24.2 Restore E6 auto-backup

```bash
cp ~/.hermes/mnemosyne/data/mnemosyne.db.pre_e6_backup \
   ~/.hermes/mnemosyne/data/mnemosyne.db
pip install 'mnemosyne-memory==2.7.0'
hermes gateway restart
```

### 24.3 Export, nuke, re-import (nuclear)

```bash
hermes mnemosyne export --output ~/backup.json
rm ~/.hermes/mnemosyne/data/mnemosyne.db
pip install 'mnemosyne-memory==2.7.0'
hermes gateway restart
hermes mnemosyne import --input ~/backup.json
```

### 24.4 Loop-of-update bug

Symptom: agent suggests "you need to update Mnemosyne" repeatedly even though update is already applied.

Cause: agent sees outdated docs and keeps trying.

Fix: kill the session, start fresh. The update is already there.

---

## 25. Backups & disaster recovery

SQLite is a single file. Backup is simple. **Verify is the part people forget.**

### 25.1 Hot copy (safe while server is running)

```bash
# Simple
cp mnemosyne.db mnemosyne-backup-$(date +%Y%m%d).db

# Safer — uses SQLite's online backup API
sqlite3 mnemosyne.db ".backup to mnemosyne-backup.db"
sqlite3 mnemosyne-backup.db "PRAGMA integrity_check"   # → "ok"
```

### 25.2 DR module

```python
from mnemosyne.dr.recovery import create_backup, restore_backup, verify_integrity

backup_path = create_backup("/opt/mnemosyne/data/banks/hermes-main/mnemosyne.db",
                             "/opt/mnemosyne/backups/")
# Returns gzip-compressed backup with SHA-256

verify_integrity("/opt/mnemosyne/data/banks/hermes-main/mnemosyne.db")

restore_backup(backup_path, "/opt/mnemosyne/data/banks/hermes-main/mnemosyne.db")
```

Features:
- gzip compression (~70% reduction)
- SHA-256 integrity
- Automatic 6-hourly backups (when DR daemon enabled)
- Rotation: keeps last 10
- CLI: `python -m mnemosyne.dr backup | restore | emergency | health`

### 25.3 Continuous replication (Litestream)

```yaml
# /etc/litestream.yml
access-key-id: AKIA...
secret-access-key: ...

dbs:
  - path: /opt/mnemosyne/data/banks/hermes-main/mnemosyne.db
    replicas:
      - url: s3://my-backup-bucket/mnemosyne
        retention: 720h               # 30 days
        snapshot-interval: 24h
```

```bash
sudo systemctl enable litestream
sudo systemctl start litestream
# Restore: litestream restore -o mnemosyne.db s3://my-backup-bucket/mnemosyne
```

### 25.4 Verify script

```bash
#!/bin/bash
BACKUP_FILE=$1
if ! sqlite3 "$BACKUP_FILE" "PRAGMA integrity_check;" | grep -q "ok"; then
    echo "BACKUP FAILED: integrity check failed"
    exit 1
fi
WORKING=$(sqlite3 "$BACKUP_FILE" "SELECT COUNT(*) FROM working_memory;")
EPISODIC=$(sqlite3 "$BACKUP_FILE" "SELECT COUNT(*) FROM episodic_memory;")
echo "Backup verified: $WORKING working, $EPISODIC episodic"
```

### 25.5 Retention policy

| Cadence | Frequency | Retention |
|---|---|---|
| Hourly | Every hour | 24 hours |
| Daily | Daily | 30 days |
| Weekly | Weekly | 12 weeks |
| Monthly | Monthly | 12 months |

### 25.6 Test restores

**A backup you cannot restore is worthless.** Monthly restore drill to staging environment.

---

## 26. Monitoring & health

### 26.1 REST health endpoint

```
GET /health
```

```json
{
  "status": "healthy",
  "version": "3.1.2",
  "db_connected": true,
  "db_size_mb": 12.5,
  "working_count": 42,
  "episodic_count": 128,
  "semantic_count": 64,
  "last_sleep_at": "2026-04-25T13:00:00Z",
  "uptime_seconds": 86400
}
```

### 26.2 Prometheus metrics endpoint

```
GET /metrics
```

Exposed metrics:
```
mnemosyne_working_memory_entries
mnemosyne_episodic_memory_entries
mnemosyne_semantic_memory_entries
mnemosyne_query_latency_ms{quantile="0.5"}
mnemosyne_query_latency_ms{quantile="0.99"}
mnemosyne_consolidation_last_run
mnemosyne_db_size_bytes
```

### 26.3 Alert thresholds

| Metric | Target | Alert at |
|---|---|---|
| Query latency p50 | <100ms | >200ms |
| Query latency p99 | <500ms | >1000ms |
| DB size | <1 GB | >5 GB |
| Working memory usage | <80% max | >95% max |
| Time since last sleep | <2× interval | >3× interval |

**Most important alert:** `MemoryNotConsolidating` (time since last sleep > 3× expected interval). If this fires, Working Memory fills and eviction starts losing data.

### 26.4 Programmatic polling (Python)

```python
stats = mem.get_stats()
if stats["beam"]["working_memory"] > 0.9 * 10_000:
    mem.sleep()   # emergency consolidation
```

### 26.5 Built-in plugins

- `LoggingPlugin` — logs every operation
- `MetricsPlugin` — emits metrics
- `FilterPlugin` — applies `ignore_patterns` filtering

Tool call logging: `MNEMOSYNE_LOG_TOOLS=true` (verbose; debug only).

---

## 27. Performance characteristics & tuning

### 27.1 Benchmarks (with `sqlite-vec`, single CPU core)

| Operation | 1K entries | 10K entries | 100K entries |
|---|---|---|---|
| Write | 12ms | 15ms | 18ms |
| Vector search | 15ms | 35ms | 85ms |
| FTS5 search | 5ms | 8ms | 20ms |
| Hybrid search | 45ms | 65ms | 150ms |
| Consolidation | 2s | 5s | 25s |

**At 2K episodics:** hybrid recall = 7.0ms avg, 8.6ms p95.

### 27.2 Memory footprint per entry

| Component | Per entry | 10K total |
|---|---|---|
| Working Memory | ~50KB | ~500MB |
| Episodic Memory | ~2KB | ~20MB |
| Semantic Memory | ~500B | ~5MB |
| Vector index | ~6KB | ~60MB |
| FTS5 index | ~2KB | ~20MB |

Working Memory is by far the heaviest — another reason to consolidate via `sleep()`.

### 27.3 Throughput (BEAM benchmark)

- WM writes: 58 ops/sec
- Episodic inserts (with embedding): 47 ops/sec
- Scratchpad writes: 172 ops/sec
- Sleep consolidation: 300 items in 33ms

### 27.4 Tuning knobs

1. **WAL mode** — `PRAGMA journal_mode=WAL` for concurrent reads/writes. **Essential** for production.
2. **Batch writes** — `mem.beam.remember_batch([{...}, ...])` instead of loop
3. **Embedding cache** — LRU 512, automatic
4. **Lower WM cap** — `MNEMOSYNE_WM_MAX_ITEMS=5000` for faster queries
5. **Aggressive TTL** — `MNEMOSYNE_WM_TTL_HOURS=12` for faster eviction
6. **Regular consolidation** — periodic `sleep()` calls
7. **Vector type** — `int8` → `bit` for max compression, `float32` for accuracy
8. **Episodic scan limit** — `MNEMOSYNE_EP_LIMIT` controls how many rows recall scans
9. **Sleep batch size** — `MNEMOSYNE_SLEEP_BATCH`
10. **Degrade batch** — `MNEMOSYNE_DEGRADE_BATCH`

### 27.5 Storage

**SSD strongly recommended.** SQLite is I/O-bound. HDD shows 5–10× slower queries.

---

## 28. Use case patterns (5 official)

| Pattern | Description | Complexity |
|---|---|---|
| **Personal Assistant** | Remember user preferences and history | Low |
| **Multi-Agent Team** | Shared memory across agent team | Medium |
| **Long-Running Tasks** | Maintain context across sessions | Medium |
| **Knowledge Base** | Structured domain knowledge | Medium |
| **Decision Log** | Track decisions and rationale | Low |

### 28.1 Success metrics targets

| Metric | Target | Measurement |
|---|---|---|
| Context relevance | >80% top-3 | Manual evaluation |
| User satisfaction | >4.0/5.0 | Survey |
| Task completion | >90% | Task tracking |
| Memory accuracy | >95% | Fact checking |

---

## 29. Multi-agent topology

Two clean patterns:

### 29.1 Isolated DBs (no cross-recall)

```python
agent_a = Mnemosyne(session_id="agent-a", db_path="/data/agent-a/mem.db")
agent_b = Mnemosyne(session_id="agent-b", db_path="/data/agent-b/mem.db")
# Agent B cannot see Agent A's memories. Period.
```

### 29.2 Shared DB with identity (fleet)

```python
# Orchestrator
orch = Mnemosyne(
    bank="hermes-prod",
    author_id="hermes-orchestrator",
    author_type="agent",
    channel_id="hermes-main",
)

# Each subagent
research = Mnemosyne(
    bank="hermes-prod",
    author_id="research-subagent",
    author_type="agent",
    channel_id="hermes-main",
)
coder = Mnemosyne(
    bank="hermes-prod",
    author_id="coder-subagent",
    author_type="agent",
    channel_id="hermes-main",
)

# Subagent records a finding
research.remember(
    "pgvector beats sqlite-vec at >1M rows by 3x",
    importance=0.8, veracity="tool", source="research")

# Orchestrator sees all subagent work in one channel
findings = orch.recall("performance findings",
                       author_type="agent",
                       channel_id="hermes-main")

# Filter to one subagent
mine = orch.recall("research findings", author_id="research-subagent")
```

### 29.3 Cross-machine sharing via MCP

```bash
# Hetzner side
mnemosyne mcp --transport sse --port 8080 --bank hermes-main

# Laptop side
MNEMOSYNE_AUTHOR_ID=kamil-laptop \
MNEMOSYNE_AUTHOR_TYPE=human \
MNEMOSYNE_CHANNEL_ID=hermes-main \
  your-mcp-client --url http://<hetzner-ip>:8080/sse
```

### 29.4 SQLite concurrency caveat

SQLite is **single-writer, many-reader**. For high write rates from many subagents:
- Enable WAL mode (essential)
- Consider per-agent DBs with periodic export/import sync
- Or use REST/MCP server as serialisation point (it owns the writes)

### 29.5 Shared Surface alternative (v3.1+)

If `channel_id` plumbing is awkward, activate the shared surface:

```bash
hermes memory surface
```

Then use `mnemosyne_shared_*` tools instead of channel-based filtering.

---

## 30. Long-running agent patterns

### 30.1 Reconnect across restarts

```python
# Day 1
mem = Mnemosyne(session_id="hermes-prod-2026", db_path="/opt/mnemosyne/data/mnemosyne.db")
mem.remember("Decided on MiniMax M2.7 as primary model.", importance=0.9, source="decision")

# Day 30 — process restart, same code reconnects
mem = Mnemosyne(session_id="hermes-prod-2026", db_path="/opt/mnemosyne/data/mnemosyne.db")
context = mem.recall("model decisions", top_k=10)
# Day-1 decision still there.
```

### 30.2 Scope: session vs project

```python
# Session-bounded (this run only)
mem.remember("Currently debugging the auth flow.", scope="session", importance=0.4)

# Permanent project knowledge
mem.remember("Auth uses OAuth 2.0 PKCE.", scope="global", importance=0.9)
```

### 30.3 Pre-deploy backup pattern

```python
from mnemosyne.dr.recovery import create_backup, restore_backup
create_backup(db_path="/opt/mnemosyne/data/mnemosyne.db",
              backup_path="/backups/pre-deploy.bak")

# If something goes wrong post-deploy:
restore_backup(backup_path="/backups/pre-deploy.bak",
               db_path="/opt/mnemosyne/data/mnemosyne.db")
```

### 30.4 Descriptive session IDs

`hermes-prod-2026` is OK. `hermes-prod-2026-05-website-redesign` is better. Grep future-you will thank you.

---

## 31. Decision log pattern

Low-complexity, high-value pattern for an agent tracking its own decisions:

```python
# Every decision gets both a memory and a triple
def record_decision(aspect: str, decision: str, rationale: str, decided_at: str = None):
    decided_at = decided_at or datetime.now(timezone.utc).isoformat()
    
    mem.remember(
        f"Decision on {aspect}: {decision}\n\nRationale: {rationale}",
        importance=0.9,
        source="decision",
        veracity="stated",
        scope="global",
        metadata={"aspect": aspect, "decided_at": decided_at},
    )
    
    # Triple form for "what's the current decision on X" lookups
    ts.add(
        subject="Project",
        predicate=f"decided_{aspect}",
        object=decision,
        valid_from=decided_at,
        source="decision",
        confidence=1.0,
    )
    # Auto-invalidates older decisions on this aspect

# Usage
record_decision(
    aspect="primary_model",
    decision="MiniMax M2.7 via Token Plan",
    rationale="Cost-effective, supports tool calling, available globally"
)

# Later: "what's the current primary model decision?"
current = ts.query(subject="Project", predicate="decided_primary_model")
# Only active triples returned

# "what was the primary model decision on March 1?"
historical = ts.query(subject="Project", predicate="decided_primary_model",
                      as_of="2026-03-01T00:00:00Z")
```

---

## 32. Knowledge base pattern

For an agent consulting a body of knowledge:

```python
# Load documentation
mem.remember("API authentication uses OAuth 2.0 with PKCE flow.",
             source="documentation", importance=0.9,
             extract_entities=True)
mem.remember("Rate limit is 1000 requests per minute per API key.",
             source="documentation", importance=0.7)

# Structured equivalent
ts.add("API", "auth_method", "OAuth 2.0 PKCE")
ts.add("API", "rate_limit", "1000/min")

# Refresh outdated knowledge
new_id = mem.remember("API v2 uses OAuth 2.0 with refresh-token rotation.",
                      source="documentation", importance=0.9)
mem.invalidate(old_id, replacement_id=new_id)

# Triple form auto-handles it
ts.add("API", "auth_method", "OAuth 2.0 + refresh rotation")
# Previous triple auto-marked valid_until=now
```

Support-ticket shape:
```python
def handle_ticket(ticket):
    docs = mem.recall(ticket.description, top_k=5, source="documentation")
    response = generate_response(docs, ticket)
    mem.remember(f"Ticket {ticket.id}: {response.summary}",
                 source="support", importance=0.5,
                 metadata={"ticket_id": ticket.id})
    return response
```

---

## 33. Security model

### 33.1 Threat model

| Threat | Mitigation |
|---|---|
| Local data theft | Filesystem permissions (`chmod 600` DB, dedicated user) |
| Network data theft | No network exposure by default (local SQLite) |
| Prompt injection | Parameterised SQL throughout |
| Memory poisoning | `importance` + `veracity` weighting, `scope` isolation, `get_contaminated()` audit |
| Data loss | DR module + Litestream + verified backups |

### 33.2 Built-in features

- **Local-first** — SQLite by default, no network calls
- **Local embeddings** — fastembed/BAAI/bge-small-en-v1.5, no text leaves the box
- **Session isolation** — separate `session_id` = separate scope
- **Bank isolation** — separate `bank` = separate SQLite file
- **DR module** — `mnemosyne.dr.recovery` for backup/restore/verify
- **Parameterised SQL** — input sanitisation throughout
- **PII-safe diagnostics** — `diagnose()` excludes content

### 33.3 Access control

Mnemosyne does **not** expose a network API by default. All access is via Python SDK or Hermes plugin, governed by application code and OS-level permissions.

For multi-tenant: use separate `db_path` per tenant.

### 33.4 Filesystem hardening

```bash
# DB file permissions
chmod 600 /opt/mnemosyne/data/banks/hermes-main/mnemosyne.db
chown mnemosyne:mnemosyne /opt/mnemosyne/data/banks/hermes-main/mnemosyne.db

# Encrypted volume (Linux LUKS)
cryptsetup luksFormat /dev/sdb1
cryptsetup luksOpen /dev/sdb1 mnemosyne-data
mount /dev/mapper/mnemosyne-data /data
```

---

## 34. Encryption — [NOT IMPLEMENTED]

Per the docs `/security/encryption` page, application-level encryption at rest is **not yet implemented**. The `Mnemosyne()` constructor does **not** accept an `encryption_key` parameter.

### 34.1 What you should do today

| Layer | Use |
|---|---|
| Disk | LUKS full-disk encryption |
| File | `chmod 600` on the DB file |
| Process | Dedicated non-root user, container/VM with encrypted storage |

### 34.2 Planned (per docs, not committed)

| Data | Planned | Notes |
|---|:---:|---|
| Memory content | ✓ | All text |
| Embeddings | ✗ | Performance reasons |
| Metadata | partial | Tags, source; not timestamps |
| Semantic triples | ✓ | Subject, predicate, object |

**Bottom line:** if your threat model requires encryption at rest, use filesystem-level. Application-level is a planned feature, not a current one.

---

## 35. Data privacy

### 35.1 PII handling — not automatic

Mnemosyne does **not** auto-detect or redact PII. Best practices:

1. **Flag via metadata:** `metadata={"pii": True}` on `remember()`
2. **Always set `valid_until` for PII** (30/60/90 days):
   ```python
   from datetime import datetime, timedelta
   mem.remember("User email is alice@example.com",
                importance=0.5,
                valid_until=(datetime.now() + timedelta(days=30)).isoformat())
   ```
3. **Isolate PII** by `session_id` or `bank`
4. **`forget(memory_id)`** for "right to be forgotten" requests

### 35.2 Veracity audit

Periodic audit of non-stated memories (potentially poisoned via tool outputs or inference):

```python
contaminated = mem.get_contaminated()  # Python SDK
# Review, then update to "stated" or forget noise
```

### 35.3 Principles

1. **Data minimisation** — only `remember()` what's needed
2. **Local processing** — all data + embeddings local by default
3. **User control** — full CRUD via `remember`, `recall`, `forget`
4. **Transparency** — `get_stats()` exposes everything

---

## 36. Cross-provider import

Mnemosyne v3.1+ has built-in importers for 7 memory providers via `hermes mnemosyne import --from <provider>`:

| Provider | Source | Identity preservation |
|---|---|---|
| Mem0 | SDK or REST API | `user_id` + `agent_id` → `author_id` |
| Letta | SDK, `.af` file, or REST | agent identity from `.af` metadata |
| Zep | SDK (per-session) | `user_id` from session owner |
| Cognee | SDK (Kuzu graph export) | node labels and relationships → triples |
| Honcho | SDK (Workspace/Peer/Session/Message) | preserved |
| SuperMemory | REST API | preserved |
| Hindsight | JSON export file | preserved |

### 36.1 Quick start

```bash
# List supported providers
hermes mnemosyne import --list-providers

# Mem0
hermes mnemosyne import --from mem0 --api-key sk-xxx
hermes mnemosyne import --from mem0 --api-key sk-xxx --dry-run

# Letta from agent file
hermes mnemosyne import --from letta --agent-file-path ./agent.af

# Zep into a specific channel
hermes mnemosyne import --from zep --api-key sk-xxx --channel-id team

# Hindsight from export
hermes mnemosyne import --from hindsight --file ./export.json

# JSON re-import (from Mnemosyne export)
hermes mnemosyne import --input ./export.json
```

### 36.2 Pipeline (every importer)

1. **Extract** — pull from source (SDK / REST / file)
2. **Validate** — check required fields
3. **Transform** — map provider-specific fields to Mnemosyne
4. **Import** — write into Working Memory with identity preserved

All metadata, timestamps, identity, and original IDs preserved.

### 36.3 Agentic fallback

For providers without clean APIs or when source SDK can't be installed:

```bash
hermes mnemosyne import --from zep --generate-script     # generate Python script
hermes mnemosyne import --from zep --agentic             # generate AI agent instructions
hermes mnemosyne import --from zep --output-script ./migrate_zep.py
```

---

## 37. Things that do NOT exist

For agent honesty — when asked "can Mnemosyne do X" and X is one of these, the answer is no:

1. **Multi-hop graph traversal** — TripleStore only supports direct `(s, p, o)` queries with `as_of`
2. **Distributed / clustered mode** — single-node only
3. **Cross-DB sync** beyond `delta_sync` (per-instance) and export/import
4. **Automatic PII detection** — flag manually via metadata
5. **Web dashboard** — CLI / Python / REST / MCP only
6. **Team collaboration features** — single-user / single-fleet by design
7. **Automatic background sleep on a timer** — opt-in via env var or driven by cron/agent loop
8. **Application-level encryption at rest** — planned, not implemented (see §34)
9. **Predefined ontology for triples** — predicates are free-form
10. **AAAK decode/expand** — compression is one-way and lossy
11. **Cross-machine sync beyond MCP SSE** — or manual export/import
12. **Automatic conflict resolution** for concurrent updates — last-write-wins
13. **`profile` parameter on tools** — profile isolation is transparent at DB level
14. **`encryption_key` constructor parameter** — does not exist
15. **`mnemosyne migrate` command** — schema migrations are automatic
16. **Polish multilingual MEMORIA** — only DE/RU/ZH have language-specific patterns; Polish falls back to English

---

## 38. Common failures & how to recognise them

### 38.1 The catastrophic one

**Symptom:** All `mnemosyne_*` tools vanish from `hermes tools list` after some Hermes operation.

**Cause:** Someone (you, the agent, an outdated guide) ran `hermes tools disable memory`. This removes **both** built-in AND Mnemosyne tools — they share the same toolset gate.

**Fix:** `hermes tools enable memory && hermes gateway restart`.

**Prevention:** **never** run `hermes tools disable memory`. Document this prominently in your agent's instructions.

### 38.2 Display bug on health check

**Symptom:** `hermes memory status` always says "Built-in: always active" regardless of state.

**Cause:** Known docs-confirmed display bug.

**Fix:** Use `hermes doctor | grep -i memory` and `hermes tools list | grep mnemosyne` instead.

### 38.3 PEP 668 on install

**Symptom:** `error: externally-managed-environment` on Debian 13+/Ubuntu 24.04+.

**Fix (best):** Use a venv. **Fix (override):** `pip install ... --break-system-packages`. **Fix (pipx):** `pipx install mnemosyne-memory`.

### 38.4 Database locked

**Symptom:** `DatabaseError: database is locked`.

**Cause:** Concurrent writes without WAL.

**Fix:** `sqlite3 /path/to/mnemosyne.db "PRAGMA journal_mode=WAL;"` or `Mnemosyne(wal_mode=True)` if your version supports the kwarg.

### 38.5 Mnemosyne tools missing after install

**Symptom:** `hermes tools list | grep mnemosyne` returns nothing.

**Diagnosis:**
1. `pip list | grep mnemosyne` — installed?
2. Installed in the right venv? Check Hermes's venv vs where pip installed
3. `provider: mnemosyne` in config?
4. `hermes gateway restart` after config changes

### 38.6 Recall too narrow after upgrade (v3.1.2)

**Symptom:** Queries that worked before return fewer/no results.

**Cause:** v3.1.2 made strict fact matching default.

**Fix:** `export MNEMOSYNE_LENIENT_FACT_MATCH=1 && hermes gateway restart` or tighten queries to be more specific.

### 38.7 Dashboard shows thousands of memories after upgrade

**Symptom:** After upgrade, dashboard shows thousands of memories to review.

**Cause:** Schema migration re-indexing (one-time per major version).

**Fix:** Let it finish.

### 38.8 Loop of "update Mnemosyne" suggestions

**Symptom:** Agent repeatedly suggests updating Mnemosyne even when it's current.

**Cause:** Agent sees outdated docs and tries to "fix".

**Fix:** Kill the session, start fresh.

### 38.9 Italian/English profiles share memories

**Cause:** `profile_isolation: false` (the default).

**Fix:** Set `profile_isolation: true`, restart Hermes for each profile.

### 38.10 Embedding model download fails on first recall

**Symptom:** First `recall()` hangs or errors with network message.

**Fix:** Pre-download: `python -c "from fastembed import TextEmbedding; m = TextEmbedding('BAAI/bge-small-en-v1.5'); list(m.embed(['warmup']))"`.

### 38.11 sqlite-vec not loading

**Symptom:** Vector search seems slow, or you see fallback warnings.

**Fix:** `pip install sqlite-vec`, restart. Without it, vector queries use in-memory numpy fallback (fine up to ~100K entries, slower beyond).

### 38.12 Missing column / table errors

**Symptom:** SQL errors after upgrade.

**Fix:** Try `hermes gateway restart` first (auto-migration may not have run). If still failing: `python scripts/migrate_from_legacy.py --db <path>` or export/nuke/re-import.

---

## 39. Documentation inconsistencies (defensive notes)

The live docs at docs.mnemosyne.site have known inconsistencies. When the agent sees one, here's what to do:

| Inconsistency | Reality |
|---|---|
| Some sidebars say "Backups", others "Disaster Recovery" — same URL `/operations/backups` | Same page, just renamed |
| `/operations/migration` vs `/getting-started/updating` | Both exist; `/getting-started/updating` is newer and more comprehensive |
| MCP server invoked as `mnemosyne mcp` (Quick Start) vs `python -m mnemosyne.mcp_server` (Fly.io) | Both work; first is CLI wrapper |
| `MNEMOSYNE_DATA_DIR` (Configuration) vs `MNEMOSYNE_DB_PATH` (Fly.io) | `DATA_DIR` is modern preferred; `DB_PATH` overrides for a specific file |
| `Memory()` class (older examples) vs `Mnemosyne()` (current) | Use `Mnemosyne`. `Memory` is older API |
| Quick Start says verify command should print "2.8.0" | Docs typo. Actual current is 3.1.2 |
| Some pages list 15 tools, others 10, latest 17+ | 17+ is correct after v3.1 shared surface |

**Agent rule:** when in doubt about installed behaviour, verify against the running instance via `print(mnemosyne.__version__)`, `dir(mem)`, `mem.get_stats()`, and the table-name check in §22.1.

---

## 40. Quick reference cheat sheet

```
PERSIST                  remember | triple_add | scratchpad_write | shared_*
RETRIEVE                 recall | triple_query | scratchpad_read | get | get_context | get_stats
INSPECT                  get_stats | diagnose | get_contaminated
MODIFY                   update | invalidate | forget | scratchpad_clear
LIFECYCLE                sleep | sleep_all_sessions | degrade_episodic
ANALYSE                  detect_patterns | summarize_patterns
COMPRESS                 compress | decompress | compress_memories (AAAK/dict/RLE/semantic)
STREAM                   stream.on | stream.listen | enable_streaming
SYNC                     sync_to | sync_from (DeltaSync)
PORTABILITY              export_to_file | import_from_file
DR                       create_backup | restore_backup | verify_integrity
CROSS-PROVIDER           hermes mnemosyne import --from <mem0/letta/zep/cognee/honcho/supermemory/hindsight>

DEFAULT WEIGHTS:         vec 0.5  +  fts 0.3  +  importance 0.2  (auto-normalised)
TIER WEIGHTS:            hot 1.0  •  warm 0.5  •  cold 0.25
VERACITY WEIGHTS:        stated 1.0 > unknown 0.8 > inferred 0.7 > imported 0.6 > tool 0.5
WORKING MEM TTL:         24h, evicts after 12h via sleep()
TIER THRESHOLDS:         30d → tier 2,  180d → tier 3
TOOL SURFACE:            17+ mnemosyne_* tools (BEAM core + MEMORIA + shared surface)
INTERFACES:              Python SDK | Hermes plugin | MCP (stdio/SSE) | REST API
ISOLATION dimensions:    bank > session_id > scope > author_id > channel_id

FILTERS on recall:
  from_date, to_date, source, topic, author_id, author_type,
  channel_id, veracity, memory_type
TUNING on recall:
  top_k, vec_weight, fts_weight, importance_weight,
  temporal_weight, temporal_halflife, query_time

THE TWO CONTROLS (Hermes):
  memory.memory_enabled (YAML)   — built-in MemoryStore on/off (Mnemosyne unaffected)
  hermes tools enable memory     — toolset on/off (controls BOTH built-in AND Mnemosyne tools)

THE THREE PROHIBITIONS:
  1. NEVER `hermes tools disable memory`     — removes ALL memory tools
  2. NEVER use `hermes memory status`         — known display bug
  3. NEVER assume `encryption_key` exists    — encryption NOT implemented yet
```

---

## 41. Glossary

| Term | Meaning |
|---|---|
| **AAAK** | Adaptive Associative Abstraction Kernel — text-substitution fallback summariser. Lossy, one-way |
| **annotations** (table) | v2.8+ E6 split — annotation rows separated from `triples` table |
| **`author_id`/`author_type`/`channel_id`** | Multi-agent identity fields (v2.1+) |
| **auto-context** | Hermes plugin feature — injects relevant memories before every LLM call |
| **bank** | Fully separate SQLite database under `data_dir/banks/<name>/` |
| **BEAM** | Biological-inspired Episodic-Associative Memory — Mnemosyne's 4-tier architecture (Working + Episodic + Triples + Scratchpad) |
| **BEAM benchmark** | The benchmark named BEAM (Benchmark for Evaluating Agent Memory) — different from the architecture |
| **BM25** | TF-IDF-flavoured ranking algorithm used by FTS5 |
| **`channel_id`** | Cross-session shared context identifier |
| **consolidation** | Working → Episodic promotion via `sleep()` |
| **degradation** | Tier 1 → 2 → 3 compression as memories age |
| **DR module** | `mnemosyne.dr.recovery` — backup, restore, verify, with gzip + SHA-256 |
| **E6 split** | v2.8 schema change splitting `triples` into `triples` + `annotations` |
| **Episodic Memory** | Long-term store, tiered, hybrid-searchable. Written only by `sleep()` |
| **fastembed** | Local ONNX runtime loading `BAAI/bge-small-en-v1.5` for 384-dim embeddings |
| **FTS5** | SQLite's full-text search extension. Requires SQLite 3.45+ |
| **`get(id)`** | v3.1+ deterministic retrieval by ID. No scoring |
| **Hermes** | The agent framework Mnemosyne is most commonly integrated with |
| **hybrid retrieval** | The combined `(vec × fts × importance)` scoring formula |
| **importance** | 0.0–1.0 score on every memory. Affects retention and recall ranking |
| **invalidate** | Soft supersede (sets `superseded_by`). Use when outdated |
| **forget** | Hard delete. Use when wrong / sensitive |
| **MCP** | Model Context Protocol. Stdio or SSE transport |
| **MEMORIA** | v3.0+ architecture adding 5 category-typed tables alongside BEAM |
| **Memory** | [LEGACY] older API class name. Use `Mnemosyne` |
| **`memoria_facts/timelines/instructions/preferences/kg`** | The 5 MEMORIA tables (v3.0+) |
| **`memory_enabled`** | YAML config — controls built-in MemoryStore. Independent of Mnemosyne |
| **`memory_type`** | Per-memory category label (`FACT`/`PREFERENCE`/`DECISION`/etc) |
| **MEMORY.md** | The built-in file-based memory system prompt section (separate from Mnemosyne) |
| **pre_e6_backup** | Auto-created backup file `{db}.pre_e6_backup` before v2.8 E6 migration |
| **PEP 668** | Python "externally managed environment" protection on Debian 13+/Ubuntu 24.04+ |
| **Personal Assistant** (pattern) | Low-complexity use case: remember user preferences and history |
| **profile_isolation** | YAML config — `true` = each Hermes profile gets own DB; `false` = all share one |
| **Scratchpad** | Session-bound workspace for chain-of-thought. Cleared on session end or explicit clear |
| **Semantic Memory** | The TripleStore — structured `(s, p, o)` facts |
| **`session_id`** | Visibility scope within one DB |
| **shared surface** | v3.1+ opt-in cross-agent persistence (Hermes-activated) |
| **SHMR** | Semantic Harmony Memory Refinement — clustering/harmony pass during consolidation |
| **smart compression** | Tier 2→3 sentence-scoring instead of truncation |
| **sqlite-vec** | Optional native C extension for vector search inside SQLite. Recommended for >100K entries |
| **strict fact matching** | v3.1.2+ default — single-token queries need 5+ chars, non-stopword; entity prefix 30% min ratio |
| **Temporal Graph** | Time-aware view on TripleStore. Supports `as_of` |
| **tier** | Episodic age bucket: 1 (hot, 0–30d) / 2 (warm, 30–180d) / 3 (cold, 180+d). Weights 1.0 / 0.5 / 0.25 |
| **TripleStore** | Class managing structured `(s, p, o)` facts. Separate from `Mnemosyne` class |
| **veracity** | Trust label per memory: `stated` 1.0 > `unknown` 0.8 > `inferred` 0.7 > `imported` 0.6 > `tool` 0.5 |
| **WAL mode** | SQLite "Write-Ahead Logging" journal. Essential for concurrent reads/writes |
| **Working Memory** | Hot tier. Default 10K entries, 24h TTL. Consolidated to Episodic by `sleep()` |

---

*End of master reference. v3.1.2, audited against docs.mnemosyne.site (50 of 56 pages) and github.com/AxDSan/mnemosyne. Updated June 7, 2026.*

*This file supersedes the four earlier files (`MNEMOSYNE_AGENT_OVERVIEW.md`, `MNEMOSYNE_OPERATIONS_REFERENCE.md`, `MNEMOSYNE_CAPABILITIES.md`, `MNEMOSYNE_BUILD_PHASES.md`) and the `MNEMOSYNE_CORRECTIONS.md` addendum. When this file contradicts those, this file is authoritative.*
