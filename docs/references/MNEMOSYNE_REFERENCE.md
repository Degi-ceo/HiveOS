# MNEMOSYNE_REFERENCE — full deep audit (code-level)

> Code-level audit of `/home/user/mnemosyne` for the HiveOS unification effort.
> Mnemosyne (Abdias Moya / @axdsan, MIT, Python 3.9+) = **"zero-dependency AI memory
> that works everywhere — SQLite-backed, sub-millisecond, Hermes-first."** It is
> HiveOS's **designated active memory layer** and, alongside OpenJarvis/Hermes, a
> **top-tier REUSE source** (it's Python and ~22% of its core runs on stdlib+sqlite3
> alone).
>
> **This doc does NOT duplicate the existing conceptual reference.** HiveOS already
> ships an exhaustive 2,182-line `docs/memory/MNEMOSYNE.md` (BEAM tiers, MEMORIA,
> shared surface, the full tool/CLI reference) and a 1,434-line
> `docs/memory/MNEMOSYNE_INTEGRATION_PHASES.md`. **Read those for concepts, tools,
> and install steps.** This file adds what they lack: a **file-by-file code map,
> dependency matrix, REUSE-READY ranking, and the shortest wiring path** — the
> Phase-6 inputs. Sources: first-hand reads of README + the existing HiveOS doc +
> two code-level deep-read passes (every core file read).

## Coverage tiers

| Tier | What | How |
|------|------|-----|
| A — exhaustive | all 34 `mnemosyne/core/*.py` (incl. beam.py 7.4k LOC), `mnemosyne/extraction/*`, `mnemosyne/dr/recovery.py`, `mnemosyne/migrations/*`, `mnemosyne/{cli,install,diagnose,mcp_server,mcp_tools}.py`, `mnemosyne/integrations/*`, `mnemosyne/core/importers/*` (8 importers + base), `hermes_memory_provider/*` (the 23-tool provider), `pyproject.toml` | direct reads + 2 subagents |
| B — sampled | `tools/` (15 bench/diag scripts), `integrations/hermes/src/mnemosyne_hermes/`, `skills/` | characterized |
| C — enumerated | `tests/` (94 files), `scripts/`, `docs/`, benchmark shell scripts | listed |

Nothing flagged unreadable. ~197 `.py` total; core ≈19.2K LOC. Cross-references to
the conceptual doc are marked **[see MNEMOSYNE.md §N]**.

---

## 1. What Mnemosyne is, at the code level (one paragraph)

A single-file-SQLite memory engine exposing a small `Mnemosyne` facade
(`core/memory.py`) over `BeamMemory` (`core/beam.py`) — the BEAM architecture
(working_memory hot tier + episodic_memory long-term + scratchpad + TripleStore/
AnnotationStore + MEMORIA tables) **[see MNEMOSYNE.md §2–4]**. Recall is a
deterministic **hybrid score** (0.5 vector + 0.3 FTS5 + 0.2 importance) ×
Weibull temporal boost − MMR diversity penalty, with query-intent weight
adjustment, all computed inside SQLite. Vectors are compressed 32× via **MIB**
(maximally-informative binarization) with Hamming distance — no external vector DB,
no ANN index. Embeddings (fastembed/bge-small, optional) and LLM extraction/
consolidation (host→remote→local-GGUF fallback chain) are **soft dependencies**
that degrade gracefully to FTS-only. It surfaces three integration paths: a
**Hermes `MemoryProvider`** (`hermes_memory_provider/`, 23 tools), an **MCP server**
(stdio/SSE), and **OpenClaw/OpenWebUI** providers; plus 8 cross-provider importers
(letta/honcho/zep/mem0/hindsight/supermemory/cognee/agentic) and disaster recovery.

---

## 2. Core file map (`mnemosyne/core/`, 34 files)

