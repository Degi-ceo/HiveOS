# HiveOS — Configuration Reference

All configuration is read from environment variables by `HiveConfig.from_env()` in
`src/hive/core/config.py`. The config is a frozen dataclass — no import-time side
effects, no mutation after construction. `HiveOS.build()` calls it once.

Copy `.env.example` to `.env` and edit before starting:

```bash
cp .env.example .env
```

---

## Required minimum (local dev)

| Variable | Example | Notes |
|---|---|---|
| `MINIMAX_API_KEY` | `eyJ...` | MiniMax API key (Token Plan or PAYG) |
| `HIVE_SECRET` | `my-secret-token` | Bearer token for all authenticated gateway endpoints |

With only these two set, `hive ask "hello"` works. Everything else has a working default.

---

## Executor model (`llm/`)

| Variable | Default | Notes |
|---|---|---|
| `HIVE_EXEC_PROVIDER` | `minimax` | `minimax` or `anthropic` — selects which adapter the router uses |
| `HIVE_EXEC_MODEL` | `MiniMax-M3` | Primary model string (passed verbatim to the adapter) |
| `HIVE_EXEC_FALLBACK_MODEL` | `MiniMax-M2.7` | Failover model when primary is rate-limited or errors |
| `HIVE_AUX_MODEL` | `MiniMax-M2.7` | Cheaper model for summarisation, titling, memory consolidation |

### MiniMax executor (default)

| Variable | Default | Notes |
|---|---|---|
| `MINIMAX_API_KEY` | *(required)* | API key; may be comma-separated for multi-key pool (auto-rotates on 429) |
| `MINIMAX_ANTHROPIC_BASE` | `https://api.minimax.io/anthropic` | Anthropic-compatible endpoint for chat completions with interleaved thinking |
| `MINIMAX_OPENAI_BASE` | `https://api.minimax.io/v1` | OpenAI-compatible endpoint (used by Codex planner subprocess) |
| `HIVE_REMAINS_URL` | `https://api.minimax.io/v1/token_plan/remains` | Endpoint polled by the budgeter to self-calibrate the rolling token window |

### Anthropic executor (alternative)

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(required if provider=anthropic)* | Anthropic API key; may be comma-separated |
| `ANTHROPIC_BASE` | `https://api.anthropic.com` | Override for Anthropic-compatible proxies |

---

## Planner (`llm/planner`)

The planner is the **thinking-only** path — it plans but never executes. Uses ChatGPT Plus
via Codex OAuth subprocess. Disabled by default; enable when you need deep architecture work.

| Variable | Default | Notes |
|---|---|---|
| `HIVE_PLANNER_ENABLED` | `false` | Set `true` to enable the thinking model for novel/complex tasks |
| `HIVE_PLANNER_CMD` | `codex exec` | Shell command that invokes the planner (must be on `PATH`) |
| `HIVE_PLANNER_TIMEOUT` | `120` | Seconds before the planner subprocess is killed |

---

## Budget guard (`core/budgeter`)

