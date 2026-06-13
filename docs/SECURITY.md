# HiveOS — Security Model

HiveOS is a self-modifying autonomous agent that can propose and apply code changes,
execute shell commands, send messages, and restart services. This document describes
the security model: what Hive can do autonomously, what requires human approval, and
what is categorically impossible regardless of instructions.

---

## Threat model summary

| Capability | Autonomous? | Gate | Limitations |
|---|---|---|---|
| Read files | Yes | file_safety denylist | Cannot read PROTECTED paths or credential files |
| Write files | Approval required | `dangerous=True` + approval gate | Cannot write PROTECTED paths |
| Shell commands | Approval required | `dangerous=True` + approval gate | Logged in audit trail |
| Web requests | Yes (GET), approval (POST) | Approval gate for writes | Logged |
| Send Telegram message | Approval required | `dangerous=True` | Only when token is set |
| Restart systemd service | Approval required | `dangerous=True` + target allowlist | Only `gateway`, `orchestrator`, `keeper` |
| Spend money | Approval required | `dangerous=True` | No payment backend; always fails gracefully |
| Self-modify code | See RiskTier below | Tiered gate | Never touches PROTECTED files; never merges |

---

## The three-tier self-modification gate

All code changes Hive proposes go through a deterministic risk tier assigned by
`core/spec_search.py`. The model **cannot self-escalate** its tier — the table is code,
not LLM output.

### AUTO tier — `op: edit_docs`, `add_test`, `update_config`

1. Edit is applied in an **isolated git worktree** (never the live tree)
2. Tests run in the worktree (optionally inside a Docker container with `--network none`)
3. On test failure: worktree is discarded, failure written to memory, no branch pushed
4. On test pass: branch pushed, **draft PR opened via GitHub REST API**
5. **Hive never merges.** The PR sits until Kamil reviews and merges.

### REVIEW tier — `op: patch_code`, `new_tool`, `add_capability`, `refactor`

1. Change is queued in the approval gate (`gate._pending`)
2. The `Edit` object is stored in `HiveOS.edit_pending` (keyed by `approval_id`)
3. Kamil sees it at `GET /approvals` in the dashboard
4. `POST /approvals/decide {approved: true}` routes the edit through `SelfImprovement.apply_approved`
5. If rejected: edit is discarded, `edit_pending` is cleaned up
6. **Process restart caveat:** `edit_pending` is in-memory only. If the process restarts
   between the gate request and the approval decision, the edit is lost and must be
   re-triggered. See [`docs/decisions/005-edit-pending-in-memory.md`](decisions/005-edit-pending-in-memory.md).

### MANUAL tier — `op: manual`

Recorded only. No code is executed. Hive produces a description of what a human should do.

---

## PROTECTED files

`Config/SOUL.md` and `Core/approval_gate.py` are **categorically unmodifiable** by Hive:

- `core/self_mod.py::_touches_protected(changed_paths)` checks every path the worktree
  apply function reports as changed. If any path matches `PROTECTED_PATHS`, the propose
  returns `{"ok": False, "stage": "protected"}` before tests even run.
- `PROTECTED_PATHS` is defined in `Core/approval_gate.py` and loaded read-only via the
  `importlib` bridge in `src/hive/core/approval.py`.
- No API endpoint, tool call, or approval can override this check — it runs in the
  self-modifier, upstream of the approval gate.

---

## Approval gate

The approval gate (`Core/approval_gate.py`) is the danger firewall:

- **Pattern scan:** before any tool call, the executor calls `gate.is_dangerous(tool, args)`
  which regex-scans args for dangerous patterns (shell metacharacters, PROTECTED paths, etc.)
- **Allowlist:** certain tool+arg combinations are always allowed without prompting
- **DANGEROUS_TOOLS:** tools that always require approval regardless of args (e.g. `deploy`, `spend_money`, `external_message`, all MCP tools)
- **Queueing:** dangerous calls are queued in `gate._pending` (dict, keyed by UUID), not executed
- **Constant-time resolution:** `gate.resolve(approval_id, approved)` pops the item atomically

The gate file itself is protected — modifying it would require Kamil to manually merge
the change. No agent path can alter the gate's own logic.

---

## Path traversal protection

The `_diagnoser` in `HiveOS.self_improve_from_symptom` builds `_apply` closures that
write files inside a git worktree. The guard:

```python
wt_root = Path(wt).resolve()
target = (wt_root / _p).resolve()
if not target.is_relative_to(wt_root):
    log.warning("diagnoser: path %r escapes worktree — skipping", _p)
    return []
```

