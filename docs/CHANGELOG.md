# HiveOS — Changelog

All milestones since the P0 foundation, newest first.
Each entry links to the PR that delivered it.

> 📌 **Note:** Sprint 7 pillars (1–4) are committed locally on `sprint7/learned-skills`,
> `sprint7/approval-hardening`, and `sprint7/selfmod-safety`. Awaiting human review & merge per
> `CLAUDE.md` rules. Full per-branch breakdown: `RELEASE_NOTES.md`. Root mirror: `CHANGELOG.md`.

---

## [GPT UI improvements concept preview v0.8.5] — IN REVIEW (2026-08-23)

- Added domain-specific canonical layouts for Chat, Skills, Files, Agents, Channels,
  MCP, Logs, Activity, Sessions, Self-improve, Analytics, Docs and Settings. Together
  with Hub, Memory, Tasks and Approvals, all 17 canonical pages now avoid the generic
  placeholder composition.
- Fixed real interaction state: selectable Memory rows and inspector, correctly indexed
  Tasks filtering, selected task feedback, distinct Approvals pending/history content,
  native keyboard-operable Hub tiles and stateful Settings controls.
- Replaced false-pass E2E patterns with exact, fail-closed browser assertions. The suite
  verifies 29 navigation destinations, all 70 tabs, all 93 relationships, actionable
  controls, overlays/history/keyboard behavior, zero backend requests and 145 responsive
  screen checks across five viewports.
- Made screenshot capture portable and deterministic. It imports all hyphenated screen
  IDs directly, clears stale output, captures at CSS 1440×900 (or 390×844) with DPR 2,
  verifies layout/errors/network, hashes every PNG and produces 79 captures plus a ZIP.
- Added an independent `dashboard-preview` GitHub Actions job and reproducible npm scripts.
- Local verification: 111/111 tests, production build, and preview coverage of 99.55%
  statements/lines, 100% functions and 90.83% branches. Chromium E2E and artifact capture
  are mandatory CI gates rather than assumed local passes.

---

## [GPT UI improvements concept preview v0.8.4] — IN REVIEW (2026-08-23)

- **Hub tile-based redesign**: dedicated `HubView` component with primary tile row
  (Gateway/Agents/Memory/Approvals), secondary overview row, tertiary resource row,
  "Needs attention" section, "Active now" grid.
- **Memory low-density view**: dedicated `MemoryView` — two summary tiles only,
  importance badges, calm dt/dd inspector. No graphs or decorative widgets.
- **Tasks Kanban view**: dedicated `TasksView` — status counters bar over 4-column board
  (Backlog/In progress/Review/Done) with status-tinted accents.
- **Approvals safety view**: dedicated `ApprovalsView` — risk banner, risk tones
  (REVIEW=red, MANUAL=amber, PROTECTED=dark-red), safety policy panel.
- **Contrast regression fix**: `--text-3: #9898a0` (6.74:1 on bg, 6.43:1 on surface),
  `--text-4: #aeafb2` (8.80:1 on bg, 8.40:1 on surface) — both pass WCAG AA 4.5:1.
- **Screenshot ZIP artifact**: `HiveOS_UI_v0.8.4.zip` (23.75 MB) — 76 captures at
  HiDPI 2880×1800, 19 screens + 57 tab variants, manifest.json included.
- **Playwright user-journey suite**: `dashboard/e2e-journeys.mjs` — 8 journeys
  (Hub overview, New task overlay, Chat tabs, Memory filters, Tasks routing,
  Command palette Ctrl+K, Mobile nav highlight, Browser history) all pass.
- All 107/107 unit tests pass; production build passes; 29/29 e2e screens pass.

---

## [GPT UI improvements concept preview v0.8.3] — IN REVIEW (2026-08-23)

- Complete CSS redesign with premium dark design system — CSS custom properties for all
  design tokens, ambient amber radial light source, edge vignette, amber-glow selected
  row state with `shadow-amber`, animated `pulse-green` online indicator, `slide-in-notice`
  animation for toasts, custom scrollbar styling.
- "UI PLACEHOLDER" banner changed to "CONCEPT PREVIEW" with dimmer amber and reduced
  opacity — less attention-grabbing.