| Variable | Default | Notes |
|---|---|---|
| `HIVE_DAILY_CALL_CAP` | `3000` | Hard daily call ceiling (counts completions, not tokens) |
| `HIVE_WINDOW_WARN_PCT` | `70` | Warn (but don't block) when token-window usage exceeds this percentage |

The budgeter polls `HIVE_REMAINS_URL` to track the rolling token window and records per-call
cost from `INFERENCE_END` events. Snapshot available at `GET /budget`.

---

## Gateway (`gateway/app`)

| Variable | Default | Notes |
|---|---|---|
| `HIVE_HOST` | `0.0.0.0` | FastAPI bind address |
| `HIVE_PORT` | `8088` | FastAPI bind port |
| `HIVE_SECRET` | `change_me` | Bearer token for all `/chat`, `/budget`, `/approvals`, `/telemetry`, `/audit`, `/tasks`, `/traces` endpoints |

---

## Storage

| Variable | Default | Notes |
|---|---|---|
| `HIVE_DATA_DIR` | `<repo>/data` | Directory for all runtime state (SQLite, audit log, backups) |
| `HIVE_STATE_DB` | `<data_dir>/hive.sqlite` | Shared SQLite database (sessions, memory, tasks, cron, commitments) |

---

## Memory (`memory/`)

| Variable | Default | Notes |
|---|---|---|
| `MNEMOSYNE_HOME` | `<data_dir>/mnemosyne` | Where the Mnemosyne package stores its SQLite databases (when `memory` extra is installed) |
| `MNEMOSYNE_MCP_URL` | *(empty)* | HTTP(S) URL of a remote Mnemosyne MCP SSE server; loaded automatically as an MCP server at gateway startup |
| `OBSIDIAN_VAULT_PATH` | `<repo>/vault` | Root of the Obsidian vault for long-term markdown notes |

**Note:** Without the `memory` extra (`pip install -e ".[memory]"`), HiveOS falls back to
`LocalMemoryProvider` (SQLite, no semantic search). All APIs remain compatible.

---

## Autonomy (`autonomy/`)

| Variable | Default | Notes |
|---|---|---|
| `HIVE_HEARTBEAT_SEC` | `900` | Seconds between heartbeat ticks (15 min default; reduce for testing) |
| `HIVE_MAX_AGENTS` | `3` | Maximum concurrent subagents during task dispatch (concurrency cap) |

---

## GitHub identity

Required for `SelfModifier` to open draft PRs automatically. Without this, Hive pushes
the branch but a human must open the PR manually.

| Variable | Default | Notes |
|---|---|---|
| `HIVE_GITHUB_TOKEN` | *(empty)* | Fine-grained PAT or GitHub App token with `contents:write` and `pull_requests:write` |
| `HIVE_GITHUB_OWNER` | *(empty)* | GitHub username or org that owns the repo (e.g. `hiveosagent`) |
| `HIVE_GITHUB_REPO` | *(empty)* | Repository name (e.g. `hiveos`) |

---

## Telegram surface

Optional. Set to enable the Telegram webhook endpoint and `external_message` tool.

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *(empty)* | BotFather token; also activates `ExternalMessage` tool |
| `TELEGRAM_WEBHOOK_SECRET` | *(empty)* | Validated from `X-Telegram-Bot-Api-Secret-Token` header; leave empty to skip verification (dev) |

---

## Self-mod sandbox

| Variable | Default | Notes |
|---|---|---|
| `HIVE_SANDBOX_IMAGE` | *(empty)* | Docker image for running candidate test suites in isolation (e.g. `python:3.12`). Leave empty to run tests locally. |

When set, any AUTO-tier self-mod edit runs `pytest` inside the container before pushing.
The container gets `--network none` and a read-only worktree bind-mount.

---

## MCP servers

| Variable | Default | Notes |
|---|---|---|
| `HIVE_MCP_SERVERS` | *(empty)* | Semicolon-separated list of MCP server specs loaded at gateway startup |

**Spec formats:**
- `npx -y @modelcontextprotocol/server-github` — stdio command; HiveOS spawns the process
- `https://mnemosyne.example.com/mcp` — HTTP(S) URL with SSE transport
- `MNEMOSYNE_MCP_URL` is loaded automatically in addition to this list

---

## Model pricing overrides

Fine-tune cost accounting for non-standard models or pricing tiers:

| Variable | Example | Notes |
|---|---|---|
| `HIVE_PRICE_<MODEL>_IN` | `HIVE_PRICE_MiniMax-M3_IN=0.3` | Input price in USD per 1M tokens for `<MODEL>` |
| `HIVE_PRICE_<MODEL>_OUT` | `HIVE_PRICE_MiniMax-M3_OUT=1.2` | Output price in USD per 1M tokens for `<MODEL>` |

Replace `/` and `-` in model names with `_` when constructing the env var name.

---

## Credentials vault

In addition to env vars, HiveOS loads secrets from a `0o600` vault file managed by
`core/credentials.py`. Use `credentials.save(key, value)` to write; `credentials.inject()`
is called at build time and populates env vars from the vault without overwriting existing ones.

Vault location: `<data_dir>/credentials.json` (owner-only, `chmod 600`).

This is the recommended way to store API keys on production — edit `.env` only for bootstrap.

---

## Full variable summary

| Variable | Required | Default | Subsystem |
|---|---|---|---|
| `MINIMAX_API_KEY` | ✓ (default provider) | — | llm/minimax |
| `HIVE_SECRET` | ✓ | `change_me` | gateway auth |
| `HIVE_EXEC_PROVIDER` | | `minimax` | llm/router |
| `HIVE_EXEC_MODEL` | | `MiniMax-M3` | llm/router |
| `HIVE_EXEC_FALLBACK_MODEL` | | `MiniMax-M2.7` | llm/failover |
| `HIVE_AUX_MODEL` | | `MiniMax-M2.7` | llm/router (aux) |
| `MINIMAX_ANTHROPIC_BASE` | | `https://api.minimax.io/anthropic` | llm/minimax |
| `MINIMAX_OPENAI_BASE` | | `https://api.minimax.io/v1` | llm/minimax |
| `ANTHROPIC_API_KEY` | ✓ (if provider=anthropic) | — | llm/anthropic |
| `ANTHROPIC_BASE` | | `https://api.anthropic.com` | llm/anthropic |
| `HIVE_PLANNER_ENABLED` | | `false` | llm/planner |
| `HIVE_PLANNER_CMD` | | `codex exec` | llm/planner |
| `HIVE_PLANNER_TIMEOUT` | | `120` | llm/planner |
| `HIVE_REMAINS_URL` | | MiniMax token plan URL | llm/budgeter |
| `HIVE_DAILY_CALL_CAP` | | `3000` | core/budgeter |
| `HIVE_WINDOW_WARN_PCT` | | `70` | core/budgeter |
| `HIVE_HOST` | | `0.0.0.0` | gateway |
| `HIVE_PORT` | | `8088` | gateway |
| `MNEMOSYNE_HOME` | | `<data>/mnemosyne` | memory |
| `MNEMOSYNE_MCP_URL` | | — | memory/mcp |
| `OBSIDIAN_VAULT_PATH` | | `<repo>/vault` | memory/vault |
| `HIVE_DATA_DIR` | | `<repo>/data` | storage |
| `HIVE_STATE_DB` | | `<data>/hive.sqlite` | storage |
| `HIVE_HEARTBEAT_SEC` | | `900` | autonomy |
| `HIVE_MAX_AGENTS` | | `3` | agents/delegate |
| `HIVE_GITHUB_TOKEN` | | — | core/self_mod |
| `HIVE_GITHUB_OWNER` | | — | core/self_mod |
| `HIVE_GITHUB_REPO` | | — | core/self_mod |
| `TELEGRAM_BOT_TOKEN` | | — | gateway/telegram |
| `TELEGRAM_WEBHOOK_SECRET` | | — | gateway/telegram |
| `HIVE_SANDBOX_IMAGE` | | — | core/sandbox |
| `HIVE_MCP_SERVERS` | | — | tools/mcp |
| `HIVE_PRICE_<MODEL>_IN` | | catalog default | llm/pricing |
| `HIVE_PRICE_<MODEL>_OUT` | | catalog default | llm/pricing |
| `HIVE_LIVE_TEST` | | — | tests (smoke only) |
