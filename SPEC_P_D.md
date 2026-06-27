# SPEC — P-D A2A Envelope (SPRINT_6, issue #72)

> **Worktree:** `/home/hive/hiveos/.worktrees/sprint6-a2a`
> **Branch:** `sprint6/a2a-envelope` (cut from `main` @ 8dd4b88)
> **Issue:** #72
> **Owner:** coder sub-agent (Hive orchestrator)
> **Merger:** Hive (NOT Kamil — per his go-ahead in this session)

## Goal

Formalize the 5 named sub-agents behind a minimal JSON-RPC-style envelope so future
external agents can connect to the same contract. **Internal-only for this phase:**
remote-agent HTTP bridge is out of scope (deferred to next sprint per SPRINT_6 doc L127).

## Acceptance (from `docs/sprints/SPRINT_6_AUTONOMY_LIB.md` L113-133)

1. **Local sub-agent round-trip works:**
   `a2a.call("researcher", task="...")` returns a result via the envelope
2. **Mock HTTP server accepted by `A2AClient`:**
   timeout + retry wired
3. **100% coverage on `src/hive/agents/a2a/`** (all new files)
4. **No behavior change for existing `delegate_to_specialist` callers** (snapshot test)
5. New gateway endpoint `POST /a2a/rpc` (auth-gated via `Depends(require_token)`)

## Files to create

```
src/hive/agents/a2a/__init__.py        # public API: call, register, A2AError
src/hive/agents/a2a/envelope.py        # Pydantic models: Request, Response, Error
src/hive/agents/a2a/router.py          # routes envelopes to local agents OR remote URI
src/hive/agents/a2a/client.py          # A2AClient (httpx-based, timeout + retry)
tests/test_a2a.py                      # full coverage + snapshot test
```

## Files to modify

- `src/hive/agents/delegate.py` — wrap `delegate_to_specialist` to emit/consume envelope
- `src/hive/agents/registry.py` — expose named agents via A2A contract
- `src/hive/gateway/app.py` — add `POST /a2a/rpc` endpoint
- `docs/STATUS.md` — add P-D section (Hermes/OpenClaw rule: docs change with behavior)

## Envelope shape (minimal)

```python
class A2ARequest(BaseModel):
    id: str                    # uuid4 for correlation
    method: str                # e.g. "researcher.run"
    params: dict[str, Any]     # task, context, etc.

class A2AResponse(BaseModel):
    id: str                    # mirrors request.id
    result: Any | None = None
    error: A2AError | None = None

class A2AError(BaseModel):
    code: int                  # -32601 (method not found), -32603 (internal), etc.
    message: str
    data: dict | None = None
```

**Deliberately minimal — NOT full JSON-RPC 2.0** (no batch, no notifications). Our internal
contract only. Future remote-agent bridge can add JSON-RPC 2.0 conformance if needed.

## Read these files BEFORE editing

1. `src/hive/agents/delegate.py` — current `delegate_to_specialist` implementation
2. `src/hive/agents/registry.py` — how 5 named agents are registered
3. `src/hive/runtime.py` L96-99 — `agents_registry` dict + `register_agent`
4. `src/hive/gateway/app.py` — pattern for auth-gated endpoints (look at `/learning/*`)
5. `tests/test_m10_observability.py` — gateway test pattern to follow
6. `docs/sprints/SPRINT_6_AUTONOMY_LIB.md` L113-133 — full SPRINT_6 scope for P-D

## Rules (CLAUDE.md + coder.md)

- **Never edit** `Config/SOUL.md` or `Core/approval_gate.py`
- **Never push directly to `main`** — branch only, push, open PR
- **No abstractions beyond what's needed** — three similar lines beats premature helper
- **No comments** unless WHY is non-obvious
- **No docstrings** longer than one line
- **No backwards-compat shims** — `delegate_to_specialist` calls envelope internally; old
  callers see no behavior change via snapshot test, not via compat layer

## Test style