- Improved sidebar hover/active states with smooth 150ms transitions, active amber glow.
- Metric tiles: refined spacing rhythm, larger bold numerals, `shadow-md` hover lift.
- Row items: hover background, amber selected glow with shadow-amber, refined icon styling.
- Focus styles: amber `outline` with `outline-offset` on all `focus-visible` states.
- `-webkit-font-smoothing: antialiased` for crisp typography on all surfaces.
- Fixture data improvements: richer Chat messages, accurate Files preview, improved
  Memory importance labels.
- All 145 screenshots captured (29 screens × 5 viewports: 1440p, 1280p, 1024p, 768p, 390p).
- All 107/107 tests pass; Vite build passes; preview coverage 100% s/l/f, 97.24% br.

---

## [GPT UI improvements concept preview v0.8.2] — IN REVIEW (2026-08-22)

- Deep-audited all 29 approved standalone mockup states, 70 tab transitions,
  111 primary/row actions and 93 cross-view relationships.
- Added the missing Cron, Commitments, Mobile Hub, Mobile Chat and Mobile Navigation states.
- Fixed deep-link and browser-history synchronization, meaningful tab state, overlay
  close/Escape behavior, command palette and notification triggers, action routing
  and clickable related-view paths.
- Fixed mobile action loss, ambiguous tablet navigation, keyboard tab navigation,
  focus visibility, contextual status tones and dynamic viewport handling.
- Added `docs/UI_AUDIT_2026-08-22.md` with the full finding and verification ledger.
- Added preview coverage enforcement; final dashboard verification is 107/107 tests,
  with 100% preview statements/lines/functions and 97.24% branches.
- The v0.8.1 foundation added an isolated `/?ui-preview=1` fixture-only preview
  for the 17 canonical HiveOS pages and key global UI states.
- Added `docs/UI_RELATIONS_AND_API.md`, verified against the live gateway route table,
  with Implemented / Partial / Gap status for each UI contract.
- Added `docs/UI_MOCKUP_GENERATION_GUIDE.md` with the locked visual prompt and agent workflow.
- Merged the Overview concept into Hub and simplified Memory to two summaries, one list
  and one inspector.
- The production `Centre` remains unchanged unless the explicit preview query flag is used.

---

## [Sprint 7 — Pillars 1/2/3/4: Safety & Autonomy Hardening] — IN PROGRESS (2026-08-22)

See `RELEASE_NOTES.md` (root) for the per-branch breakdown with commit hashes and test counts. Quick index:

- **Pillar 1** — `sprint7/learned-skills` @ `b431c44` — Self-improvement loop audit + 4 real bug fixes
- **Pillar 2** — `sprint7/approval-hardening` @ `c1e4aed` — TTL, kill-switch, audit history, batch approval
- **Pillar 3** — `sprint7/learned-skills` @ `fa193e8` — Pattern detection → skill template → registry
- **Pillar 4** — `sprint7/selfmod-safety` @ `99b63bb` — Pre-flight safety checks (5 checks, tier-escalation policy)

43 new tests, 0 regressions, ruff clean. After-session recovery: `bash scripts/status.sh`.

---

Format: `## [Milestone label] — PR #N (date)`

---

## [System gaps completion — Sprint 1 + Sprint 2] — PR #40 (2026-06-18)