- `Path.is_relative_to` is reliable on Python 3.9+ (both 3.11 and 3.12 are tested)
- Absolute paths (e.g. `/etc/passwd`) are caught: `(wt_root / "/etc/passwd").resolve()` equals
  `/etc/passwd`, which is not relative to `wt_root` → rejected
- `../..` escape paths are caught: fully resolved paths cannot be relative to the worktree root
- Test coverage: `tests/test_m10_self_improve.py::test_diagnoser_apply_closure_rejects_path_traversal`
  exercises the production closure (not a copy) via a patched `SelfModifier.propose`

---

## SSE stream error isolation

The `/chat/stream` SSE endpoint catches all model/tool errors and emits only:

```
event: error
data: TimeoutError
```

`type(exc).__name__` only — never `str(exc)` (which can contain API keys, endpoint URLs,
or other sensitive context). The full exception is logged server-side via `log.warning`.

Test coverage: `tests/test_surfaces.py::test_chat_stream_error_does_not_leak_exception_detail`

---

## Credential security

**Vault file** (`data/credentials.json`):
- Created with `os.chmod(path, 0o600)` — owner-read-only
- Never committed to git (`.gitignore` covers `data/`)
- `credentials.inject()` populates env vars from the vault without overwriting already-set vars
- `credentials.get(key, default)` checks vault first, then `os.getenv`, then default

**API keys in transit:**
- `core/redact.py` masks secrets in the audit log: `sk-...`, `Bearer ...`, `Authorization:`, JWT
  tokens, and vendor-prefix patterns (`MINIMAX_*`, `ANTHROPIC_*`, etc.)
- `observability/audit.py` runs every tool-call arg dict through `redact_text` before storing
- SSE stream errors never include exception message content (see above)

**Multi-key credential pool:**
- `MINIMAX_API_KEY` may be comma-separated for automatic rotation on 429/rate-limit
- `CredentialPool` rotates to the next key on exhaustion, with per-key cooldown tracking
- Keys are never logged or included in telemetry snapshots

---

## Gateway authentication

All endpoints except `GET /health` require:

```
Authorization: Bearer <HIVE_SECRET>
```

`gateway/auth.py::token_ok` uses `hmac.compare_digest` for constant-time comparison,
preventing timing attacks. A missing or empty `HIVE_SECRET` (`"change_me"` default) is
flagged by `hive doctor` as a configuration warning.

The WebSocket endpoint (`/ws`) uses first-frame token exchange instead of HTTP headers.

---

## MCP tool security

All tools loaded from external MCP servers via `HIVE_MCP_SERVERS` are:
- Marked `dangerous=True` automatically (see `tools/mcp/client.py::mcp_tool_to_spec`)
- Subject to the approval gate on every call — they cannot execute without Kamil's approval
- Prefixed with the server name (e.g. `github.search_code`) to distinguish from builtins

The `discover` tool caches search results in memory but performs a mandatory safety audit
before any tool is recommended for adoption. External MCP server code is treated as hostile
until audited.

---

## Docker sandbox (self-mod)

When `HIVE_SANDBOX_IMAGE` is set (e.g. `python:3.12`), AUTO-tier self-mod edits run
candidate tests inside a Docker container:

```
docker run --rm --network none -v /path/to/worktree:/repo:ro -w /repo <image> pytest -q
```

- `--network none` — no outbound network during test execution
- `-v ... :ro` — worktree mounted read-only; the container cannot modify source files
- Container is removed immediately after (`--rm`)

Without `HIVE_SANDBOX_IMAGE`, tests run locally in the worktree. Docker is strongly
recommended for production deployments.

---

## Responsible disclosure

If you discover a security vulnerability in HiveOS, please report it via
[GitHub Security Advisories](https://github.com/hiveosagent/hiveos/security/advisories/new)
rather than opening a public issue.

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (optional)

---

## See also

- [`docs/decisions/003-no-auto-merge.md`](decisions/003-no-auto-merge.md) — why Hive never self-merges
- [`docs/decisions/004-core-is-leaf.md`](decisions/004-core-is-leaf.md) — why the DAG is enforced
- [`docs/decisions/005-edit-pending-in-memory.md`](decisions/005-edit-pending-in-memory.md) — REVIEW-tier edit storage
- [`docs/CONTRIBUTING.md`](CONTRIBUTING.md) — PR rules and what Kamil vs Hive each do
- [`Core/approval_gate.py`](../Core/approval_gate.py) — the live danger firewall (read-only)
- [`Config/SOUL.md`](../Config/SOUL.md) — the immutable identity contract
