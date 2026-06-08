# ARCHITECTURE REVIEW — is the HiveOS skeleton built the best possible way?

> Requested checkpoint (Kamil): pause before the heavy porting (P3+) and prove the
> skeleton + target structure are genuinely best-in-class for a system of this
> class — not merely "fine". Full refactor permission granted (root → folders).
> This document stress-tests the design in `SYNTHESIS.md` against (a) the four
> audited reference systems (OpenJarvis, Hermes, Mnemosyne, OpenClaw) and (b)
> mainstream Python packaging / agent-runtime best practice, then lists what was
> changed in this pass, what is recommended, and the decisions that are Kamil's.

## Verdict (top line)
The skeleton is **structurally sound and on the right track** — `src/` layout,
typed dataclasses, a registry + EventBus spine, SQLite-first storage, an explicit
acyclic dependency rule, and an untouched safety spine are all the *correct*
choices and match what the reference systems converged on. It was **not yet
optimal** in three respects, two of which are fixed in this pass (naming;
structure-enforcing tests + CI) and one of which is a genuine design decision
left for Kamil (config shape). With those, the foundation is ready for P2/P3.

## Naming correction applied (Kamil's note)
**Hive** is the agent; **OS** = operating system. So the *system / repo* stays
**HiveOS**, but the importable Python package is now **`hive`**, not `hiveos`:

- `src/hiveos/` → `src/hive/`; every import is `from hive.core import …`.
- `pyproject` distribution name `hive`; entry points `hive` / `hive-doctor`.
- The system name **HiveOS** is preserved everywhere it denotes the OS (docstrings,
  systemd units `hiveos-*.service`, the state-DB concept). Only the *code symbol*
  changed. Verified: `import hive` + full test suite green on Linux.

---

## 1. What is already right (validated, keep)

Each row is a dimension a reviewer would challenge, the verdict, and *why it is
best practice here* — not just "looks ok".

| Dimension | Verdict | Why it is the right call |
|---|---|---|
| **`src/` layout + `pyproject`** | ✅ keep | PyPA-recommended src-layout prevents accidental imports of the working tree, forces an installed/importable package, and *is the structural fix for the casing showstopper* (HIVEOS_AUDIT §0). Matches OpenJarvis. |
| **Typed dataclasses for the chat protocol** (`core/types.py`) | ✅ keep | One canonical `Message/ToolCall/ToolResult/ModelSpec`; adapters normalize at the edge. `slots=True` keeps them cheap. This is exactly OpenJarvis's contract and avoids the dict-soup that bit the old HiveOS. |
| **Registry + EventBus spine** | ✅ keep | Decorator registry with per-subclass isolation (`__init_subclass__`) + a thread-safe pub/sub bus is the proven extensibility pattern from OpenJarvis. Observability subscribes instead of coupling to producers — the right inversion. |
| **SQLite-first storage** | ✅ keep | OpenClaw's hard rule; Hermes + Mnemosyne both do it. One state DB + Mnemosyne's memory DB, no JSON sidecars for runtime state. |
| **Explicit acyclic dependency rule** (A.3) | ✅ keep + now enforced | `core` is a leaf; `llm/memory/tools/context → core`; `agents → those`; `gateway/autonomy/surfaces → agents`. Mirrors OpenJarvis's directed graph and OpenClaw's "core stays plugin-agnostic". *Now machine-checked* (see §2, F4). |
| **PROTECTED safety spine untouched** | ✅ keep | `Config/SOUL.md` + `Core/approval_gate.py` referenced by a read-only/path bridge, never edited or moved. Tests assert byte-identity and that the gate still fires. |
| **Env-driven model strings** | ✅ keep | `HIVE_EXEC_MODEL` etc. stay out of code because MiniMax moves M2→M3. Correct — no model ids hardcoded in logic. |
| **Doctor as the migration owner** | ✅ keep | `hive doctor [--fix]` is the single place legacy shapes are migrated; runtime reads only canonical shape (OpenClaw §2). Scaffolded correctly. |

## 2. Findings & refinements (ranked)

### F1 — Package naming `hiveos` → `hive` · **APPLIED**
See above. Cheapest at skeleton stage; done now rather than after porting.