| File | LOC | Purpose | External deps |
|------|----:|---------|---------------|
| memory.py | 860 | `Mnemosyne` facade (session/bank/author/channel ctx; remember/recall/etc.) | sqlite3, embeddings |
| beam.py | 7,387 | `BeamMemory` — write/recall/sleep, hybrid scoring, schema, MEMORIA | sqlite3, numpy?, embeddings |
| banks.py | 205 | `BankManager` multi-tenant DB isolation | sqlite3, pathlib |
| orchestrator.py | 19 | empty placeholder | — |
| embeddings.py | 254 | `embed/embed_query/available` — fastembed local OR OpenRouter API | fastembed OR urllib, numpy |
| binary_vectors.py | 372 | `BinaryVectorStore`, MIB binarization, hamming_distance (32× compress) | numpy |
| shmr.py | 656 | Self-harmonizing memory reasoning (echo contradiction, resonance) | numpy, embeddings |
| polyphonic_recall.py | 878 | 4-voice retrieval (vector+graph+fact+temporal) + deterministic rerank | numpy |
| mmr.py | 95 | MMR diversity rerank (Jaccard) | — |
| query_cache.py | 343 | 5-tier semantic query cache | sqlite3, numpy |
| query_intent.py | 167 | regex intent classify → weight bias (temporal/factual/entity/pref/proc) | re |
| weibull.py | 183 | per-memory-type Weibull temporal decay (13 types) | math, datetime |
| synonyms.py | 152 | query synonym expansion/normalize | — |
| recall_diagnostics.py | 270 | per-voice recall introspection | json, sqlite3 |
| streaming.py | 617 | memory event stream + delta sync (allowlisted tables) | sqlite3, json, hashlib |
| episodic_graph.py | 620 | REMem gist+fact graph, **rule-based zero-LLM extraction** | sqlite3, re |
| triples.py | 497 | `TripleStore` (single-current-truth; routes to AnnotationStore post-E6) | sqlite3 |
| annotations.py | 552 | `AnnotationStore` (E6 append-only multi-valued) | sqlite3 |
| typed_memory.py | 349 | `classify_memory`, `MemoryType` (13 types) — regex | re, enum |
| entities.py | 237 | entity extraction + pure-Python Levenshtein fuzzy match | re |
| patterns.py | 412 | memory compression + pattern detection | re, math |
| extraction.py | 364 | LLM structured fact extraction (host→remote→local, temp=0.0) | local_llm |
| veracity_consolidation.py | 947 | Bayesian confidence + conflict; collision-safe fact_id (SHA-256 + len-prefix) | sqlite3, hashlib |
| llm_conflict_detector.py | — | LLM-based contradiction detection | — |
| local_llm.py | 648 | local GGUF (llama-cpp/ctransformers) + remote/host fallback | optional |
| llm_backends.py | 123 | `LLMBackend` protocol, `set_host_llm_backend` (host pluggability) | dataclasses |
| token_counter.py | 72 | token/cost estimate (tiktoken or chars/4) | tiktoken? |
| aaak.py | 152 | lossless compression dialect for context | re |
| cost_log.py | 78 | memory-injection cost log | sqlite3 |
| content_sanitizer.py | 169 | binary/large/high-entropy payload → content-addressed blob | base64, hashlib |
| chat_normalize.py | 149 | chat normalization (contraction/filler/emoji) | re |
| temporal_parser.py | 404 | NL date extraction (24 patterns) | re, datetime |
| plugins.py | 676 | plugin architecture (`MnemosynePlugin`, manager, logging/metrics/filter) | importlib |
| polyphonic/synonyms dup | — | (synonyms listed once) | — |

Subpackages: `extraction/` (client.py OpenRouter, diagnostics.py, prompts.py),
`dr/recovery.py` (gzip backup/restore/verify via sqlite3.backup), `migrations/`
(e6_triplestore_split), `core/importers/` (base + 8 providers).

### Data model (SQLite) — code-confirmed
Tables: `working_memory`, `episodic_memory` (both carry author_id/author_type/
channel_id/trust_tier/validator for multi-agent), `scratchpad`, `vec_episodes`
(sqlite-vec float32|int8|bit), `fts_episodes`/`fts_working`/`fts_facts` (FTS5 +
triggers), `facts`, MEMORIA (`memoria_facts|timelines|instructions|preferences|kg`)
**[see MNEMOSYNE.md §4]**, `consolidation_log`, `memory_validations`,
`memory_embeddings`. Partial/covering indexes (e.g. `WHERE consolidated_at IS NULL`
for sleep eligibility, `WHERE superseded_by IS NULL`).

### Pipelines (code-confirmed; conceptual version in MNEMOSYNE.md §§3,6)
- **Write** `remember()`: sanitize blobs → temporal-tag → working_memory INSERT →
  (opt) embeddings → (opt) entity annotations → (opt) LLM fact extraction → episodic
  graph gist/fact (zero-LLM) → veracity aggregation.
- **Recall** `recall()`: query cache (5-tier) → intent classify → synonym expand →
  4-voice polyphonic (vector/FTS/fact/temporal) → deterministic rerank + MMR + budget
  truncate → cache put.
- **Sleep** `sleep()`: scan unconsolidated WM → batch → LLM summary (host→remote→
  local→skip) → extract facts → insert episodic summary → mark `consolidated_at`
  (no delete post-E3) → log.

---

## 3. Integration surfaces (the wiring layer)

