# SPEC — P-H AST Tool Auto-Discovery (SPRINT_6, issue #76)

> **Worktree:** `/home/hive/hiveos/.worktrees/sprint6-ast`
> **Branch:** `sprint6/ast-tool-discovery` (cut from `main` @ 8dd4b88)
> **Issue:** #76
> **Owner:** coder sub-agent (Hive orchestrator)
> **Merger:** Hive (NOT Kamil — per his go-ahead in this session)

## Goal

Self-introspection of `tools/builtins/` + `tools/mcp/*` so Hive can answer
"what tools do you have?" from its own state, not external docs. Augments the
existing `discover` builtin with a local AST fast-path (web search only when local
index scores below threshold).

## Acceptance (from `docs/sprints/SPRINT_6_AUTONOMY_LIB.md` L202-221)

1. `python -c "from hive.tools.introspect import index; print(len(index()))"`
   returns **≥30** (current tool count)
2. `discover` query "github pr list" returns `GitHubListPRs` with score > 0.8
   from **local AST** (no web hit needed)
3. **100% coverage on `src/hive/tools/introspect.py`**
4. **Negative test:** malformed tool module is skipped with a logged warning,
   not crashed
5. `hive ask "what tools do you have for deploying?"` returns concrete list from
   AST index, with `source: ast` attribution

## Files to create

```
src/hive/tools/introspect.py          # AST walker + index builder + search
tests/test_introspect.py              # full coverage + malformed module test
```

## Files to modify

- `src/hive/tools/builtins/__init__.py` — augment `discover` to check AST first
- `src/hive/tools/discovery.py` — add `score` + `source` fields to results
- `docs/STATUS.md` — add P-H section (Hermes/OpenClaw rule: docs change with behavior)

## Implementation outline

```python
# src/hive/tools/introspect.py
import ast
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_TOOL_ROOTS = (Path(__file__).parent / "builtins", Path(__file__).parent / "mcp")

def index(roots: tuple[Path, ...] = _TOOL_ROOTS) -> list[dict[str, Any]]:
    """Walk tool modules, AST-parse, extract BaseTool subclasses.
    Returns list of {"name": ..., "module": ..., "doc": ..., "args_schema": ...}.
    Skips malformed modules with a logged warning (never crashes)."""
    ...

def search(query: str, k: int = 5) -> list[dict[str, Any]]:
    """Score-based search over the index.
    Score = token overlap on name + docstring (simple but deterministic)."""
    ...

def format_for_discover(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shape results to match existing discover() output schema,
    with extra 'source': 'ast' field."""
    ...
```

## Read these files BEFORE editing

1. `src/hive/tools/builtins/__init__.py` — current `discover` implementation, score format
2. `src/hive/tools/discovery.py` — current result shape (what fields `discover` returns)
3. `src/hive/tools/base.py` — `BaseTool` class signature (so AST parser knows what to look for)
4. `src/hive/tools/executor.py` — how tools get registered (to confirm tool roots)
5. `src/hive/tools/registry.py` — tool registration
6. `tests/test_builtins.py` — pattern for discover tests
7. `docs/sprints/SPRINT_6_AUTONOMY_LIB.md` L202-221 — full SPRINT_6 scope for P-H

## Rules (CLAUDE.md + coder.md)

- **Use stdlib `ast` only** — no third-party AST libraries (ast-grep, libCST, etc.)
- **Never edit** `Config/SOUL.md` or `Core/approval_gate.py`
- **Never push directly to `main`** — branch only, push, open PR
- **No abstractions beyond what's needed** — three similar lines beats premature helper
- **No comments** unless WHY is non-obvious
- **No docstrings** longer than one line
- **Score function must be deterministic** — same query → same score (test for this)

## Test style

- `asyncio.run()` in sync test functions (no `@pytest.mark.asyncio`)
- Pattern: see `tests/test_builtins.py`
- Tests must include:
  - Happy path: index has ≥30 tools
  - Search ranking: known query returns expected top hit with score > 0.8
  - Malformed module: create a fake broken .py file, confirm it's skipped + warning logged
  - Source attribution: search result includes `"source": "ast"`

