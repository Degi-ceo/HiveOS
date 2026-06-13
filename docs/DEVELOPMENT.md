# HiveOS — Development Guide

How to set up a local development environment, run the test suite, add new features,
and understand the architectural rules that keep the codebase safe and maintainable.

---

## Prerequisites

- Python 3.11 or 3.12 (both are tested in CI)
- git (for self-mod worktrees)
- Node ≥ 18 (optional — only for dashboard development)

---

## Quick start

```bash
git clone https://github.com/hiveosagent/hiveos.git
cd hiveos
python -m pip install -e ".[dev]"   # editable install with test deps
cp .env.example .env                 # add at least MINIMAX_API_KEY
python -m compileall -q src/hive     # compile check
pytest -q                            # full suite (~364 tests, ~15 s)
hive doctor --fix                    # health check + dir creation
hive ask "say hi"                    # one-shot turn (needs API key)
```

---

## Project structure

```
src/hive/                   installable package (see ARCHITECTURE.md)
  core/                     leaf layer — no imports from higher layers
  llm/                      router, adapters, resilience
  agents/                   orchestrator, delegate, planner
  memory/                   Mnemosyne bridge + local fallback + vault
  context/                  sessions, compaction, prompt builder
  tools/                    registry, executor, builtins, MCP
  gateway/                  FastAPI app, auth, Telegram, protocol
  autonomy/                 heartbeat, cron, tasks, commitments
  surfaces/                 CLI, voice
  observability/            telemetry, traces, audit
  runtime.py                composition root (HiveOS + HiveOS.build)
tests/                      pytest suite (one file per subsystem)
  conftest.py               global fixtures — singleton reset, config isolation
dashboard/                  React + Vite SPA (Mission Control)
deploy/                     systemd units + deploy guide
Config/SOUL.md              PROTECTED — never edit
Core/approval_gate.py       PROTECTED — never edit
```

---

## Environment

`.env` is loaded by `HiveConfig.from_env()` at startup (not at import time). The minimal
set for offline development:

```bash
HIVE_SECRET=dev-secret          # any string
MINIMAX_API_KEY=your_key_here   # only needed for hive ask / live tests
```

For tests that do not need network access, no `.env` is required — tests inject a `_ScriptRouter`
that returns canned responses without making HTTP calls.

See [`docs/CONFIGURATION.md`](CONFIGURATION.md) for all variables.

---

## Running tests

```bash
pytest -q                    # full suite, fast (no network)
pytest -q -x                 # stop at first failure
pytest tests/test_runtime.py # single file
pytest -k "mcp"              # keyword filter
HIVE_LIVE_TEST=1 pytest -q   # include live API smokes (needs valid keys)
```

The architecture DAG test (`tests/test_architecture.py`) runs an AST scan to assert that
`hive.core.*` never imports any higher-layer module, even in function-local imports. This
prevented a real `core→llm` leak once.

---

## Adding a new tool

1. Add a class in `src/hive/tools/builtins/__init__.py` (or a new file under `tools/`):

```python
class MyTool(BaseTool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="my_tool",
            description="Does something useful",
            parameters={
                "type": "object",
                "properties": {"input": {"type": "string", "description": "The input"}},
                "required": ["input"],
            },
            dangerous=False,   # set True if the tool has side effects
            category="utility",
        )

    async def execute(self, input: str = "", **_) -> ToolResult:
        return ToolResult(tool_name="my_tool", content=f"processed: {input}")
```

2. Register it in `register_builtins()`:

```python
registry.register(MyTool())
```

3. Add a test in `tests/test_tools.py` (or a dedicated file).

**Important:** If `dangerous=True`, the tool executor routes calls through the approval gate.
The user sees the call in `GET /approvals` and must approve it. Never mark a tool `dangerous=False`
if it has irreversible side effects (file writes, network posts, code execution).

---

## Adding a new gateway endpoint

All endpoints live in `src/hive/gateway/app.py` inside `create_app(hive)`. Add a new
route following the existing pattern:

```python
@app.get("/my_endpoint", dependencies=[Depends(require_token)])
async def my_endpoint() -> dict:
    return hive.some_subsystem.snapshot()
```

- Use `Depends(require_token)` for all authenticated endpoints.
- Keep endpoints thin — they call `hive.*` methods, never subsystem internals directly.
- Add the new endpoint to the docstring at the top of `app.py` and to [`docs/API.md`](API.md).

---

## Architectural rules (enforced)

### 1. `core` is a leaf

`src/hive/core/` imports nothing from `llm/`, `agents/`, `memory/`, `context/`, `tools/`,
`gateway/`, `autonomy/`, `surfaces/`, or `runtime.py`. Cross-layer needs are injected
(e.g. `MemoryKeeper` takes a `Summarizer` callable — it never imports `llm`).

