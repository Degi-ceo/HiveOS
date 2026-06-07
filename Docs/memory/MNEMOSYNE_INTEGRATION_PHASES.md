# Mnemosyne × Hermes — Integration Phases (v3.1.2)

> **Ten copy-paste-ready Claude Code prompts.** Send them in order. Each is self-contained — tells Claude what to do, what context matters, and what "done" looks like.
>
> **Audience.** You, running on a Hetzner VPS with Hermes Agent installed or being installed. The receiving Claude is Claude Code (or any shell-capable Claude).
>
> **Reference.** All capability questions answered in `MNEMOSYNE.md` (the master reference packaged with this file). When a phase says "see master §X", that's where to look.
>
> **Outcome after Phase 10:** Mnemosyne v3.1.2 fully wired into Hermes — persistent memory across restarts, identity-tagged for orchestrator + subagents, cron-driven sleep, daily backups, all gotchas avoided.

---

## How to use

1. **Open one phase.** Don't paste them all at once.
2. **Copy the prompt** between the ```text fences.
3. **Wait** for Claude to complete. Check Success criteria.
4. **If something fails:** point Claude at `MNEMOSYNE.md §38` (Common failures catalog).
5. **Phases 0–7 mandatory.** 8–10 optional.

---

## Phase overview

| # | Phase | Mandatory |
|---|---|:---:|
| 0 | Pre-flight check | ✓ |
| 1 | Install Mnemosyne + dependencies | ✓ |
| 2 | Initialize DB + smoke test | ✓ |
| 3 | Environment configuration (env + YAML) | ✓ |
| 4 | Multi-agent identity setup | ✓ |
| 5 | Hermes plugin wiring | ✓ |
| 6 | Memory pre-loading | ✓ |
| 7 | Cron jobs (sleep + backup + DR verify) | ✓ |
| 7.5 | Schema awareness + upgrade procedure | recommended |
| 8 | Capabilities reference injection | optional |
| 9 | Full-flow smoke test | optional |
| 10 | MCP SSE server for laptop access | optional |

---

## Phase 0 — Pre-flight check

**Why.** Verify substrate before touching anything.

### Message to Claude

```text
I'm integrating Mnemosyne v3.1.2 (https://github.com/AxDSan/mnemosyne) as the memory layer for my Hermes Agent on this Hetzner VPS. Pre-flight: read-only checks only, no installs.

Verify and report as a Markdown table (Check / Result / Status ✓✗⚠ / Notes):

1. OS: `lsb_release -a` (or `cat /etc/os-release`)
2. Python: `python3 --version` — must be ≥ 3.9
3. SQLite: `sqlite3 --version` — must be ≥ 3.45 (FTS5 requirement)
4. FTS5 support: `python3 -c "import sqlite3; c=sqlite3.connect(':memory:'); c.execute('CREATE VIRTUAL TABLE t USING fts5(content)')"` — must exit cleanly
5. pip / uv: which available? Versions?
6. Disk free at `~`: `df -h ~`
7. Disk free at `/opt`: `df -h /opt` (if exists)
8. Hermes installed?: `which hermes` and `hermes --version`
9. Hermes venv path — check BOTH:
   - `/usr/local/lib/hermes-agent/venv/` (system install)
   - `~/.hermes/hermes-agent/venv/` (user install)
   Report which exists.