- `asyncio.run()` in sync test functions (no `@pytest.mark.asyncio`)
- Use `_ScriptRouter` with `CompletionResult(text=..., model="test")` (no flat token fields)
- Pattern: see `tests/test_m10_observability.py`
- Snapshot test: capture old `delegate_to_specialist` output for a known input, confirm
  new envelope-based path produces identical output

## Acceptance verification (run before opening PR)

```bash
cd /home/hive/hiveos/.worktrees/sprint6-a2a
source ../../.venv/bin/activate

# 1. Compile check
python -m compileall src/hive

# 2. Lint
ruff check src/ tests/

# 3. New module tests + coverage
pytest tests/test_a2a.py -q
coverage erase
coverage run --source=src/hive/agents/a2a -m pytest tests/test_a2a.py -q
coverage report --include="src/hive/agents/a2a/*" --fail-under=100

# 4. Full suite (no regressions)
pytest -q   # must show 3657 + N passing (your N new tests)

# 5. Gateway endpoint smoke
pytest tests/test_gateway.py -q -k "a2a"
```

## Commit + PR

```bash
git add -A
git commit -m "feat(a2a): P-D A2A envelope (SPRINT_6, #72)

- New agents/a2a/ package (envelope, router, client)
- delegate_to_specialist now emits/consumes A2A envelope internally
- New POST /a2a/rpc endpoint (auth-gated)
- Snapshot test proves no behavior change for existing callers
- 100% coverage on src/hive/agents/a2a/*

Co-Authored-By: Claude <noreply@anthropic.com>"

git push -u origin sprint6/a2a-envelope

gh pr create \
  --title "feat(a2a): P-D A2A envelope (SPRINT_6) — closes #72" \
  --body "## Summary
Implements P-D of SPRINT_6: formalizes the 5 named sub-agents behind a minimal
JSON-RPC-style envelope so future external agents can connect via the same contract.

## Scope
- New src/hive/agents/a2a/ package (envelope, router, client)
- delegate_to_specialist refactored to emit/consume A2A envelopes internally
- New POST /a2a/rpc endpoint (auth-gated)
- agents/registry.py exposes named agents via A2A contract

## Files changed
- src/hive/agents/a2a/{__init__,envelope,router,client}.py (new)
- src/hive/agents/{delegate,registry}.py (modified)
- src/hive/gateway/app.py (+1 endpoint)
- tests/test_a2a.py (new, 100% coverage)
- docs/STATUS.md (P-D section added)

## Test plan
- [x] Local round-trip: a2a.call('researcher', task='...') returns result
- [x] Mock HTTP server: A2AClient accepts with timeout + retry
- [x] 100% coverage on src/hive/agents/a2a/*
- [x] Snapshot test: delegate_to_specialist output unchanged
- [x] pytest -q green (full suite, 3657 + N passing)
- [x] ruff check src/ tests/ clean

## Acceptance (from SPRINT_6 doc L113-133)
- [x] Local sub-agent round-trip works
- [x] Mock HTTP server accepted by A2AClient
- [x] 100% coverage on agents/a2a/
- [x] Snapshot test (no behavior change)
- [x] POST /a2a/rpc endpoint added

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

## Do NOT merge

Leave the PR open. Hive will run reviewer + security-reviewer sub-agents, then merge.

## Status file

When done, append to `docs/STATUS.md` after the P-F section:

```markdown
### P-D — A2A protocol envelope (issue #72, branch `sprint6/a2a-envelope`)

- **PR:** <number> · **State:** OPEN (awaits Hive merge)
- New `src/hive/agents/a2a/` package: envelope (Pydantic), router (local + remote), client (httpx)
- `delegate_to_specialist` refactored to emit/consume envelope internally — no behavior change for callers (snapshot test)
- New `POST /a2a/rpc` gateway endpoint (auth-gated via `require_token`)
- 100% coverage on `src/hive/agents/a2a/*`
- Full suite <N> passing · ruff clean
```

## Report back to Hive

When done, report:
- PR number + URL
- Total files changed
- Test count delta
- Coverage % on new module
- Any decisions you made that weren't in this SPEC (justify briefly)