## Acceptance verification (run before opening PR)

```bash
cd /home/hive/hiveos/.worktrees/sprint6-ast
source ../../.venv/bin/activate

# 1. Compile check
python -m compileall src/hive

# 2. Lint
ruff check src/ tests/

# 3. Live index sanity
python -c "from hive.tools.introspect import index; print(len(index()))"
# Expect: >= 30

# 4. Live search sanity
python -c "from hive.tools.introspect import search; r = search('github pr list'); print(r[0])"
# Expect: top hit has name like 'github_*_prs' or 'github_list_prs', score > 0.8

# 5. New module tests + coverage
pytest tests/test_introspect.py -q
coverage erase
coverage run --source=src/hive/tools/introspect -m pytest tests/test_introspect.py -q
coverage report --include="src/hive/tools/introspect.py" --fail-under=100

# 6. Builtins tests (discover augment must not break existing tests)
pytest tests/test_builtins.py -q

# 7. Full suite (no regressions)
pytest -q   # must show 3657 + N passing (your N new tests)
```

## Commit + PR

```bash
git add -A
git commit -m "feat(introspect): P-H AST tool auto-discovery (SPRINT_6, #76)

- New src/hive/tools/introspect.py (stdlib ast walker + index + search)
- discover() augmented to check local AST first, web only on low score
- Malformed tool module skipped with warning, never crashed
- 100% coverage on src/hive/tools/introspect.py

Co-Authored-By: Claude <noreply@anthropic.com>"

git push -u origin sprint6/ast-tool-discovery

gh pr create \
  --title "feat(introspect): P-H AST tool auto-discovery (SPRINT_6) — closes #76" \
  --body "## Summary
Implements P-H of SPRINT_6: self-introspection of tools/builtins/ + tools/mcp/
via stdlib ast, augmenting the discover() builtin with a local fast-path.

## Scope
- New src/hive/tools/introspect.py (stdlib ast only — no new deps)
- discover() now checks local AST index first, falls back to web only when score < threshold
- Malformed tool modules logged + skipped, never crashed
- discover() result includes 'source': 'ast' attribution

## Files changed
- src/hive/tools/introspect.py (new)
- src/hive/tools/builtins/__init__.py (augment discover)
- src/hive/tools/discovery.py (add source field)
- tests/test_introspect.py (new, 100% coverage)
- docs/STATUS.md (P-H section added)

## Test plan
- [x] Index returns >= 30 tools (live sanity)
- [x] Search 'github pr list' returns top hit with score > 0.8 from AST (no web)
- [x] Malformed module: skipped + warning logged
- [x] 100% coverage on src/hive/tools/introspect.py
- [x] pytest -q green (full suite, 3657 + N passing)
- [x] ruff check src/ tests/ clean

## Acceptance (from SPRINT_6 doc L202-221)
- [x] Live index >= 30 tools
- [x] Search returns known top hit with score > 0.8 from local AST
- [x] 100% coverage on tools/introspect.py
- [x] Malformed module negative test
- [x] source attribution field added to discover results

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

## Do NOT merge

Leave the PR open. Hive will run reviewer + security-reviewer sub-agents, then merge.

## Status file

When done, append to `docs/STATUS.md` after the P-D section (or wherever fits):

```markdown
### P-H — AST tool auto-discovery (issue #76, branch `sprint6/ast-tool-discovery`)

- **PR:** <number> · **State:** OPEN (awaits Hive merge)
- New `src/hive/tools/introspect.py` (stdlib `ast` only — zero new deps)
- `discover()` augmented: local AST fast-path first, web fallback only on low score
- Malformed tool modules skipped with logged warning, never crashed
- 100% coverage on `src/hive/tools/introspect.py`
- Full suite <N> passing · ruff clean
```

## Report back to Hive

When done, report:
- PR number + URL
- Total files changed
- Test count delta
- Coverage % on new module
- Live index size (must be >= 30)
- Live search top hit for "github pr list" (name + score)
- Any decisions you made that weren't in this SPEC (justify briefly)