10. Existing Mnemosyne?: `pip show mnemosyne-memory` (don't fail if missing)
11. Existing Mnemosyne data?: `ls -la ~/.hermes/mnemosyne/data/ 2>/dev/null`
12. Network: `curl -sI https://pypi.org` — should return 200
13. Existing crontab: `crontab -l` (read-only)
14. Disk type: `lsblk -d -o name,rota` — `0` means SSD
15. Free RAM: `free -h`

After the table, list any ✗/⚠ as blockers/warnings. DO NOT install or modify anything.
```

### Success criteria

- [ ] All 15 checks reported
- [ ] Python 3.9+, SQLite 3.45+, FTS5 working
- [ ] At least 2 GB free at install location

### Fallback

- SQLite < 3.45: `sudo apt-get update && sudo apt-get install -y sqlite3`
- Python < 3.9: `pyenv` or `uv` install

---

## Phase 1 — Install Mnemosyne + dependencies

**Why.** Get v3.1.2 with all extras and `sqlite-vec`.

**Reference.** Master §21 (installation extras), §23 (upgrade procedure).

### Message to Claude

```text
Install Mnemosyne v3.1.2 with all features. Strategy:

1. CHOOSE VENV:
   - If Phase 0 found `/usr/local/lib/hermes-agent/venv/` → use it (system Hermes)
   - Else if `~/.hermes/hermes-agent/venv/` → use it (user Hermes)
   - Else create `/opt/mnemosyne/venv` and use it (standalone)
   Report which path and why.

2. ACTIVATE & DETECT pip vs uv:
   - `source <venv>/bin/activate`
   - `pip --version` — if missing, the venv was made with `uv venv`. Use `uv pip install` for everything below.

3. INSTALL:
   ```bash
   pip install "mnemosyne-memory[all]"       # or: uv pip install "mnemosyne-memory[all]"
   pip install sqlite-vec
   pip install mnemosyne-hermes
   ```
   If PEP 668 blocks (Debian 13+/Ubuntu 24.04+):
   - Best:  use the venv (already activated above — should work)
   - Else: append `--break-system-packages` to commands

4. VERIFY (must succeed):
   ```bash
   python -c "from mnemosyne import __version__; print(__version__)"   # expect 3.1.2
   python -c "import sqlite_vec; print(sqlite_vec.__version__)"
   python -c "import fastembed; print(fastembed.__version__)"
   python -c "from mnemosyne import Mnemosyne, TripleStore; print('ok')"
   ```

5. PRE-DOWNLOAD embedding model (avoid first-recall latency):
   ```bash
   python -c "from fastembed import TextEmbedding; m = TextEmbedding('BAAI/bge-small-en-v1.5'); list(m.embed(['warmup']))"
   ```
   ~120 MB to fastembed cache.

6. REPORT:
   - Chosen venv path + reason
   - Installed versions (mnemosyne-memory, sqlite-vec, fastembed, mnemosyne-hermes)
   - Cache location of embedding model
   - Total footprint

DO NOT initialise DB or configure Hermes yet.
```

### Success criteria

- [ ] `mnemosyne.__version__` returns `3.1.2`
- [ ] `sqlite_vec` and `fastembed` importable
- [ ] Embedding model downloaded (warm cache)

### Fallback

- Quote `pip install` fails / wrong venv → see master §38.5
- PEP 668 blocks → see master §38.3

---

## Phase 2 — Initialize DB + smoke test

**Why.** Verify the local install works end-to-end before integrating with Hermes.

**Reference.** Master §6 (tool reference), §8 (hybrid scoring).

### Message to Claude

```text
Run a smoke test against a temporary DB. Production location set in Phase 3.

Save as `/tmp/mnemo_smoke.py` and run:

```python
import time
from pathlib import Path
from mnemosyne import Mnemosyne, TripleStore

db_path = Path("/tmp/mnemo_smoke.db")
if db_path.exists():
    db_path.unlink()

print("== Init Mnemosyne ==")
mem = Mnemosyne(session_id="smoke-test", db_path=str(db_path))

print("\n== Write 3 memories ==")
id1 = mem.remember("Kamil prefers Python over JavaScript for backend work.",
                    importance=0.9, source="preference", veracity="stated")
id2 = mem.remember("Hermes uses MiniMax M2.7 as primary reasoning model.",
                    importance=0.9, source="decision", extract_entities=True,
                    veracity="stated")
id3 = mem.remember("Smoke test memory.", importance=0.3, source="test",
                    veracity="tool")
print(f"  {id1[:8]}, {id2[:8]}, {id3[:8]}")

print("\n== Add triple ==")
ts = TripleStore(db_path=str(db_path))
tid = ts.add(subject="Hermes", predicate="primary_model", object="MiniMax M2.7",
             confidence=1.0, source="manual")
print(f"  Triple {tid}")

print("\n== Semantic recall ==")
for r in mem.recall("What language does Kamil prefer?", top_k=3):
    print(f"  [{r['score']:.3f}] {r['content'][:60]}")

print("\n== FTS-boosted recall (exact match) ==")
for r in mem.recall("MiniMax M2.7", vec_weight=20, fts_weight=60, importance_weight=20):
    print(f"  [{r['score']:.3f}] {r['content'][:60]}")

print("\n== Triple query ==")
for t in ts.query(subject="Hermes"):
    print(f"  ({t['subject']}, {t['predicate']}, {t['object']})")

print("\n== Stats ==")
stats = mem.get_stats()
print(f"  WM: {stats['beam']['working_memory']}")
print(f"  EM: {stats['beam']['episodic_memory']}")
print(f"  Triples: {stats['beam']['triples']['total']}")

print("\n== Sleep dry-run ==")
print(f"  {mem.sleep(dry_run=True)}")

print("\n== Cleanup ==")
db_path.unlink()
print("\nSMOKE TEST PASSED.")
```

Run: `python /tmp/mnemo_smoke.py`

Report full output. STOP on any error and report the traceback.
```

### Success criteria

- [ ] Script exits with `SMOKE TEST PASSED.`
- [ ] Semantic recall ranks Python-preference first
- [ ] FTS-boosted recall ranks MiniMax memory first
- [ ] Triple query returns the (Hermes, primary_model, MiniMax M2.7) triple

### Fallback

- See master §38.10 (embedding model download) and §38.11 (sqlite-vec loading)

---

## Phase 3 — Environment configuration

**Why.** Lock production paths, weights, identity defaults, sleep behaviour.

**Reference.** Master §17 (env vars), §18 (YAML), §20 (Hermes integration).

### Message to Claude

```text
Configure Mnemosyne for production. Two parts: env vars + YAML.

PART A — `/etc/profile.d/mnemosyne.sh` (sudo)

```bash
# Mnemosyne v3.1.2 — system-wide environment

# Storage
export MNEMOSYNE_DATA_DIR="/opt/mnemosyne/data"

# Working Memory
export MNEMOSYNE_WM_MAX_ITEMS=10000
export MNEMOSYNE_WM_TTL_HOURS=24
export MNEMOSYNE_SP_MAX=1000

# Retrieval weights
export MNEMOSYNE_VEC_TYPE=int8
export MNEMOSYNE_VEC_WEIGHT=0.5
export MNEMOSYNE_FTS_WEIGHT=0.3
export MNEMOSYNE_IMPORTANCE_WEIGHT=0.2
export MNEMOSYNE_TEMPORAL_HALFLIFE_HOURS=24

# Tiered degradation
export MNEMOSYNE_TIER2_DAYS=30
export MNEMOSYNE_TIER3_DAYS=180
export MNEMOSYNE_TIER1_WEIGHT=1.0
export MNEMOSYNE_TIER2_WEIGHT=0.5
export MNEMOSYNE_TIER3_WEIGHT=0.25
export MNEMOSYNE_SMART_COMPRESS=true
export MNEMOSYNE_TIER3_MAX_CHARS=300

# Veracity
export MNEMOSYNE_STATED_WEIGHT=1.0
export MNEMOSYNE_INFERRED_WEIGHT=0.7
export MNEMOSYNE_TOOL_WEIGHT=0.5
export MNEMOSYNE_IMPORTED_WEIGHT=0.6
export MNEMOSYNE_UNKNOWN_WEIGHT=0.8

# Embedding (local)
export MNEMOSYNE_EMBEDDING_MODEL="BAAI/bge-small-en-v1.5"

# Sleep summarisation: route through Hermes' MiniMax M2.7
export MNEMOSYNE_HOST_LLM_ENABLED=true
export MNEMOSYNE_HOST_LLM_PROVIDER=minimax
export MNEMOSYNE_HOST_LLM_MODEL="MiniMax-M2.7"
export MNEMOSYNE_HOST_LLM_N_CTX=32000

# Don't auto-sleep — driven by cron in Phase 7
export MNEMOSYNE_AUTO_SLEEP_ENABLED=false

# v3.1.2: strict matching is default. If recall is too narrow later, opt back:
# export MNEMOSYNE_LENIENT_FACT_MATCH=1

# Auto-migration on by default (keep this)
# export MNEMOSYNE_AUTO_MIGRATE=0   # only set to opt-out

# MCP default bank
export MNEMOSYNE_MCP_BANK="hermes-main"

# Logging
export MNEMOSYNE_LOG_TOOLS=false
```

Source and verify:
```bash
sudo install -m 644 /tmp/mnemosyne.sh /etc/profile.d/mnemosyne.sh
source /etc/profile.d/mnemosyne.sh
env | grep MNEMOSYNE | sort
```

Create data dir with correct ownership:
```bash
sudo mkdir -p /opt/mnemosyne/data
sudo chown -R $(whoami):$(whoami) /opt/mnemosyne
chmod 700 /opt/mnemosyne/data
```

PART B — Hermes `config.yaml`

Locate via `hermes config path` (or check `~/.hermes/config.yaml`). Add/merge:

```yaml
memory:
  # Built-in MemoryStore — independent of Mnemosyne. Keep true (or false; doesn't affect Mnemosyne).
  memory_enabled: true
  user_profile_enabled: true

  # Tell Hermes to use Mnemosyne.
  provider: mnemosyne

  mnemosyne:
    profile_isolation: false      # all profiles share one DB (default)
    data_dir: /opt/mnemosyne/data
    bank: hermes-main
    auto_sleep: false             # cron-driven in Phase 7
    sleep_threshold: 50
    vector_type: int8
    auto_context: true
    context_injection:
      enabled: true
      max_memories: 5
      min_relevance: 0.7
    ignore_patterns:
      - "be ACTIVE"
      - "nothing to change"
      - "skill.*refined"
```

CRITICAL — the two controls are independent:
- `memory_enabled` (YAML) controls the built-in MemoryStore. Setting false does NOT disable Mnemosyne.
- `hermes tools enable/disable memory` controls the toolset (BOTH built-in AND Mnemosyne). 
  **NEVER run `hermes tools disable memory`** — see Master §38.1.

PART C — Verify

```python
import os
from mnemosyne import Mnemosyne
print("DATA_DIR:", os.environ["MNEMOSYNE_DATA_DIR"])
print("BANK:", os.environ["MNEMOSYNE_MCP_BANK"])
mem = Mnemosyne(bank="hermes-main")
mem.remember("Config verification stamp.", importance=0.1, source="config-test",
             veracity="stated")
print("Stats:", mem.get_stats())
```

Report:
- Path of `config.yaml` + diff
- `env | grep MNEMOSYNE | sort` output (full)
- Verification script output

DO NOT touch identity wrappers yet.
```

### Success criteria

- [ ] `/etc/profile.d/mnemosyne.sh` sourced
- [ ] `/opt/mnemosyne/data` exists, owned by agent user, 700 perms
- [ ] Hermes `config.yaml` has the `memory.mnemosyne` block with `memory_enabled` + `provider`
- [ ] Verification script writes + reads against `hermes-main` bank

---

## Phase 4 — Multi-agent identity setup

**Why.** Orchestrator + subagents need distinct `author_id` so channel-recall works.

**Reference.** Master §14, §29 (multi-agent topology).

### Message to Claude

```text
Set up multi-agent identity: 1 orchestrator + 3 subagents + system, sharing channel `hermes-main`.

PART A — Identity table

Document at `/opt/mnemosyne/IDENTITY.md`:

```
# Hermes Memory Identity Table

Bank: hermes-main
Channel: hermes-main

| author_id              | author_type | role                                    |
|------------------------|-------------|-----------------------------------------|
| kamil                  | human       | The user                                |
| hermes-orchestrator    | agent       | Front agent / heartbeat                 |
| hermes-research        | agent       | Research subagent                       |
| hermes-coder           | agent       | Coding / implementation subagent        |
| hermes-ops             | agent       | Ops / monitoring subagent               |
| hermes-system          | system      | Cron jobs, sleep, automation            |
```

PART B — Wrapper

Create `/opt/mnemosyne/agent_factory.py`:

```python
"""
Mnemosyne identity wrappers for Hermes agents.
Usage:
    from agent_factory import mem_for
    mem = mem_for("hermes-research")
"""
import os
from mnemosyne import Mnemosyne

BANK = os.environ.get("MNEMOSYNE_MCP_BANK", "hermes-main")
CHANNEL = "hermes-main"

IDENTITIES = {
    "kamil":               ("human",  CHANNEL),
    "hermes-orchestrator": ("agent",  CHANNEL),
    "hermes-research":     ("agent",  CHANNEL),
    "hermes-coder":        ("agent",  CHANNEL),
    "hermes-ops":          ("agent",  CHANNEL),
    "hermes-system":       ("system", CHANNEL),
}

def mem_for(author_id: str, session_id: str | None = None) -> Mnemosyne:
    if author_id not in IDENTITIES:
        raise ValueError(f"Unknown author_id: {author_id}")
    author_type, channel_id = IDENTITIES[author_id]
    return Mnemosyne(
        session_id=session_id or f"{author_id}-default",
        bank=BANK,
        author_id=author_id,
        author_type=author_type,
        channel_id=channel_id,
    )

def recall_channel(query: str, top_k: int = 10, **kw):
    """Read across all authors in the channel."""
    return mem_for("hermes-orchestrator").recall(
        query, top_k=top_k, channel_id=CHANNEL, **kw)

def recall_only(query: str, author_id: str, top_k: int = 5, **kw):
    """Read only one author."""
    return mem_for("hermes-orchestrator").recall(
        query, top_k=top_k, author_id=author_id, **kw)
```

PART C — Verify

```python
import sys
sys.path.insert(0, "/opt/mnemosyne")
from agent_factory import mem_for, recall_channel

mem_for("kamil").remember("Channel test from human.", importance=0.5, veracity="stated")
mem_for("hermes-orchestrator").remember("Channel test from orchestrator.",
                                        importance=0.5, veracity="inferred")
mem_for("hermes-research").remember("Channel test from research subagent.",
                                    importance=0.5, veracity="tool")

results = recall_channel("channel test", top_k=10)
print(f"Channel recall: {len(results)} results")
for r in results:
    print(f"  [{r.get('author_id','?'):25}] {r['content'][:60]}")
```

Expected: 3 results, one per author.

PART D — Hermes wiring

Find where Hermes constructs its memory provider. Change to `mem_for("hermes-orchestrator")`. For each subagent entry point, use matching `author_id`.

If you can't find the location: `grep -rn "memory" ~/.hermes/ | head -20` and we'll decide together.

Report:
- Paths of IDENTITY.md and agent_factory.py
- Verification output
- Hermes file(s) modified (paths + diffs only)

DO NOT seed real memories yet.
```

### Success criteria

- [ ] `IDENTITY.md` and `agent_factory.py` exist
- [ ] Verification writes 3 memories with distinct `author_id`s
- [ ] Channel recall returns all 3
- [ ] Hermes orchestrator uses `mem_for("hermes-orchestrator")`

---

## Phase 5 — Hermes plugin wiring

**Why.** Activate auto-context injection so memory is in every LLM call.

**Reference.** Master §20 (Hermes integration — read this in full).

### Message to Claude

```text
Register Mnemosyne as Hermes' memory provider and verify the right way.

⚠️ CRITICAL — DO NOT USE `hermes memory status` AS A VERIFICATION COMMAND.
   It's a known docs-confirmed display bug. Always prints "Built-in: always active"
   regardless of actual state. Use `hermes doctor` instead.

⚠️ CRITICAL — DO NOT RUN `hermes tools disable memory`.
   This removes BOTH built-in AND Mnemosyne tools (they share the same toolset gate).
   This is the single most common Hermes-Mnemosyne integration failure.
   See Master §38.1.

STEPS

1. Register:
   ```bash
   hermes config set memory.provider mnemosyne
   hermes gateway restart
   ```
   If `hermes config set` doesn't exist or fails, the YAML edit from Phase 3 is the path.

2. Plugin loaded?
   ```bash
   hermes plugins list
   ```
   Expected: `mnemosyne` (or `mnemosyne-memory`). If absent:
   ```bash
   python -m mnemosyne.install
   hermes gateway restart
   hermes plugins list
   ```

3. Tools registered? (THE REAL CHECK)
   ```bash
   hermes tools list | grep mnemosyne
   ```
   Expected ≥ 10 tools including: mnemosyne_remember, mnemosyne_recall, mnemosyne_sleep,
   mnemosyne_get_stats, mnemosyne_triple_add, mnemosyne_triple_query,
   mnemosyne_scratchpad_write/read/clear, mnemosyne_invalidate. May include
   mnemosyne_forget, mnemosyne_update, mnemosyne_export, mnemosyne_import,
   mnemosyne_diagnose depending on version. Possibly `mnemosyne_shared_*` if v3.1+
   shared surface is activated (`hermes memory surface`).

4. Real health check:
   ```bash
   hermes doctor | grep -i memory
   ```

5. Direct functional test:
   ```bash
   hermes --tool mnemosyne_remember content="Phase 5 test" veracity="stated"
   hermes --tool mnemosyne_recall query="Phase 5 test"
   ```
   Second call must return the first call's content.

6. Auto-context injection check:
   - Confirm `auto_context: true` and `context_injection.enabled: true` in config.yaml
   - Trigger one agent turn the way you normally would
   - Watch prompt construction (set `MNEMOSYNE_LOG_TOOLS=true` temporarily if helpful)
   - Look for marker in the prompt:
     ```
     ═══════════════════════════════════════════════════════════════
     MNEMOSYNE MEMORY (persistent local context)
     ```

7. If tools missing → checklist (see Master §38.5):
   - `pip list | grep mnemosyne` — installed?
   - Correct venv (Hermes's venv, not somewhere else)?
   - `provider: mnemosyne` in config?
   - Was `hermes tools disable memory` ever run? Re-enable: `hermes tools enable memory`
   - `hermes gateway restart` after each config change

Report:
- `hermes plugins list` output
- `hermes tools list | grep mnemosyne` output
- `hermes doctor | grep -i memory` output
- Direct functional test (write + read) output
- Auto-context injection observed? (paste log snippet)

⚠️ I repeat: DO NOT use `hermes memory status`. DO NOT run `hermes tools disable memory`.
```

### Success criteria

- [ ] `mnemosyne` in `hermes plugins list`
- [ ] ≥ 10 `mnemosyne_*` tools in `hermes tools list`
- [ ] `hermes doctor` reports memory healthy
- [ ] Direct write + read works
- [ ] Auto-context block appears in agent turns

### Fallback

- Tools missing → Master §38.5
- Display bug confusion → Master §38.2

---

## Phase 6 — Memory pre-loading

**Why.** Auto-injection needs content. Seed SOUL.md identity, projects, preferences.

**Reference.** Master §28 (use case patterns), §31 (decision log).

### Message to Claude

```text
Seed Hermes' Mnemosyne bank with foundational context: identity + projects + preferences.

PART A — Inventory

Locate and read (don't write yet):
1. `~/.hermes/SOUL.md` — Hermes' identity
2. `~/.hermes/*.md` in any bootstrap bundle
3. Existing project notes in `~/projects/`, `~/code/`, `/opt/hermes/`

Report what you found (paths + brief content summary).

PART B — Seed plan

Create `/opt/mnemosyne/seed.py`:

```python
"""
Idempotent seed of foundational memories into the hermes-main bank.
Run: python /opt/mnemosyne/seed.py
Safe to re-run.
"""
import sys
sys.path.insert(0, "/opt/mnemosyne")
from agent_factory import mem_for

mem = mem_for("kamil")   # human-stated → veracity defaults to stated semantics

# === IDENTITY (importance ≥ 0.9, scope=global) ===
IDENTITY_MEMORIES = [
    # (content, importance, source)
    # Fill from SOUL.md and stated identity. Example:
    # ("Kamil works at Moat House building internal operational tools.", 0.95, "identity"),
    # ("Kamil's primary dev device is an iPad — vibe coder, prolific.", 0.9, "identity"),
    # ("Kamil converses in Polish but writes code/docs in English.", 0.95, "identity"),
]

# === ACTIVE PROJECTS (importance ~0.85) ===
PROJECT_MEMORIES = [
    # ("Hermes is the autonomous agent framework, primary MiniMax M2.7.", 0.9, "project:hermes"),
    # ("Mnemosyne v3.1.2 is Hermes' memory backend.", 0.9, "project:hermes"),
]

# === PREFERENCES (durable, scope=global) ===
PREFERENCE_MEMORIES = [
    # ("Kamil prefers Python for agent logic and backend.", 0.9, "preference"),
]

def seed_block(memories, scope="global"):
    for content, importance, source in memories:
        existing = mem.recall(content[:50], top_k=1, source=source)
        if existing and existing[0]["score"] > 0.95:
            print(f"  SKIP: {content[:60]}")
            continue
        mem.remember(content, importance=importance, source=source,
                     scope=scope, veracity="stated", extract_entities=True)
        print(f"  WROTE: {content[:60]}")

print("== IDENTITY ==");    seed_block(IDENTITY_MEMORIES)
print("\n== PROJECTS ==");  seed_block(PROJECT_MEMORIES)
print("\n== PREFERENCES ==");seed_block(PREFERENCE_MEMORIES)

print(f"\n== Stats ==\n{mem.get_stats()}")
```

CRITICAL: fill the lists from real source files only. If a fact isn't in SOUL.md or
a file I can show, leave the slot empty and ASK ME. Do not invent.

PART C — Run

```bash
python /opt/mnemosyne/seed.py
```

PART D — Foundational triples

```python
import sys, os
sys.path.insert(0, "/opt/mnemosyne")
from mnemosyne import TripleStore

ts = TripleStore(db_path=os.path.join(
    os.environ["MNEMOSYNE_DATA_DIR"], "banks", "hermes-main", "mnemosyne.db"))

triples = [
    ("Hermes", "primary_model", "MiniMax M2.7"),
    ("Hermes", "fallback_provider", "ChatGPT Plus/Pro OAuth"),
    ("Hermes", "memory_provider", "Mnemosyne v3.1.2"),
    ("Hermes", "deployment", "Hetzner VPS"),
    ("Kamil", "communication_language", "Polish"),
    ("Kamil", "code_language", "English"),
    ("Kamil", "primary_dev_language", "Python"),
]
for s, p, o in triples:
    print(f"  + ({s}, {p}, {o}) [id={ts.add(s, p, o, confidence=1.0, source='seed')}]")
```

PART E — Verify

```python
from agent_factory import recall_channel
for label, q in [("identity", "who am I"),
                 ("projects", "active projects"),
                 ("preferences", "my preferences")]:
    print(f"\n--- {label} ---")
    for r in recall_channel(q, top_k=5):
        print(f"  [{r['score']:.3f}] {r['content']}")
```

Report:
- Inventory from Part A
- Filled `seed.py`
- Run output Parts C-E
- Final `get_stats()`
```

### Success criteria

- [ ] `seed.py` populated from real sources only
- [ ] Re-running it skips existing entries
- [ ] Foundational triples queryable
- [ ] Recalls return meaningful results

---

## Phase 7 — Cron jobs (sleep + backup + DR verify)

**Why.** Sleep must run. Backups must run. Verification must run.

**Reference.** Master §11 (sleep), §25 (backups).

### Message to Claude

```text
Set up three cron jobs as `hermes-system` identity:
1. Daily sleep at 03:15
2. Daily backup at 03:45
3. Weekly DR verify on Sundays 04:00

PART A — Wrappers

`/opt/mnemosyne/jobs/sleep.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
export $(grep -v '^#' /etc/profile.d/mnemosyne.sh | sed 's/^export //')
LOG=/var/log/mnemosyne/sleep-$(date +%Y%m%d).log
mkdir -p /var/log/mnemosyne
{
  echo "[$(date -Iseconds)] Sleep starting"
  /opt/mnemosyne/venv/bin/python -c "
import sys
sys.path.insert(0, '/opt/mnemosyne')
from agent_factory import mem_for
mem = mem_for('hermes-system')
print('Sleep result:', mem.sleep())
print('Sleep-all-sessions:', mem.sleep_all_sessions())
print('Stats:', mem.get_stats())
"
  echo "[$(date -Iseconds)] Sleep done"
} >> "$LOG" 2>&1
```

`/opt/mnemosyne/jobs/backup.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
export $(grep -v '^#' /etc/profile.d/mnemosyne.sh | sed 's/^export //')
LOG=/var/log/mnemosyne/backup-$(date +%Y%m%d).log
DEST=/opt/mnemosyne/backups
mkdir -p "$DEST" /var/log/mnemosyne
{
  echo "[$(date -Iseconds)] Backup starting"
  /opt/mnemosyne/venv/bin/python -c "
from mnemosyne.dr.recovery import create_backup
import os
db = os.path.join(os.environ['MNEMOSYNE_DATA_DIR'], 'banks', 'hermes-main', 'mnemosyne.db')
print('Backup:', create_backup(db, '$DEST'))
"
  find "$DEST" -name "*.db.gz" -mtime +14 -delete
  echo "[$(date -Iseconds)] Inventory:"
  ls -lah "$DEST"
} >> "$LOG" 2>&1
```

`/opt/mnemosyne/jobs/dr_verify.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
export $(grep -v '^#' /etc/profile.d/mnemosyne.sh | sed 's/^export //')
LOG=/var/log/mnemosyne/dr_verify-$(date +%Y%m%d).log
DEST=/opt/mnemosyne/backups
mkdir -p /var/log/mnemosyne
{
  echo "[$(date -Iseconds)] DR verify starting"
  LATEST=$(ls -t "$DEST"/*.db.gz 2>/dev/null | head -1 || echo "")
  if [[ -z "$LATEST" ]]; then echo "ERROR: no backups in $DEST"; exit 1; fi
  echo "Verifying: $LATEST"
  TMP=$(mktemp -d)
  gunzip -c "$LATEST" > "$TMP/restore.db"
  RESULT=$(sqlite3 "$TMP/restore.db" "PRAGMA integrity_check;")
  if [[ "$RESULT" != "ok" ]]; then
    echo "FAIL: integrity_check: $RESULT"
    rm -rf "$TMP"; exit 1
  fi
  WORKING=$(sqlite3 "$TMP/restore.db" "SELECT COUNT(*) FROM working_memory;")
  EPISODIC=$(sqlite3 "$TMP/restore.db" "SELECT COUNT(*) FROM episodic_memory;")
  echo "OK: integrity good, working=$WORKING episodic=$EPISODIC"
  rm -rf "$TMP"
} >> "$LOG" 2>&1
```

PART B — Executable

```bash
chmod +x /opt/mnemosyne/jobs/*.sh
```

PART C — Crontab (agent user, NOT root)

`crontab -e` and add:
```cron
# Mnemosyne — hermes-main bank maintenance
15 3 * * * /opt/mnemosyne/jobs/sleep.sh
45 3 * * * /opt/mnemosyne/jobs/backup.sh
0  4 * * 0 /opt/mnemosyne/jobs/dr_verify.sh
MAILTO=
```

PART D — Smoke test each

```bash
/opt/mnemosyne/jobs/sleep.sh && echo "sleep OK"
/opt/mnemosyne/jobs/backup.sh && echo "backup OK"
/opt/mnemosyne/jobs/dr_verify.sh && echo "dr_verify OK"
```

PART E — Log rotation

`/etc/logrotate.d/mnemosyne` (sudo):
```
/var/log/mnemosyne/*.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
}
```

Report:
- All three wrapper paths
- `crontab -l` output
- Smoke test results
- `/var/log/mnemosyne/` listing

DO NOT enable `MNEMOSYNE_AUTO_SLEEP_ENABLED=true` — cron handles it.
```

### Success criteria

- [ ] Three wrappers executable
- [ ] Crontab shows three jobs
- [ ] Each runs cleanly manually
- [ ] `/opt/mnemosyne/backups/` has at least one `.db.gz`
- [ ] DR verify reports "OK: integrity good"

---

## Phase 7.5 — Schema awareness + upgrade procedure

**Why.** Mnemosyne uses automatic migrations. Document current state + safe upgrade path.

**Reference.** Master §22 (schema versioning), §23 (upgrades), §24 (rollback).

### Message to Claude

```text
Document current schema and prepare for future upgrades.

PART A — Schema version detector

`/opt/mnemosyne/jobs/schema_version.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
export $(grep -v '^#' /etc/profile.d/mnemosyne.sh | sed 's/^export //')
DB="${MNEMOSYNE_DATA_DIR}/banks/hermes-main/mnemosyne.db"
/opt/mnemosyne/venv/bin/python -c "
import sqlite3
conn = sqlite3.connect('$DB')
tables = conn.execute(\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name\").fetchall()
names = [t[0] for t in tables]
print(f'Tables: {len(names)} — {names}')
if 'memoria_facts' in names:
    print('Schema: v3.0+ (MEMORIA)')
elif 'annotations' in names:
    print('Schema: v2.8+ (E6 TripleStore split)')
elif 'episodic_memory' in names:
    print('Schema: v2.0+ (BEAM)')
else:
    print('Schema: v1.x (legacy)')
conn.close()
"
```

`chmod +x` and run once. Report output.

PART B — Baseline snapshot

```bash
/opt/mnemosyne/venv/bin/python -c "
import sqlite3, pathlib
db = pathlib.Path('${MNEMOSYNE_DATA_DIR}/banks/hermes-main/mnemosyne.db')
conn = sqlite3.connect(str(db))
schema = conn.execute(\"SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name\").fetchall()
for row in schema:
    print(row[0] + ';')
conn.close()
" > /opt/mnemosyne/schema_baseline_$(date +%Y%m%d).sql

ls -la /opt/mnemosyne/schema_baseline_*.sql
```

PART C — `/opt/mnemosyne/UPGRADE.md`

```markdown
# Mnemosyne upgrade procedure

See MNEMOSYNE.md §23 (upgrades) and §24 (rollback) for the full reference.

## Routine patch/minor (v3.1.2 → v3.1.x)

1. Backup: `/opt/mnemosyne/jobs/backup.sh`
2. Schema baseline: `bash /opt/mnemosyne/jobs/schema_version.sh > /tmp/pre-upgrade.txt`
3. Activate venv (Hermes's): `source <hermes-venv>/bin/activate`
4. Upgrade: `pip install --upgrade mnemosyne-memory`
5. Restart: `hermes gateway restart`
6. Verify: `hermes mnemosyne version`, `hermes doctor | grep -i memory`
7. Diff schema: `bash /opt/mnemosyne/jobs/schema_version.sh > /tmp/post-upgrade.txt; diff /tmp/pre-upgrade.txt /tmp/post-upgrade.txt`
8. Tail logs: `journalctl -u hermes -n 100 | grep -iE "migration|e6|memoria"`
9. Smoke recall on a known seeded memory

## Major (v2.7 → v3.0+)

Same as routine plus:
- BEFORE step 4: check `ls -la /opt/mnemosyne/data/banks/*/mnemosyne.db.pre_e6_backup`
- AFTER step 4: first init may take longer (auto-migration)
- AFTER step 7: expect new tables (MEMORIA: memoria_facts/timelines/instructions/preferences/kg)

## Rollback

Three options in order of preference (see Master §24):
1. Package: `pip install 'mnemosyne-memory==<version>' && hermes gateway restart`
2. E6 backup: copy `.pre_e6_backup` over, then downgrade
3. Nuke + re-import: export, rm DB, downgrade, import

## Loop-of-update bug

Agent suggests "update Mnemosyne" repeatedly even though it's already current.
Fix: kill the session, start fresh. See Master §38.8.
```

Report:
- Output of schema_version.sh
- Files at `/opt/mnemosyne/schema_baseline_*.sql`
- Path of UPGRADE.md
```

### Success criteria

- [ ] `schema_version.sh` reports schema correctly
- [ ] Baseline `.sql` file exists
- [ ] `UPGRADE.md` exists

---

## Phase 8 — Capabilities reference injection

**Why.** Drop `MNEMOSYNE.md` where the agent sees it. No more re-fetching docs.

### Message to Claude

```text
Inject the Mnemosyne master reference into the agent's permanent context.

STEPS

1. Transfer/create `/opt/mnemosyne/MNEMOSYNE.md`. If you don't have a copy on the VPS,
   ask me — I'll paste it (~2200 lines, ~80 KB).

2. Verify:
   ```bash
   ls -la /opt/mnemosyne/MNEMOSYNE.md
   wc -l /opt/mnemosyne/MNEMOSYNE.md   # ~2182 lines
   ```

3. Add reference block to Hermes' SOUL.md (or your top-level system prompt file —
   ask if location unclear):

   ```markdown
   ## Memory system reference

   This agent uses Mnemosyne v3.1.2 (SQLite-backed, BEAM + MEMORIA + Shared Surface
   architecture) as its memory layer. Complete capability list, scoring formula,
   tool reference, configuration knobs, and failure catalog at:

       /opt/mnemosyne/MNEMOSYNE.md

   That file is the single source of truth. Do not invent capabilities not listed
   there. If a needed capability seems missing, surface the gap rather than
   implementing around it.

   The three prohibitions (Master §40):
   1. NEVER run `hermes tools disable memory` — removes ALL memory tools
   2. NEVER use `hermes memory status` — known display bug
   3. NEVER assume `encryption_key` exists — encryption NOT implemented yet

   Bank: hermes-main
   Identity table: /opt/mnemosyne/IDENTITY.md
   Construction helper: /opt/mnemosyne/agent_factory.py
   Upgrade procedure: /opt/mnemosyne/UPGRADE.md
   ```

4. Create a self-discovery shortcut. `/opt/mnemosyne/jobs/capabilities.sh`:
   ```bash
   #!/usr/bin/env bash
   cat /opt/mnemosyne/MNEMOSYNE.md
   ```
   `chmod +x`.

5. Restart Hermes (`hermes gateway restart`) and confirm next turn's system prompt
   includes the new reference block.

Report:
- File hash + size of MNEMOSYNE.md
- SOUL.md path + diff
- Hermes restart status
- One turn after restart showing the new reference is in scope
```

### Success criteria

- [ ] `/opt/mnemosyne/MNEMOSYNE.md` exists (~2182 lines)
- [ ] SOUL.md references it
- [ ] Hermes restarted cleanly
- [ ] New reference appears in agent context

---

## Phase 9 — Full-flow smoke test

**Why.** Prove the whole pipeline works after all configuration.

### Message to Claude

```text
Run a full integration test. Save as `/opt/mnemosyne/jobs/integration_test.py`.

```python
"""
Full-flow integration test for Mnemosyne ↔ Hermes on Hetzner.
Uses ephemeral test memories with source='int-test-{ts}' for cleanup.
"""
import os, sys, time
from datetime import datetime, timezone
sys.path.insert(0, "/opt/mnemosyne")
from agent_factory import mem_for, recall_channel, recall_only
from mnemosyne import TripleStore

STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
SRC = f"int-test-{STAMP}"
print(f"== Integration test {STAMP} ==\n")

# 1. Identity-tagged writes
print("[1] Identity-tagged writes")
kamil = mem_for("kamil")
research = mem_for("hermes-research")
coder = mem_for("hermes-coder")
ops = mem_for("hermes-ops")

ids = {}
ids["kamil"] = kamil.remember("Test preference: light syntax highlighting.",
                               importance=0.7, source=SRC, veracity="stated")
ids["research"] = research.remember("Test finding: bge-small wins at 384-dim.",
                                     importance=0.7, source=SRC, veracity="tool")
ids["coder"] = coder.remember("Test note: refactored agent_factory.",
                               importance=0.5, source=SRC, veracity="tool")
ids["ops"] = ops.remember("Test ops: cron last sleep at 03:15 succeeded.",
                           importance=0.5, source=SRC, veracity="tool")
for k, v in ids.items():
    print(f"  {k}: {v[:8]}...")

# 2. Recall variants
print("\n[2] Recall variants")
for variant, args in [
    ("2a default", {"top_k": 3}),
    ("2b FTS-boosted", {"top_k": 3, "vec_weight": 20, "fts_weight": 60, "importance_weight": 20}),
    ("2c temporal (recent)", {"top_k": 5, "temporal_weight": 0.7, "temporal_halflife": 1}),
]:
    print(f"  {variant}")
    for r in kamil.recall("test", source=SRC, **args):
        print(f"    [{r['score']:.3f}] {r['content'][:60]}")

print("  2d author filter (research only)")
for r in recall_only("test", author_id="hermes-research", top_k=3):
    if r.get("source") == SRC:
        print(f"    {r['content'][:60]}")

print("  2e channel-wide (all 4 authors)")
for r in recall_channel("test", top_k=10):
    if r.get("source") == SRC:
        print(f"    [{r.get('author_id', '?')}] {r['content'][:60]}")

print("  2f veracity=stated only")
for r in kamil.recall("test", top_k=10, source=SRC, veracity="stated"):
    print(f"    [{r['veracity']}] {r['content'][:60]}")

# 3. Triples + auto-invalidation
print("\n[3] Triples + auto-invalidation")
ts = TripleStore(db_path=os.path.join(
    os.environ["MNEMOSYNE_DATA_DIR"], "banks", "hermes-main", "mnemosyne.db"))
ent = f"TestEntity-{STAMP}"
t1 = ts.add(subject=ent, predicate="has_property", object="A",
            confidence=1.0, source=SRC)
time.sleep(0.5)
t2 = ts.add(subject=ent, predicate="has_property", object="B",
            confidence=1.0, source=SRC)
print(f"  Added (id={t1}, id={t2}). Current state:")
for tr in ts.query(subject=ent):
    print(f"    {tr}")

# 4. Sleep dry-run
print("\n[4] Sleep dry-run")
print(f"  {kamil.sleep(dry_run=True)}")

# 5. Invalidate + forget
print("\n[5] Invalidate + forget")
print(f"  Invalidate {ids['kamil'][:8]}: {kamil.invalidate(ids['kamil'])}")
print(f"  Forget {ids['research'][:8]}: {research.forget(ids['research'])}")

# 6. Stats
print("\n[6] Stats")
stats = kamil.get_stats()
print(f"  WM: {stats['beam']['working_memory']}")
print(f"  EM: {stats['beam']['episodic_memory']}")
print(f"  Triples: {stats['beam']['triples']['total']}")

# 7. Cleanup
print("\n[7] Cleanup")
remaining = kamil.recall("test", top_k=20, source=SRC)
for r in remaining:
    if r.get("source") != SRC:
        continue
    author = r.get("author_id", "kamil")
    try:
        mem_for(author).forget(r["id"])
        print(f"  forgot {r['id'][:8]} (author={author})")
    except Exception as e:
        print(f"  ! failed {r['id'][:8]}: {e}")

print(f"\n== Integration test {STAMP} COMPLETE ==")
```

Run: `python /opt/mnemosyne/jobs/integration_test.py`

Report full output. If any error, paste traceback.
```

### Success criteria

- [ ] Reaches `COMPLETE` without exception
- [ ] All 7 sections produce expected output
- [ ] Cleanup leaves no `source='int-test-*'` memories

---

## Phase 10 — MCP SSE server (optional, for laptop access)

**Why.** Connect Claude Desktop on your laptop to the same bank.

**Reference.** Master §29.3 (cross-machine), §38 (failures).

### Message to Claude

```text
Stand up Mnemosyne MCP SSE server on the VPS so laptop Claude Desktop can connect.

PART A — Systemd service

`/etc/systemd/system/mnemosyne-mcp.service` (sudo):
```ini
[Unit]
Description=Mnemosyne MCP SSE Server (hermes-main bank)
After=network.target

[Service]
Type=simple
User=$(whoami)
EnvironmentFile=/etc/mnemosyne-mcp.env
ExecStart=/opt/mnemosyne/venv/bin/mnemosyne mcp --transport sse --port 8080 --bank hermes-main --host 0.0.0.0
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Replace `$(whoami)` with your actual username.

Create `/etc/mnemosyne-mcp.env` — convert from `/etc/profile.d/mnemosyne.sh` by stripping `export `:
```bash
sed 's/^export //' /etc/profile.d/mnemosyne.sh | grep -v '^#' > /etc/mnemosyne-mcp.env
sudo chmod 600 /etc/mnemosyne-mcp.env
```

Enable + start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable mnemosyne-mcp
sudo systemctl start mnemosyne-mcp
sudo systemctl status mnemosyne-mcp
ss -tlnp | grep 8080
curl -sN http://127.0.0.1:8080/sse | head -5
```

PART B — Firewall

Get laptop public IP first (on laptop): `curl ifconfig.me`. Then on VPS:
```bash
sudo ufw allow from <LAPTOP_PUBLIC_IP> to any port 8080 proto tcp
sudo ufw status verbose
```

⚠️ DO NOT open 8080 to 0.0.0.0 without firewall restriction or TLS.

PART C — TLS (recommended for non-test use)

nginx + Let's Encrypt:
```nginx
server {
    listen 443 ssl http2;
    server_name <your-subdomain>;
    ssl_certificate /etc/letsencrypt/live/<domain>/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/<domain>/privkey.pem;

    location /sse {
        proxy_pass http://127.0.0.1:8080/sse;
        proxy_http_version 1.1;
        proxy_set_header Connection '';
        proxy_buffering off;
        proxy_read_timeout 24h;
    }

    auth_basic "Mnemosyne MCP";
    auth_basic_user_file /etc/nginx/.mnemosyne_htpasswd;
}
```

PART D — Claude Desktop (laptop)

`claude_desktop_config.json` (Mac: `~/Library/Application Support/Claude/`):
```json
{
  "mcpServers": {
    "mnemosyne-hermes": {
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/inspector",
        "--url",
        "https://<your-domain>/sse"
      ]
    }
  }
}
```

Restart Claude Desktop. Expect 7 MCP-native tools (mnemosyne_remember/recall/sleep/
get_stats/scratchpad_*).

PART E — Verify

In laptop Claude Desktop:
```
mnemosyne_get_stats
mnemosyne_recall query="active projects" top_k=3
```

Should match VPS-side stats and return memories from Phase 6 seeding.

Report:
- `systemctl status mnemosyne-mcp` output
- Local curl test
- UFW status (redact other rules)
- Whether TLS is set up (or skipped for now)
- Whether Claude Desktop sees tools

ALTERNATIVE if you don't want public exposure: SSH port-forward instead.
```bash
ssh -L 8080:127.0.0.1:8080 <vps>
```
Then point Claude Desktop at `http://127.0.0.1:8080/sse`. No firewall changes needed.
```

### Success criteria

- [ ] `mnemosyne-mcp` service active
- [ ] Port 8080 listening, reachable from laptop
- [ ] Claude Desktop sees the MCP tools
- [ ] `mnemosyne_get_stats` from laptop matches VPS

### Fallback

- Service fails → `journalctl -u mnemosyne-mcp -n 50`
- Network blocked → SSH port-forward alternative
- Don't want public exposure → use SSH tunnel only

---

## Diagnostic prompt (if something goes wrong mid-build)

Send this if a phase fails:

```text
Mnemosyne integration is in a bad state. Diagnose without making changes.

State:
- Phase reached: <fill in>
- Last known-good phase: <fill in>
- Symptoms: <fill in>

Gather and report as a Markdown report:

A. ENVIRONMENT
   - `env | grep MNEMOSYNE | sort`
   - `which python && python --version`
   - `pip show mnemosyne-memory | grep -E 'Name|Version|Location'`
   - `pip show sqlite-vec | grep -E 'Name|Version'`
   - `pip show fastembed | grep -E 'Name|Version'`

B. MNEMOSYNE SELF-CHECK
   - `python -c "from mnemosyne import Mnemosyne; m=Mnemosyne(bank='hermes-main'); print(m.diagnose())"`
   - `python -c "from mnemosyne import Mnemosyne; m=Mnemosyne(bank='hermes-main'); print(m.get_stats())"`

C. DB INTEGRITY
   - `sqlite3 /opt/mnemosyne/data/banks/hermes-main/mnemosyne.db "PRAGMA integrity_check"`
   - Schema check (run /opt/mnemosyne/jobs/schema_version.sh if it exists)
   - `du -h /opt/mnemosyne/data/banks/hermes-main/mnemosyne.db`

D. HERMES (use the RIGHT commands, NOT `hermes memory status`)
   - `hermes plugins list`
   - `hermes tools list | grep mnemosyne`
   - `hermes doctor | grep -i memory`
   - `hermes mnemosyne version` (if available)
   - `journalctl -u hermes -n 50 --no-pager`

E. CRON + JOBS
   - `crontab -l`
   - `ls -la /var/log/mnemosyne/`
   - tail of most recent log in /var/log/mnemosyne/

F. FILESYSTEM
   - `ls -la /opt/mnemosyne/`
   - `ls -la /opt/mnemosyne/data/banks/`
   - `df -h /opt`

Output as one report. Mark ✗/⚠ on anything wrong. DO NOT fix — diagnose only.
DO NOT use `hermes memory status` (known display bug).
DO NOT run `hermes tools disable memory` (removes ALL memory tools).
```

---

## Phase completion checklist

- [ ] Phase 0 — Pre-flight check
- [ ] Phase 1 — Install + dependencies (v3.1.2)
- [ ] Phase 2 — DB initialised + smoke test PASSED
- [ ] Phase 3 — Environment + YAML configured
- [ ] Phase 4 — Multi-agent identity wired
- [ ] Phase 5 — Hermes plugin wired (using `hermes doctor`, NOT `hermes memory status`)
- [ ] Phase 6 — Memory pre-loaded from real sources only
- [ ] Phase 7 — Cron jobs (sleep + backup + DR verify) running
- [ ] Phase 7.5 — Schema baseline captured, UPGRADE.md in place
- [ ] Phase 8 — MNEMOSYNE.md injected into agent context
- [ ] Phase 9 — Full-flow smoke test PASSED
- [ ] Phase 10 — MCP SSE for laptop (optional)

---

## After integration

The agent's permanent reference is `/opt/mnemosyne/MNEMOSYNE.md`. It does not need to re-fetch docs.

**The three prohibitions never change:**
1. NEVER run `hermes tools disable memory`
2. NEVER use `hermes memory status` as verification
3. NEVER assume application-level encryption exists

For ongoing operations (upgrades, troubleshooting, monitoring, etc) consult MNEMOSYNE.md by section.

---

*End of integration phases. v3.1.2, all corrections applied.*