- **`hermes_memory_provider/__init__.py`** (2,236 LOC) — `MnemosyneMemoryProvider`
  **implements the Hermes `MemoryProvider` ABC documented in HERMES_REFERENCE §6**:
  `initialize / system_prompt_block / prefetch / queue_prefetch / sync_turn /
  get_tool_schemas / handle_tool_call / on_session_end`, plus refcount multi-instance
  safety (C13), init-failure visibility (C27), skip-contexts (cron/subagent),
  `PrefetchProfile` tuning. Exposes the **23 tools** [enumerated in MNEMOSYNE.md §6]:
  remember/recall/update/get/forget/invalidate/stats/validate/sleep/import/export/
  graph_query/graph_link/triple_add/triple_query/scratchpad_{write,read,clear}/
  shared_{remember,recall,forget,stats}/diagnose. `hermes_llm_adapter.py` routes
  consolidation through the host's authenticated LLM; `audit.py` logs ops; `cli.py`
  installs/verifies the plugin; `plugin.yaml` is the manifest.
- **`mnemosyne/mcp_server.py` (303) + `mcp_tools.py` (799)** — MCP server over
  stdio (default) or SSE (bearer-token auth on non-loopback; constant-time compare);
  same 23 tools sourced from the provider's `ALL_TOOL_SCHEMAS`.
- **`mnemosyne/integrations/openclaw.py`** — implements OpenClaw's `MemoryProvider`
  (`store/search/delete`); `openwebui_tool.py` + `auto_save_openwebui.py` +
  `memory_browser.py` for OpenWebUI.
- **`mnemosyne/core/importers/`** — `BaseImporter` (extract→validate→transform→run)
  + 8 concrete: letta, honcho, zep, mem0, hindsight, supermemory, cognee, agentic.
- **`mnemosyne/{cli,install,diagnose}.py`** — `mnemosyne` CLI (remember/recall/sleep/
  import/export/triples), cross-platform symlink/junction install into
  `~/.hermes/plugins/`, PII-safe diagnostics with `--fix` auto-install.
- **`pyproject.toml`** extras: `embeddings`(fastembed+sqlite-vec), `llm`(ctransformers
  +llama-cpp+hf-hub), `mcp`(mcp+anyio), `openclaw`, `all`, `dev`. Entry points:
  `mnemosyne`, `mnemosyne-install`, `mnemosyne-uninstall`. **Core install needs only
  stdlib+sqlite3.**

---

## 4. Dependency posture (why it's so portable)
- **Zero-dependency core (~22% LOC, stdlib+sqlite3 only):** memory.py, beam.py
  (FTS path), banks.py, mmr.py, query_intent.py, weibull.py, synonyms.py,
  typed_memory.py, entities.py, temporal_parser.py, aaak.py, cost_log.py,
  content_sanitizer.py, chat_normalize.py, patterns.py, plugins.py, llm_backends.py,
  triples.py, annotations.py, episodic_graph.py, veracity_consolidation.py, dr/.
- **Soft deps with graceful fallback:** fastembed/sentence-transformers (→FTS only),
  numpy (→pure-Python), sqlite-vec (→float32/memory_embeddings), tiktoken (→chars/4),
  llama-cpp/ctransformers (→remote/host LLM or skip).
- **No hard crash paths** — every external dependency has a fallback.

---

## 5. REUSE-READY (Python — the largest direct-reuse pool)

Repo-relative paths (`/home/user/mnemosyne/...`). **Tier 1 = drop-in**, **Tier 2 =
light refactor**, **Tier 3 = pattern**.

### Tier 1 — drop-in utilities (zero/near-zero adaptation)
`core/embeddings.py` (embedder + API fallback), `core/binary_vectors.py` (MIB 32×),
`core/typed_memory.py` (13-type classify), `core/weibull.py` (temporal decay),
`core/query_intent.py` (intent→weights), `core/mmr.py` (diversity), `core/token_counter.py`,
`core/temporal_parser.py` (NL dates), `core/entities.py` (Levenshtein), `core/chat_normalize.py`,
`core/aaak.py`, `core/content_sanitizer.py` (blob), `core/banks.py` (multi-tenant),
`core/plugins.py` (hooks), `core/cost_log.py` (**feeds HiveOS budgeter**),
`core/llm_backends.py` (host-LLM protocol — lets HiveOS supply MiniMax as the
consolidation backend).

### Tier 2 — extract & adapt
`core/memory.py` + `core/beam.py` (the engine — keep recall/remember/sleep, drop
Mnemosyne-app wrappers), `core/triples.py` + `core/annotations.py` (KG),
`core/episodic_graph.py` (zero-LLM gist/fact), `core/veracity_consolidation.py`
(map trust tiers to Hive), `core/query_cache.py`, `core/polyphonic_recall.py`,
`dr/recovery.py` (backup/restore).