Closed all 11 fixable gaps (G-2–G-12) discovered in the post-build deep audit. 13 GitHub
Issues created (#27–#39) to track them. G-1 (Mnemosyne VPS install) tracked in #27.

**Memory improvements (G-2):**
- `LocalMemoryProvider.system_prompt_block()` now returns top-5 facts by importance score
  instead of static text.
- `HiveMnemosyneProvider.system_prompt_block()` now recalls top-5 identity/fact items via
  BEAM instead of bare counters.

**Delegation (G-3):**
- `DelegateToSpecialist` builtin tool registered; model can call `delegate_named()` from the
  tool loop. Local import (`from hive.agents.delegate import delegate_named` inside `execute()`)
  preserves the DAG.

**Deploy (G-4):**
- `deploy/hiveos-gateway.service` adds `ExecStartPre=` to run `scripts/seed_memories.py`
  on every gateway start (fail-open `|| true`).

**OpenAI-compat endpoint (G-5):**
- `POST /v1/chat/completions` + `GET /v1/models` — Hive acts as a drop-in model provider.
  Supports streaming (SSE) and non-streaming; returns OpenAI `ChatCompletion` format.

**Migration versioning (G-6):**
- `schema_migrations(id, version, applied_at)` table in the state DB; each migration records
  its key after applying. Safe upgrade path for future `ALTER TABLE` ops.

**Security audit in discovery (G-7):**
- `discover()` gains optional `security_delegate: Callable | None` param; each candidate with
  a URL gets annotated with a `security_note` from the delegate.
- `DiscoverTool(enable_security_audit=True)` builds a local-import lambda wrapping
  `delegate_named([task], "security-reviewer")` (DAG-safe).

**Undocumented env vars (G-8):**
- `HIVE_MAX_ITERATIONS`, `HIVE_MAX_PER_TOOL`, `HIVE_SELFMOD_THRESHOLD`, `HIVE_TOOL_TIMEOUT`
  added to `.env.example` and `docs/CONFIGURATION.md`.

**Nginx IP placeholder (G-9):**
- Hardcoded `46.224.161.38` replaced with `YOUR_SERVER_IP` in `deploy/nginx-hiveos.conf`.

**Voice setup docs (G-10):**
- `[voice]` extra completed (`faster-whisper`, `piper-tts`, `sounddevice`); voice setup
  section added to `docs/CONFIGURATION.md`.

**Curator LLM umbrellas (G-11):**
- `Curator` gains `summarize: Summarizer | None` (same type as MemoryKeeper).
- `Curator.consolidate_umbrellas()` (async): groups narrow active/agent-created skills into
  pinned umbrella skills via LLM and archives the source skills. Fail-open.
- `HiveOS.curate_umbrellas()` wrapper + heartbeat calls it after `curate()`.

**CI ruff gate (G-12):**
- `ruff check src/ tests/` added to `.github/workflows/ci.yml` before tests.
- `[tool.ruff]` config added to `pyproject.toml` (`line-length=120`, per-file test ignores).

---

## [Pre-merge review + docs overhaul] — PR #20 (2026-06-13)

**Pre-merge review fixes:**
- Path-traversal test now exercises the production `_apply` closure (was testing a hand copy)
- `conftest.py` resets `_CONFIG` to `None` before each test, not only in teardown
- Merge conflicts resolved with main (A3 host-LLM + M9-transport)

**Documentation overhaul:**
- Added `docs/CONFIGURATION.md` — comprehensive env var reference (33 variables)
- Added `docs/API.md` — full gateway endpoint reference with request/response shapes
- Added `docs/DEVELOPMENT.md` — local dev guide, architecture rules, test patterns
- Added `docs/DEPLOYMENT.md` — production VPS guide with systemd, Mnemosyne, nginx
- Added `docs/CONTRIBUTING.md` — PR workflow, commit conventions, review checklist
- Added `docs/SECURITY.md` — threat model, approval tiers, credential security
- Added `docs/GLOSSARY.md` — ~30 domain-specific terms defined
- Added `docs/CHANGELOG.md` — this file
- Added `docs/decisions/` — 5 architecture decision records
- Updated `README.md` — documentation table, Mermaid diagram, correct extras
- Updated `docs/ARCHITECTURE.md` — test count, method list, gateway endpoints, Mermaid DAG
- Updated `docs/STATUS.md` — test count 364, PR #20 ready for review

---

## [Post-audit fixes round 5: runtime verification] — PR #20 (2026-06-12)

- Dashboard builds clean (`npm ci && npm run build`); gateway serves at `/app`
- `hive --help` / `-h` exits 0 to stdout (standard CLI contract)
- `pyproject.toml`: declared `mcp` and `cron` as optional extras
- `deploy/README.md`: added `pip install -e ".[memory,cron,mcp]"` + dashboard build step
- Added `dashboard/package-lock.json` for reproducible `npm ci` builds

---

## [Post-audit fixes round 4: full-repo re-audit] — PR #20 (2026-06-12)

- Added `tests/test_core_health.py` (12 tests): `core/credentials.py` (vault, 0o600 permissions,
  corrupt-file recovery, inject no-overwrite) + `core/doctor.py` (idempotent migrations, --fix,
  warn-only semantics) — both previously had zero test coverage
- Added `file_safety` symlink-bypass + `_real()` fallback security tests
- Fixed doc path-casing: `docs/reference/` → `docs/references/` in ARCHITECTURE.md and AGENTS.md
- Synced AGENTS.md with CLAUDE.md (`hive chat (REPL)`)

---

## [Post-audit fixes round 3: security hardening] — PR #20 (2026-06-12)

- Path traversal guard in `_diagnoser` `_apply` closure (`is_relative_to(wt_root)`)
- Prompt injection cap: symptom truncated to 2000 chars before LLM call
- SSE error leak fixed: `str(exc)` → `type(exc).__name__` only in stream

---

## [Post-audit fixes rounds 1–2: critical bugs] — PR #20 (2026-06-11)

- `_diagnoser()` was returning `[]` unconditionally (comment placeholder never removed)
  → full JSON→Edit parsing implemented
- REVIEW-tier self-mod approvals could never be applied: `execute_approved` failed for
  `self_mod:` prefix tools → `edit_pending` dict + `/approvals/decide` routing fixed
- `ask_stream()` dropped session history: `build_messages([], ...)` → loads last 40 messages
- `str(RiskTier).upper()` comparison was broken → direct enum membership check
- Gate + `_CONFIG` singletons leaking between tests → `conftest.py` autouse reset fixture
- `deploy/hiveos-keeper.timer` → explicit `Requires`/`After`/`Unit` directives

---

## [M10-d: Specialist sub-agents] — PR #20 (2026-06-11)

- Created `.claude/agents/` with 5 specialist agent definitions:
  `researcher`, `coder`, `reviewer`, `memory-keeper`, `security-reviewer`
- Added `delegate_named(task, agent_name, registry)` to `agents/delegate.py`
- `HiveOS.agents_registry` dict populated at build time via `register_agent()`
- Tests: `tests/test_m10_agents.py`

---

## [M10-c: Self-improvement depth] — PR #20 (2026-06-11)

- Added `TaskBoard.recent_failures(limit)` query to `autonomy/tasks.py`
- Added `HiveOS.self_improve_from_symptom(symptom)` with full JSON→Edit diagnoser
- Heartbeat tick now calls `self_improve_from_symptom` when ≥3 recent failures
- REVIEW/MANUAL tier outcomes enqueued as `self_improve` tasks (visible at `/tasks`)
- Tests: `tests/test_m10_self_improve.py`

---

## [M10-b: Action tools wired] — PR #20 (2026-06-11)

- `ExternalMessage.execute`: sends real Telegram message via `TelegramChannel` when
  `TELEGRAM_BOT_TOKEN` is set; graceful capability-absent stub otherwise
- `Deploy.execute`: calls `systemctl restart hiveos-<target>.service` for
  `gateway`, `orchestrator`, `keeper` (all already behind approval gate)
- `SpendMoney.execute`: honest "no payment backend" stub with adapter slot comment
- Tests: `tests/test_m10_actions.py`

---

## [M10-a: Mission Control visibility] — PR #20 (2026-06-11)

- Added `GET /telemetry` — model/token/cost counters
- Added `GET /traces/{session_id}` — per-session event trace
- Added `GET /audit?limit=N` — recent tool-call audit entries
- Added `GET /tasks` — task board state
- Dashboard panels: MODEL USAGE, RECENT EXECUTIONS, TASK QUEUE
- Tests: `tests/test_m10_observability.py`

---

## [M9-transport: MCP serve-side + SSE client] — PR #22 (2026-06-12, merged)

- `MCPServer.serve_stdio()`: expose Hive's tools to other agents over MCP stdio
- `HiveOS.serve_mcp()`: thin wrapper; `hive mcp-serve` CLI command
- `MCPClient(url=...)`: SSE transport for HTTP(S) MCP servers
- `load_mcp_servers()`: routes `http(s)://` specs to SSE transport
- `MNEMOSYNE_MCP_URL` loaded automatically as a remote MCP server at gateway startup

---

## [A3: Mnemosyne host-LLM bridge] — PR #21 (2026-06-12, merged)

- `HostLLMBridge`: dedicated asyncio event loop + daemon thread; bridges Mnemosyne's
  sync consolidation to HiveOS's async adapter via `run_coroutine_threadsafe`
- `build_mnemosyne_provider(host_llm=...)` wires the bridge at build time
- Mnemosyne consolidation now gets real LLM backing (semantic enrichment)

---

## [M9: mcp-serve + shell abstraction + dashboard SSE] — PR #20 (2026-06-11)

- `hive mcp-serve` CLI command + `HiveOS.mcp_server()` accessor
- `ShellProvider` ABC + `LocalShellProvider`; Shell tool accepts injected provider
- Dashboard chat pane upgraded to use `/chat/stream` SSE endpoint

---

## [M8: Providers — Anthropic + Codex adapters] — PR #18 (merged)

- `LLMAdapter` contract; `make_adapter(provider)` registry
- `AnthropicAdapter`: native Anthropic Messages API
- `CodexAdapter`: subprocess-based Codex planner (hardened: stdin + timeout + fallback)
- `HIVE_EXEC_PROVIDER` selects executor (`minimax` | `anthropic`)

---

## [M-DOCS: Authoritative architecture docs] — PR #15 (merged)

- `docs/ARCHITECTURE.md`: complete system reference, all claims cite real paths
- `docs/STATUS.md`: living capability matrix
- `docs/references/HIVEOS_COMPONENTS.md`: per-module table

---

## [M7: Hardening 2] — PR #17 (merged)

- `core/redact.py`: secret masking in audit trail (env vars, Bearer tokens, JWT, API keys)
- `gateway/protocol.py`: `PROTOCOL_VERSION` additive-first versioning
- `tools/base.py`: `BaseTool.available()` — unavailable tools hidden from model
- `context/title.py`: `HiveOS.title_session()` — aux-model session titling

---

## [M6: Wiring — discovery/MCP/credentials/executor] — PR #16 (merged)

- `tools/discovery.py` registered as `discover` builtin (memory-cached)
- `tools/mcp/client.py`: MCP stdio client + `HiveOS.load_mcp_servers()`
- `core/credentials.py`: 0o600 vault + `credentials.inject()`
- `agents/executor.py`: `AgentExecutor` with per-subagent retry

---

## [M5: Hardening] — PR #13 (merged)

- `tests/test_hardening.py`: delegate/mcp/vault coverage gaps
- `observability/telemetry.py`: cost + token counters
- `observability/traces.py`: per-session event collector
- `core/sandbox.py`: Docker sandbox for self-mod candidate tests
- Fixed systemd units: `hive heartbeat` + `hive consolidate`

---

## [M4: Surfaces] — PR #12 (merged)

- `POST /chat/stream` SSE token streaming (`ask_stream`)
- `gateway/channels/telegram.py`: transport-only Telegram channel + webhook

---

## [M3: Autonomy] — PR #11 (merged)

- `autonomy/tasks.py`: durable SQLite `TaskBoard` (survives restart)
- `autonomy/cron.py`: `CronScheduler` (croniter-optional)
- `autonomy/commitments.py`: `CommitmentBook`
- Heartbeat drives the board

---

## [M2 integration] — PR #10 (merged)

- Self-modifier opens real draft PRs via GitHub REST API
- `Curator` wired into runtime lifecycle

---

## [M2: Self-improvement] — PR #9 (merged)

- `core/spec_search.py`: risk-tiered `SelfImprovement` (AUTO/REVIEW/MANUAL)
- `memory/curator.py`: skill lifecycle (never-delete, pinned-exempt, backup)

---

## [M1: Resilience] — PR #3 (merged)

- Failover taxonomy (`core/spec_search.py::FailoverReason`)
- Multi-key `CredentialPool` with cooldowns
- Rate-limit-aware proactive cooldown
- Per-token cost `Budgeter`
- Hardened Codex planner (stdin + timeout + fallback)
- Opt-in live smokes (`HIVE_LIVE_TEST=1`)

---

## [P0–P9: Foundation] — PR #2 (merged)

- Complete re-architecture: lowercase `src/hive/` package, DAG enforcement
- Composition root: `runtime.py` + `HiveOS.build()`
- All layers: core, llm, agents, memory, context, tools, gateway, autonomy, surfaces, observability
- `hive doctor`, `hive ask`, `hive serve`, `hive chat` CLI commands
- CI: compile check + import smoke + pytest on py3.11/3.12
