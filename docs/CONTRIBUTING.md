# Contributing to HiveOS

HiveOS is a self-modifying autonomous agent. Hive proposes changes via pull requests;
**Kamil reviews and merges** — this is the central safety invariant (see
[`docs/decisions/003-no-auto-merge.md`](decisions/003-no-auto-merge.md)).

---

## The most important rules (read first)

1. **Never edit `Config/SOUL.md` or `Core/approval_gate.py`.** These require Kamil's
   manual merge. The self-modifier refuses any change touching them. Any PR that modifies
   these files will be rejected immediately.

2. **Never merge to `main` without human review.** All changes go through:
   `branch → tests → PR (draft → ready) → Kamil review → merge`.

3. **Never skip CI.** `pytest -q` must pass on py3.11 + py3.12 before a PR is marked
   ready. `python -m compileall -q src/hive` must also pass.

4. **Docs change with behaviour** (Hermes/OpenClaw rule). Any PR that changes behaviour
   must update the relevant doc — at minimum `docs/STATUS.md` and the module docstring.

---

## Branch naming

| Type | Pattern | Example |
|---|---|---|
| Feature | `hive/<scope>-<description>` | `hive/gateway-traces-endpoint` |
| Bug fix | `hive/fix-<description>` | `hive/fix-risktier-comparison` |
| Docs | `hive/docs-<description>` | `hive/docs-api-reference` |
| Agent-opened | `claude/<description>` | `claude/review-completed-tasks-prs-1xkjl9` |
| Self-mod (AUTO) | `hive/auto-<timestamp>` | `hive/auto-1718123456` |

Always branch from `main`. Never reuse a branch after its PR is merged.

---

## Commit format

```
<type>(<scope>): <short description (imperative, ≤72 chars)>

<body — why, not what; optional>
```

**Types:**

| Type | When |
|---|---|
| `feat` | New capability or behaviour |
| `fix` | Bug fix |
| `test` | New or modified tests |
| `docs` | Documentation only |
| `build` | pyproject.toml, CI, deploy units |
| `refactor` | No behaviour change |
| `security` | Security hardening |

**Scope:** module or area, e.g. `runtime`, `gateway`, `spec_search`, `dashboard`, `cli`.

**Examples:**
```
feat(gateway): add /traces endpoint exposing session event log
fix(spec_search): REVIEW-tier approval now stores edit in edit_pending
security(runtime): path traversal guard in _diagnoser _apply closure
docs(api): add curl examples for all authenticated endpoints
test(conftest): reset _CONFIG to None before each test, not only teardown
```

---

## Pull request workflow

### Opening a PR

1. Push your branch: `git push -u origin <branch>`
2. Open a **draft** PR immediately after the first push
3. Fill in the PR template:
   - **Summary**: what changed and why (not a commit log)
   - **Test plan**: which tests cover the change; checklist of manual checks if UI
   - Link to any relevant `docs/decisions/` ADR if the PR encodes a new decision

### Marking ready for review

Before moving from draft to ready, verify:

- [ ] `python -m compileall -q src/hive` — no errors
- [ ] `python -m pytest -q` — all tests pass on your machine
- [ ] `import hive; from hive.core import soul, approval, config, doctor` — import smoke passes
- [ ] No conflict markers (`<<<<<<`) in any file
- [ ] `docs/STATUS.md` updated if any capability changed
- [ ] `docs/CHANGELOG.md` updated with the PR milestone
- [ ] No hardcoded secrets, tokens, or API keys
- [ ] `Config/SOUL.md` and `Core/approval_gate.py` are **untouched**

### After review

Kamil merges. Do not merge yourself. Do not rebase a PR branch after it has been reviewed
without re-requesting review.

---

## Code style

HiveOS has no linter config by choice — the architecture rules are the style guide:

- **No comments explaining what the code does** — names should do that. Only comment
  the *why*: hidden constraints, subtle invariants, workarounds for specific bugs.
- **No premature abstraction** — three similar lines is better than a helper function
  that exists for hypothetical reuse. Don't design for imagined future requirements.
- **No unnecessary error handling** — only validate at system boundaries (user input,
  external APIs). Trust framework guarantees.
- **No backwards-compatibility shims** — if something is unused, delete it.
- **No new dependencies** without a discovery-first search (see
  [`DEVELOPMENT.md`](DEVELOPMENT.md#adding-a-new-tool) — search MCP Registry, Anthropic
  Skills, GitHub before building).

---

## Test conventions

- One test file per subsystem: `tests/test_<subsystem>.py`
- Milestone features: `tests/test_m<N>_<feature>.py`
- Use `_ScriptRouter` (returns canned `CompletionResult` without HTTP) for all tests
  that construct a `HiveOS` — never make real API calls in unit tests
- Live API smokes go in `tests/test_live_smoke.py` behind `HIVE_LIVE_TEST=1`
- Test names: `test_<what>_<condition>` e.g. `test_diagnoser_skips_unknown_op`
- See [`DEVELOPMENT.md`](DEVELOPMENT.md) for full test patterns

---

## What Hive does vs what Kamil does

| Action | Who |
|---|---|
| Opens draft PRs | Hive (via `SelfModifier.propose` + GitHub REST API) |
| Proposes code edits | Hive (AUTO/REVIEW/MANUAL tier via `spec_search`) |
| Reviews and approves dangerous tool calls | Kamil (via `POST /approvals/decide`) |
| Merges PRs to `main` | Kamil only |
| Edits `Config/SOUL.md` | Kamil only |
| Edits `Core/approval_gate.py` | Kamil only |
| Runs the system 24/7 | Both (systemd units keep Hive alive; Kamil monitors) |

---

## See also

- [`docs/SECURITY.md`](SECURITY.md) — threat model, what the approval gate prevents
- [`docs/DEVELOPMENT.md`](DEVELOPMENT.md) — local setup, architecture rules, test patterns
- [`docs/decisions/003-no-auto-merge.md`](decisions/003-no-auto-merge.md) — why Hive never merges
- [`Config/SOUL.md`](../Config/SOUL.md) — the immutable identity contract (read-only)