### Tier 3 — study/pattern
`core/local_llm.py` (host→remote→local fallback chain), `core/extraction.py`
(deterministic temp=0 extraction), `core/streaming.py` (delta sync), `core/shmr.py`
(self-harmonizing reasoning).

### Integration paths (use as-is)
`hermes_memory_provider/` (the provider), `mnemosyne/mcp_server.py`+`mcp_tools.py`
(MCP), `core/importers/` (migrate from other memory systems).

## 6. SHORTEST PATH to wire Mnemosyne as HiveOS's memory layer
**Do not re-implement.** Two viable options; the existing
`docs/memory/MNEMOSYNE_INTEGRATION_PHASES.md` is the install playbook. In short:

- **Recommended (in-process, in-repo):** `pip install mnemosyne-memory`, then in
  `memory/brain.py` instantiate `MnemosyneMemoryProvider` (from
  `hermes_memory_provider`) and call `initialize()/prefetch()/sync_turn()/
  handle_tool_call()/on_session_end()` in the HiveOS turn loop. HiveOS's
  `memory/memory_keeper.py` ↔ Mnemosyne `sleep()`. Register MiniMax as the host LLM
  via `core/llm_backends.set_host_llm_backend()` so consolidation/extraction reuse
  HiveOS's model router (no extra API keys). One SQLite file; no external services.
- **Alternative (decoupled):** run `mnemosyne mcp` and have `gateway/app.py`/tools
  consume it as an MCP server (bearer-token if non-loopback).

This means HiveOS's `memory/brain.py` (Mnemosyne active layer) is **mostly
configuration + a thin provider wrapper**, not new memory code — the single biggest
"already solved" component in the whole unification.

## 7. ADAPT-AS-PATTERN (designs to copy even beyond memory)
1. **Deterministic, debuggable hybrid ranking** (no neural reranker) — copy for any
   HiveOS retrieval/scoring.
2. **host→remote→local fallback chain** (LLM + embeddings) — mirrors Hermes
   auxiliary_client; reinforces the HiveOS model-router fallback design.
3. **Veracity tiers + Bayesian confidence + collision-safe fact IDs** (SHA-256 with
   length-prefix framing) — provenance/trust for multi-agent writes; aligns with
   HiveOS approval/trust needs.
4. **Weibull per-type temporal decay** — memory psychology baked into ranking.
5. **MIB binary vectors + Hamming in SQLite** — vector search with no vector DB.
6. **Append-only AnnotationStore vs single-truth TripleStore** — avoids silent
   data-loss on multi-valued facts; a migration lesson (E6) for HiveOS schema design.
7. **Partial indexes for eligibility predicates** (sleep scan) — DB perf pattern.
8. **Content-addressed blob storage** for large/binary payloads.
9. **Graceful-degradation everywhere** — soft deps, never crash.
10. **`llm_backends` host-pluggability** — let the host own LLM auth/routing; the
    library never holds its own keys. **Adopt this contract direction across HiveOS.**

## 8. What NOT to take
- The OpenWebUI/OpenClaw integrations (HiveOS doesn't need them yet) — but keep the
  `MemoryProvider` shape.
- The standalone CLI/MCP packaging if going fully in-process (still useful for debug).
- Heavy GGUF local-LLM path (HiveOS already has MiniMax) — wire MiniMax via
  `llm_backends` instead.

## 9. Relevance to current HiveOS + relation to the other repos
HiveOS's architecture map names `memory: brain (Mnemosyne active + Obsidian
long-term) · memory_keeper (consolidation)`. This audit confirms the **active layer
is effectively done**: `memory/brain.py` ← `MnemosyneMemoryProvider`;
`memory/memory_keeper.py` ← Mnemosyne `sleep()` consolidation; the Obsidian
long-term layer is a separate concern (Mnemosyne has no Obsidian sync — that stays
HiveOS-owned, or use Hermes/OpenClaw's Obsidian-vault patterns).
**In the Phase-6 synthesis:** Mnemosyne is the **memory primitive**; the OpenClaw
**single-active-memory-slot contract** + the Hermes **`MemoryProvider` ABC** are how
it plugs into the agent loop; OpenJarvis contributes the surrounding registry/engine
skeleton. Net: of the four repos, Mnemosyne is the one whose **code ships almost
verbatim** into HiveOS — confirm versions with `mnemosyne.__version__` per
MNEMOSYNE.md §1 before relying on any version-specific behavior.

> Cross-reference: concepts/tools/CLI/tiers → `docs/memory/MNEMOSYNE.md`;
> install/wiring steps → `docs/memory/MNEMOSYNE_INTEGRATION_PHASES.md`;
> this file → code map + reuse decisions.