Enforced by `tests/test_architecture.py` — an AST scan that will catch even function-local
imports. If you add a `from hive.llm import ...` inside any `core/` file, CI fails.

### 2. `runtime.py` is not in any layer

The composition root is `src/hive/runtime.py` — a peer of all layer directories. It is the
only file allowed to import every layer, because its job is to wire them together.

### 3. PROTECTED files are never modified

`Config/SOUL.md` and `Core/approval_gate.py` are loaded read-only via bridges in
`src/hive/core/{soul,approval}.py`. The self-modifier (`core/self_mod.py`) refuses any
change touching them. **Never edit them directly.** They require Kamil's manual merge.

### 4. Hive never merges to main

All code changes go through: branch → tests → PR → human merge. The self-modifier opens
draft PRs only. `git push --force` to `main` is blocked by branch protection.

### 5. Discovery-first

Before building any new capability, search official sources (Anthropic Skills, MCP Registry,
modelcontextprotocol/servers, GitHub). Record the research result in memory so the same
search is never repeated. See `tools/discovery.py`.

---

## Self-modification flow

When Hive proposes a code change:

1. `core/spec_search.py` assigns a `RiskTier` from a deterministic table (model cannot self-escalate)
2. **AUTO tier:** `core/self_mod.py::SelfModifier.propose` — isolated git worktree → apply edit → run tests → push branch → open draft PR via GitHub REST API
3. **REVIEW tier:** queued in the approval gate + `HiveOS.edit_pending`; visible at `GET /approvals`; `POST /approvals/decide` applies it via `SelfImprovement.apply_approved`
4. **MANUAL tier:** recorded only; a human implements it

The worktree is cleaned up after the PR is opened. On test failure, the last-known-good
snapshot is restored and the failure is written to memory.

---

## Adding a new config option

1. Add the field to `HiveConfig` in `src/hive/core/config.py`:

```python
my_option: str
```

2. Read it in `from_env()`:

```python
my_option=os.getenv("HIVE_MY_OPTION", "default"),
```

3. Add it to `.env.example` with a comment.
4. Add it to [`docs/CONFIGURATION.md`](CONFIGURATION.md).
5. Wire it in `HiveOS.build()` where the subsystem is constructed.
6. Add a test for the default and a non-default value in `tests/test_core_health.py`.

---

## Dashboard development

```bash
cd dashboard
npm ci                 # install deps from package-lock.json
npm run dev            # Vite dev server on :5173 (proxies /chat to :8088)
npm run build          # production build → dashboard/dist/
```

The `dashboard/dist/` directory is gitignored. `hive serve` mounts it at `/app` if it
exists. The gateway serves raw JSON if `dist/` is absent — the API works without the SPA.

---

## CI

`.github/workflows/ci.yml` runs on every push and PR:

1. `python -m pip install -e ".[dev]"` — editable install
2. `python -m compileall -q src/hive` — compile check (catches syntax errors and import issues)
3. `python -c "import hive; from hive.core import soul, approval, config, doctor"` — import smoke (catches casing bugs that only surface on Linux)
4. `pytest -q` — full test suite

Both Python 3.11 and 3.12 are tested in a matrix. CI is required to pass before any PR can be merged.

---

## Commit conventions

```
<type>(<scope>): <short description>

<body — why, not what>
```

Types: `feat`, `fix`, `test`, `docs`, `build`, `refactor`, `security`.
Scope: module or area (e.g. `runtime`, `gateway`, `spec_search`, `dashboard`).

Examples:
```
feat(gateway): add /traces endpoint exposing session event log
fix(spec_search): REVIEW-tier approval now stores edit in edit_pending
security(runtime): path traversal guard in _diagnoser _apply closure
```

---

## Common pitfalls

| Pitfall | Fix |
|---|---|
| `ModuleNotFoundError: hive` | Run `pip install -e .` first |
| `ImportError: mcp not installed` | `pip install -e ".[mcp]"` |
| `ImportError: croniter not installed` | `pip install -e ".[cron]"` |
| Tests leaking state between runs | Check `tests/conftest.py` — the autouse fixture resets `gate._pending` and `_CONFIG` |
| Architecture test fails | You added a `hive.llm` or `hive.agents` import inside `src/hive/core/` — move it to a higher layer or inject |
| `hive doctor` reports missing DB | Run `hive doctor --fix` — it creates the `data/` directory and initialises the schema |
| Self-mod fails at `git worktree add` | Ensure the repo has at least one commit and `git status` is clean |
