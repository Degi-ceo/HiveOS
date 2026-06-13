# ADR 002 — MiniMax as the execution model (Anthropic-compatible endpoint)

**Status:** Accepted  
**Date:** 2026-06-13  
**Deciders:** Kamil (owner), Hive (architect)

---

## Context

HiveOS needs an LLM for the agent's execution path: reasoning, tool calls, code generation, and memory synthesis. The requirements are:

1. **Native interleaved thinking** — the model must support extended reasoning (tool call → think → tool call chains without a separate API for thinking).
2. **Anthropic Messages API compatibility** — the codebase targets the Anthropic wire format; switching providers should be one config line, not a rewrite.
3. **Cost model that fits autonomous 24/7 use** — a per-token PAYG model that doesn't spike on bursty autonomy.
4. **Prompt caching** — multi-turn conversations and long system prompts should benefit from prefix caching.
5. **Rate-limit transparency** — the provider must return rate-limit headers so `CredentialPool` can self-calibrate.

The options considered:

| Provider | API | Thinking | Caching | Notes |
|---|---|---|---|---|
| MiniMax (Token Plan) | Anthropic-compatible `/anthropic` | Yes (interleaved) | Yes (`cache_control`) | Credit-based rolling window; PAYG overflow ~$0.30/M in |
| Anthropic direct | Anthropic native | Yes (via extended thinking) | Yes | Per-token PAYG; higher baseline cost |
| OpenAI | OpenAI native | No (o-series is separate) | No (at time of decision) | Different wire format; no native interleaved thinking |
| Local (Ollama) | OpenAI-compatible | Varies by model | No | No GPU on Hetzner VPS; too slow for 24/7 |

---

## Decision

**Use MiniMax via its Anthropic-compatible `/anthropic` endpoint as the primary execution model** (`HIVE_EXEC_MODEL`). MiniMax's Token Plan is credit-based (rolling windows), self-calibrated by `llm/budgeter.py` against `GET /v1/token_plan/remains`. The `MiniMaxAdapter` in `llm/adapters/minimax.py` speaks the Anthropic Messages API wire exactly, including `cache_control` and `x-ratelimit-*` headers.

Anthropic's native API is the **fallback executor** (`HIVE_EXEC_FALLBACK_MODEL`, selected via `HIVE_EXEC_PROVIDER=anthropic`). The `AnthropicAdapter` in `llm/adapters/anthropic.py` uses the same `LLMAdapter` contract — switching is one env var change.

ChatGPT Plus via Codex (`HIVE_PLANNER_ENABLED=true`) is the **planner only** — it thinks, never executes. Heavy architecture / gap-design decisions go through the planner; everything else runs on MiniMax.

---

## Consequences

**Good:**
- MiniMax's Anthropic-compatible endpoint means `MiniMaxAdapter` and `AnthropicAdapter` share the same protocol, HTTP headers, and `cache_control` semantics. The `make_adapter(provider)` registry makes switching a one-liner.
- Interleaved thinking enables tool-call chains that reason between steps without a separate "reasoning" API surface.
- Token Plan credit model avoids surprise per-token invoices during bursty autonomy (heartbeat dispatching multiple subagents).
- `CredentialPool` handles multi-key rotation and cooldowns; the comma-split `MINIMAX_API_KEY=key1,key2` pattern extends to any key count without code changes.

**Bad / trade-offs:**
- MiniMax naming/plan terms change — model strings must be verified against the live MiniMax console after any provider update. Pinned in `.env`, not in code, so the fix is one line.
- MiniMax is less widely known than Anthropic/OpenAI — contributors need to read its docs. The Anthropic-compatible wire format mitigates most surprise.
- Token Plan has server-side rate limits that differ from Anthropic's. `llm/rate_limit.py` captures the `x-ratelimit-*` headers; `CredentialPool` rotates keys on 429.

---

## Alternatives considered

**Anthropic direct as primary:** Higher per-token cost for 24/7 autonomous use. No Token Plan. Still available as fallback (`HIVE_EXEC_PROVIDER=anthropic`).

**OpenAI:** Different wire format would require a third adapter. No native interleaved thinking. Caching semantics differ.

**Local models:** VPS has no GPU. Inference latency is incompatible with real-time chat and autonomous task dispatch.

---

## See also

- [`llm/adapters/minimax.py`](../../src/hive/llm/adapters/minimax.py) — `MiniMaxAdapter`
- [`llm/adapters/anthropic.py`](../../src/hive/llm/adapters/anthropic.py) — `AnthropicAdapter`
- [`llm/adapters/__init__.py`](../../src/hive/llm/adapters/__init__.py) — `make_adapter()` registry
- [`docs/CONFIGURATION.md`](../CONFIGURATION.md) — executor env vars
- [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md#7-model-routing--resilience-llm) — model routing section
