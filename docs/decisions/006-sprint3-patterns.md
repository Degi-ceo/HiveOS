# 006 — Sprint 3 Architectural Patterns

**Status:** Accepted (implemented in PR #40, branch `claude/system-gaps-completion-6cr5rk`)  
**Date:** 2026-06-18  
**Deciders:** Kamil (owner), Hive (architect)

---

## Context

Sprint 3 was a second deep audit of four reference repositories (OpenJarvis, Hermes, OpenClaw,
Mnemosyne §5) that identified six new gaps (N-1 through N-6) and additional security hardening
items. This record documents the key architectural decisions made during implementation.

---

## Decision 1: SSRF Two-Layer Defence

**Problem:** `WebGet` validated the initial URL but `httpx`'s `follow_redirects=True` allowed
a public server to redirect to a private IP after validation.

**Decision:** Two-layer SSRF defence:
1. `_validate_url()` blocks RFC 1918, loopback, link-local, non-http(s), and URL userinfo
   before the request is dispatched.
2. `_check_redirect()` httpx event hook re-validates every `Location:` header before following
   a redirect, closing the redirect-bypass attack vector.

**Trade-off:** Slightly higher per-redirect overhead; accepted because WebGet is not a
hot path and safety wins.

---

## Decision 2: TerminalOutcome as str Enum

**Problem:** Agent exits returned bare strings ("max turns", "loop guard") — callers couldn't
distinguish exit types without string comparison.

**Decision:** `TerminalOutcome(str, Enum)` with values COMPLETED/MAX_TURNS/LOOP_GUARD/TOOL_ERROR
added to `agents/base.py`. Inheriting from `str` means it serialises cleanly to JSON and can be
compared to string literals. `AgentResult.outcome` defaults to `COMPLETED`.

**Trade-off:** None significant; pure addition.

---

## Decision 3: channel_hint NOT persisted

**Problem:** System prompt needs to know which surface is active (web/cli/telegram/api), but
the SOUL prefix must stay byte-stable for Anthropic prompt-cache hits.

**Decision:** `channel_hint` flows fresh each turn as a parameter through
`HiveOS.ask()` → `ConversationOrchestrator` → `system_prompt()`. It is inserted between
SOUL and memory block but **never stored**. `restore_or_build_system_prompt()` does not
accept it — only the fresh-build path does.

**Trade-off:** Slightly more parameter threading; accepted to preserve cache stability.

---

## Decision 4: DockerShellProvider via injection

**Problem:** `LocalShellProvider` runs shell commands in the host process. No container
isolation for the general `shell` builtin.

**Decision:** `DockerShellProvider` in `tools/shell_provider.py` wraps each command in
`docker run --rm --network none`. Wired via `HiveConfig.shell_provider="docker"` env var,
injected into `register_builtins()` — no import-time Docker dependency (provider chosen at
runtime by the builder).

**Trade-off:** Requires Docker; Docker binary absence is caught by `_m4_shell_provider`
doctor check.

---

## Decision 5: `hive init` wizard

**Problem:** No first-run onboarding; users faced a blank slate with no guidance.

**Decision:** `hive init` interactive wizard in `surfaces/cli.py`: prompts for API key,
auto-generates HIVE_SECRET, sets Mnemosyne path, runs `doctor --fix`, seeds identity
memories. Uses only stdlib `input()` — no heavy TUI deps.

**Trade-off:** None; pure DX improvement.

---

## Consequences

- SSRF redirect bypass fully closed (two-layer defence)
- `TerminalOutcome` is the canonical way to distinguish agent exit types
- System prompt channel context works across all surfaces without cache invalidation
- Shell isolation available via config without code change
- Developers get guided first-run experience

---

## See also

- [`src/hive/tools/builtins/__init__.py`](../../src/hive/tools/builtins/__init__.py) — `_validate_url` + `_check_redirect`
- [`src/hive/agents/base.py`](../../src/hive/agents/base.py) — `TerminalOutcome`
- [`src/hive/context/prompt_builder.py`](../../src/hive/context/prompt_builder.py) — `channel_hint` param
- [`src/hive/tools/shell_provider.py`](../../src/hive/tools/shell_provider.py) — `DockerShellProvider`
- [`src/hive/surfaces/cli.py`](../../src/hive/surfaces/cli.py) — `hive init` wizard