### F2 — Structure is asserted in prose but not enforced · **APPLIED**
A "best" architecture *guarantees* its invariants. Added:
- **`tests/test_architecture.py`** — imports every `hive.core.*` leaf and asserts it
  pulls in no higher layer (`hive.llm/agents/gateway/...`), so the dependency rule
  in A.3 cannot silently rot. This is the lightweight analog of OpenClaw's
  `check:import-cycles`.
- **`.github/workflows/ci.yml`** — runs `py_compile` + the pytest suite on Python
  3.11 and 3.12. The audit's root cause (a casing bug that only shows on Linux,
  never on the author's mac) is exactly the class of failure CI exists to catch.
  Every phase's "verify" step now also runs in CI, not just locally.

### F3 — Config did filesystem/process I/O at import time · **APPLIED** (Kamil approved D1)
`core/config.py` calls `load_dotenv(...)` and `DATA_DIR.mkdir(...)` at module
import, and exposes module-level constants. This works but has real costs:
importing *anything* mutates the process env and creates a `data/` dir; tests
can't construct alternate configs; `doctor` can't diff "old shape vs new shape"
cleanly. Best practice (and OpenClaw's "runtime reads canonical config" intent)
is a **typed, immutable config object built explicitly**:

```python
@dataclass(frozen=True, slots=True)
class HiveConfig:
    exec_model: str; gateway_port: int; state_db: Path; ...
    @classmethod
    def from_env(cls, root: Path | None = None) -> "HiveConfig": ...
```

Loaded once at startup, passed via `HiveOS.build()` (P7). No import-time side
effects; trivially testable; the natural input to doctor migrations. **Done:**
`HiveConfig.from_env()` + `get_config()/set_config()` + `ensure_dirs()`; consumers
(`doctor`, `credentials`) migrated; a test asserts it is frozen, typed, and creates
no dirs until asked.

### F4 — Credential JSON file vs SQLite-first · **ACCEPT, documented**
`core/credentials.py` writes a `0o600` JSON file, seemingly against OpenClaw's
"no JSON sidecars". Resolved: a credential store is a **named product artifact**
(like OpenClaw's `~/.openclaw/credentials/`), not runtime state/cache — the
SQLite-first rule targets queues/indexes/cursors, not the secret vault. Verdict:
**keep**, with a one-line contract comment so the exception is explicit.

### F5 — EventBus is synchronous in an async-first core · **KEEP, document contract**
Subscribers run inline on the publishing thread. That is OpenJarvis's design and is
correct *provided subscribers are fast/non-blocking* (telemetry append, audit
write). The contract — "subscribers must not block; offload slow work" — should be
a docstring invariant rather than a code change. No async bus needed yet.

### F6 — Relocating the PROTECTED files into the package · **DECISION (deferred)**
The bridge (`importlib` for the gate, path-read for SOUL) is slightly less elegant
than having both files inside `src/hive/core/`. A content-identical *move* is not an
*edit*, and Kamil granted folder-refactor permission — but these two files are the
safety spine, so the responsible default is to **keep the bridge through the risky
build and relocate in P9 with an explicit nod** (see §5, D2). Cheap to do later;
keeps the "never moved" hard limit unambiguous while everything else churns.

## 3. Alternatives considered and rejected

- **Flat top-level package (no `src/`).** Rejected: re-opens the casing/shadowing
  class of bug; src-layout is the deliberate fix.
- **Splitting `llm/` into `intelligence/`+`engine/`+`learning/`** (OpenJarvis's 3-way
  split). Rejected for a solo system: more folders than payload. `llm/{router,
  failover,credential_pool,model_catalog,adapters}` carries the same concepts with
  less ceremony. Revisit only if learning/routing grows real weight.
- **Sync-first core with async bridges** (Hermes's shape). Rejected: HiveOS's
  gateway is already FastAPI/async and providers are network-bound; async-first with
  sync bridges where libs are sync is the lower-friction choice here.
- **JSON/JSONL for sessions/memory.** Rejected: SQLite-first (FTS5 for session
  recall) per OpenClaw/Hermes/Mnemosyne.
- **Multi-package monorepo** (OpenClaw's `packages/*`). Rejected: overkill for one
  agent; a single installable `hive` with clean internal layers is right-sized.

## 4. Directory layout & casing (full refactor applied)

### The problem found
CLAUDE.md (the canonical conventions doc) declares **all-lowercase** paths
(`config/`, `core/`, `docs/`, `tools/`, `scripts/` …), but the real tree shipped
**Capitalized** top-level dirs. Worse, the legacy code itself imports lowercase
(`from core import settings`, `from memory.brain import brain`) while living in
`Core/`/`Memory/` — i.e. **the legacy modules already do not import on Linux**: the
audit's casing showstopper (HIVEOS_AUDIT §0) is live, repo-wide, not just in one
module. Best-build intent is unambiguous: lowercase, case-correct directories.

### What was changed
Persistent, non-protected directories were lowercased (and every reference updated
repo-wide; this also fixes Linux paths like `python -m scripts.ping`):
`Docs→docs`, `Deploy→deploy`, `Dashboard→dashboard`, `Scripts→scripts`.

### The two deliberate exceptions (every remaining Capitalized dir)
- **`Config/`, `Core/` — PROTECTED.** They hold `SOUL.md` / `approval_gate.py`,
  which the hard limit forbids moving or renaming. Per D2 they stay verbatim,
  in place, and are relocated into `src/hive/core/` only at P9. Renaming the dir =
  renaming the protected path, so it waits.
- **`Gateway/`, `Memory/`, `Tools/` — LEGACY, removed at P9.** These are the
  porting *source*; the approved plan keeps old code until each replacement reaches
  parity (no premature deletion of references). Renaming them buys nothing — their
  lowercase imports are blocked by the protected capital `Core/` regardless — so
  they keep their capital as the visible "to-be-removed" marker. At P9 they are
  deleted, not renamed.

Net: **the canonical/persistent tree is now fully best-practice lowercase; the only
capitals left are exactly the protected files and the soon-deleted legacy source.**
The single-tree end state is reached at P9 by construction.

### Finalized tree (current)
```
HiveOS/                          # repo / system = "HiveOS" (the OS)
  pyproject.toml  .env.example  README.md  AGENTS.md  CLAUDE.md
  src/hive/                      # the agent "Hive" — the only runtime package
    core/   {registry,events,types,config,doctor,credentials,soul,approval,
             self_mod,system}.py
    llm/    {router,failover,credential_pool,model_catalog}.py  adapters/
    agents/ {base,orchestrator,executor,loop_guard,delegate,planner}.py
    memory/ {provider,mnemosyne_provider,keeper,vault}.py
    context/{session_store,compaction,prompt_builder}.py
    tools/  {base,registry,executor,discovery}.py  mcp/  builtins/
    gateway/{app,protocol,auth}.py  channels/
    autonomy/{heartbeat,cron,tasks,commitments}.py
    surfaces/{cli,voice}.py
    observability/{telemetry,traces,audit}.py
  tests/  docs/  deploy/  dashboard/  scripts/  .github/workflows/ci.yml
  data/  vault/                  # runtime (gitignored)
  Config/SOUL.md                 # PROTECTED, in place until P9  (exception)
  Core/approval_gate.py          # PROTECTED, in place until P9  (exception)
  Core/ Gateway/ Memory/ Tools/  # LEGACY porting source, deleted at P9 (exception)
```

## 5. Decisions (resolved with Kamil)

- **D1 — Config shape → typed `HiveConfig.from_env()`.** ✅ Applied (F3).
- **D2 — PROTECTED relocation → keep the bridge through the build, relocate
  `SOUL.md`/`approval_gate.py` into `src/hive/core/` in P9** with an explicit nod.
  Bridge unchanged for now.

## 6. Go / no-go
F1/F2/F3 applied and D1/D2 resolved — the skeleton meets the bar: the right
structure, now enforced and typed, for the four-reference synthesis. **Greenlit:
P2 (LLM + resilience) then P3 (Mnemosyne memory — biggest leverage)**, each as its
own commit with the per-phase verify running in CI.
</content>
</invoke>
