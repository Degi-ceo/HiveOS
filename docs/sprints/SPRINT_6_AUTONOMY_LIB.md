# SPRINT 6 — Autonomy & Lib-Class Parity

> **Sprint window:** 2026-06-25 → ongoing (open-ended until all phases merged)
> **Goal:** Promote 8 DEFERRED items (issue #39 D-1, D-2, D-4, D-6, D-7, D-11, D-15,
> D-22) from "explicitly out of scope" to "shipped + 100% covered". After this sprint
> HiveOS is a lib-class agent on par with (or ahead of) Hermes/OpenClaw/OpenJarvis on the
> dimensions that matter: multi-channel, multi-agent, self-improving, observable,
> SDK-compatible, eval-gated.
>
> **Authoritative sprint doc.** All implementation work in this sprint must reference
> a phase below. When this doc and `docs/STATUS.md` disagree, this doc wins until the
> next PR that updates both lands.

---

## 0. Pre-flight ground truth (verified 2026-06-25)

Before opening any issues, I audited the current code. This is the **honest** state:

| D-id | Item | Status today | Re-evaluation rationale |
|---|---|---|---|
| D-22 | OpenAI-compat `/v1/` | ✅ **BUILT** (`gateway/app.py:946-1020`) | Issue #31 closed correctly. No new work — link-only. |
| D-7  | Full tool-loop token streaming | ❌ **MISSING** (orchestrator emits final text only) | Per-iteration SSE events needed for Cursor/Aider/Claude Code UX parity. |
| D-4  | Inbound multi-channel (Slack/Discord/Email) | ✅ **BUILT** (SPRINT_6 P-E, branch `sprint6/multi-channel-inbound`) | Slack HMAC-SHA256, Discord Ed25519, Email RFC822+SMTP — `src/hive/gateway/channels/{slack,discord,email}.py` + 3 webhook endpoints in `gateway/app.py`. |
| D-1  | A2A (agent-to-agent protocol) | 🟡 **PARTIAL** (5 named sub-agents via `delegate_to_specialist`; no envelope/contract) | We already do this — formalize. |
| D-2  | Learning loop (trace→evolve→eval) | ❌ **MISSING** (self_mod records outcomes, no eval comparator) | Real differentiator. Without eval, self_mod is blind. |
| D-15 | Full evals harness | ❌ **MISSING** (only ad-hoc pytest) | Production quality gate. Required before D-2 lands safely. |
| D-11 | Kanban multi-agent board | ❌ **MISSING** (Mission Control shows single-agent tasks) | D-11 depends on D-1. |
| D-6  | AST tool auto-discovery | ❌ **MISSING** (`tools/discovery.py` is web-only) | Discovery-first HARD rule implies introspecting own tools. |
| D-3, D-5, D-8, D-9, D-10, D-12, D-13, D-14, D-16–D-21 | (kept deferred — see issue #39) | — | Stay deferred. Re-evaluate after sprint. |

**Re-evaluation kept 8 / 22 DEFERRED items in scope for this sprint.** The other 14
remain deferred and get re-evaluated post-sprint.

---

## 1. Sprint phases (ordered)

Phases are ordered by **dependency**, not priority. A phase does not start until all
phases that block it are merged to `main`.

```
P-A OpenAI-compat verification          ─┐
P-B Evals harness                        │ foundation (no deps)
P-C Tool-loop streaming (SSE)          ─┤
                                        │
P-D A2A protocol envelope              ─┤ on top of evals + streaming
P-E Multi-channel inbound (Slack/Discord/Email) ─┘
P-F Learning loop (trace→evolve→eval)   ── depends on P-B (evals)
P-G Kanban multi-agent board            ── depends on P-D (A2A)
P-H AST tool auto-discovery             ── depends on P-C (streaming)
```

### Phase A — OpenAI-compat verification

| Field | Value |
|---|---|
| Branch | `sprint6/openai-compat-verify` |
| PR     | (none, or a doc-only PR) |
| Effort | 0 LOC (verification only) |
| Issue  | none — issue #31 already closed |
| Goal   | Confirm `/v1/chat/completions` works with real OpenAI SDK client |

**Acceptance:**
- `curl -H "Authorization: Bearer $HIVE_API_KEY" http://localhost:8000/v1/models` returns OpenAI-shaped response
- `python -c "import openai; c=openai.OpenAI(base_url='http://localhost:8000/v1', api_key=os.environ['HIVE_API_KEY']); print(c.chat.completions.create(model='hive', messages=[{'role':'user','content':'ping'}]).choices[0].message.content)"` returns `pong 🐝`
- Non-streaming + streaming both work end-to-end
- **Result:** P-A is already done. Logged here for traceability.

### Phase B — Evals harness

| Field | Value |
|---|---|
| Branch | `sprint6/evals-harness` |
| Effort | ~400 LOC + 200 LOC tests |
| Issue  | **#70** (open in this sprint) |
| Goal   | Production-grade regression gate that any PR (incl. self-improvements) must pass |

**Scope:**
- New module `evals/` with: dataset loader (JSONL + YAML), grader interface (`exact`, `regex`, `llm-judge`, `tool-trace`), runner (CLI + CI hook), reporter (HTML + JUnit XML).
- Wire `pytest tests/` as pre-merge gate already exists — extend to `hive eval run evals/datasets/*.jsonl`.
- Ship one demo dataset: `evals/datasets/golden_qa.jsonl` (30 hand-curated Q/A pairs covering SOUL, tools, self-mod refusals).
- Wire into `gateway/app.py` as `POST /eval/run` (gated) and `hive eval` CLI command.

**Acceptance:**
- 30/30 dataset items pass on a clean main
- Failure of any item exits 1 in CI
- HTML report uploaded as artifact on GitHub Actions
- 100% coverage on `evals/` package
- One integration test proves a failing eval blocks merge via the existing CI workflow

### Phase C — Tool-loop streaming (SSE)

| Field | Value |
|---|---|
| Branch | `sprint6/tool-loop-stream` |
| Effort | ~200 LOC + 100 LOC tests |
| Issue  | **#71** |
| Goal   | Stream each tool-loop iteration as SSE event so clients (Cursor/Aider/dashboard) see live progress |

**Scope:**
- New `Orchestrator.stream_ask()` async generator yielding event dicts: `{"type":"tool_call","name":...,"args":...}`, `{"type":"tool_result","name":...,"output":...}`, `{"type":"final","text":...}`.
- New gateway endpoint `POST /chat/stream/iterations` returning `text/event-stream` of those events.
- `/v1/chat/completions` gains an extended SSE variant: when client sends `stream=true` AND `x-hive-iterations=true`, emit tool-loop events too. Backward-compatible (default off).
- Dashboard "Mission Control" gains a live iteration log panel.

**Acceptance:**
- A real tool-calling conversation (e.g. "deploy to staging") shows 4+ SSE events live in `curl -N`
- Existing `/chat/stream` and `/v1/chat/completions` paths unchanged (no regressions)
- 100% coverage on the new generator + new endpoint
- One regression test proves the old SSE format still works

### Phase D — A2A protocol envelope

| Field | Value |
|---|---|
| Branch | `sprint6/a2a-envelope` |
| Effort | ~350 LOC + 200 LOC tests |
| Issue  | **#72** |
| Goal   | Formalize the 5 named sub-agents behind a JSON-RPC-style envelope; future-proof external agent connection |

**Scope:**
- New `agents/a2a/` package: `envelope.py` (request/response schemas), `router.py` (routes envelopes to local sub-agents or remote `A2A_URI`), `client.py` (HTTP/JSON-RPC client).
- `delegate_to_specialist` refactored to emit/consume A2A envelopes internally.
- New gateway endpoint `POST /a2a/rpc` (auth-gated) that speaks the envelope.
- `agents/registry.py` exposes named agents via A2A contract (id, capabilities, schema).
- **Internal only** — remote-agent bridge (D-1's "requires external agents" rebuttal) deferred to next sprint; P-D stops at the contract.

**Acceptance:**
- A local sub-agent round-trip (`a2a.call("researcher", task="...")` → result) works with the new envelope
- A mock HTTP server simulating a remote agent is accepted by `A2AClient` (with timeout + retry)
- 100% coverage on `agents/a2a/`
- Existing `delegate_to_specialist` callers see no behavior change (snapshot test)

### Phase E — Inbound multi-channel (Slack / Discord / Email)

| Field | Value |
|---|---|
| Branch | `sprint6/multi-channel-inbound` |
| Effort | ~250 LOC + 150 LOC tests |
| Issue  | **#73** |
| Goal   | Make HiveOS reachable from any messaging surface — true Hermes/OpenClaw parity |

**Scope:**
- New `gateway/channels/slack.py`, `discord.py`, `email.py` (IMAP inbound or SendGrid Inbound Parse webhook).
- Each implements the same `Channel` interface already used by Telegram: `start()`, `stop()`, `on_message()`.
- Wire channel startup into `HiveOS.build()` (gated on `HIVE_SLACK_WEBHOOK`, `HIVE_DISCORD_WEBHOOK`, `HIVE_SMTP_HOST` presence).
- Reply path uses existing `TelegramChannel.send()` outbound → all 4 channels share `gateway.app.send_external()`.
- Dashboard "Mission Control" shows channel status pills (green/red/disabled).

**Acceptance:**
- A real Slack message posted to the webhook appears in `Mission Control` → `/chat` as a user turn with `[Active surface: slack]` hint
- Same for Discord (incoming webhook via `https://discord.com/api/webhooks/{id}/{token}`) and Email (SendGrid Inbound Parse multipart form)
- Each channel has 100% coverage + a live smoke (`scripts/smokes/channel_*.py`)
- One negative test: webhook missing secret → 401, never processed

**Status (PR pending):**
- [x] 3 new channels + 3 new gateway endpoints (Slack HMAC-SHA256, Discord Ed25519, Email RFC822+SMTP)
- [x] 100% coverage on `src/hive/gateway/channels/{slack,discord,email}.py` (239 statements)
- [x] 70 new tests in `tests/test_channels_multi.py`
- [x] `scripts/smokes/channels_multi.py` — 6/6 checks pass
- [x] `ruff check src/ tests/` clean
- [x] Full suite remains green (3729 → 3799)

### Phase F — Learning loop (trace → evolve → eval)

| Field | Value |
|---|---|
| Branch | `sprint6/learning-loop` |
| Effort | ~600 LOC + 300 LOC tests |
| Issue  | **#74** |
| Depends on | P-B (evals harness) |
| Goal   | Self-improvement that **proves** it's an improvement. Real differentiator. |

**Scope:**
- New `core/learning/` package: `tracer.py` (record all tool-call outcomes per session), `evolver.py` (propose code edits via existing `self_mod` path), `evaluator.py` (run evals before/after, compare, accept/reject).
- `LearningLoop.run(symptom)` = `tracer.collect() → evolver.propose() → evaluator.before_after() → self_mod.apply_or_revert()`.
- The existing `heartbeat.tick()` self-improve path (≥3 failures) becomes a thin wrapper around `LearningLoop.run()`.
- New `/learning/history` gateway endpoint showing the last N loops with their accept/reject verdicts.

**Acceptance:**
- A simulated regression (manually break a tool, observe self-mod propose fix, verify evals catch it, verify reject if regression)
- The loop **never** accepts a change that fails evals (golden_qa must stay 30/30)
- `evals/history.jsonl` records each loop with outcome
- 100% coverage on `core/learning/`
- One integration test uses a frozen `tracer` trace + canned `evolver` proposal

### Phase G — Kanban multi-agent board

| Field | Value |
|---|---|
| Branch | `sprint6/kanban-board` |
| Effort | ~250 LOC + 150 LOC tests |
| Issue  | **#75** |
| Depends on | P-D (A2A) |
| Goal   | Mission Control shows all 5 named sub-agents as columns with live task cards |

**Scope:**
- New dashboard page `Mission Control → Agents` with 5 columns (researcher, coder, reviewer, memory-keeper, security-reviewer).
- WebSocket subscription to A2A envelope events (`a2a.call.started`, `a2a.call.completed`).
- Card content: agent name, task description, status (queued/running/done/failed), elapsed time, tool calls summary.
- Click card → opens trace overlay (`/traces/{session_id}` scoped to that A2A call).

**Acceptance:**
- Triggering a multi-step task ("refactor module X") produces visible cards moving across columns in real time
- Each card has working drill-down to its trace
- 100% coverage on the new dashboard component + WebSocket handler
- One Playwright smoke captures the 5-column layout

### Phase H — AST tool auto-discovery

| Field | Value |
|---|---|
| Branch | `sprint6/ast-tool-discovery` |
| Effort | ~200 LOC + 150 LOC tests |
| Issue  | **#76** |
| Depends on | P-C (streaming) — uses streamed introspection to surface tool schemas |
| Goal   | Self-introspection of `tools/builtins/` + `tools/mcp/*` so Hive can answer "what can I do?" from its own state, not external docs |

**Scope:**
- New `tools/introspect.py` walks `tools/builtins/` and `tools/mcp/`, AST-parses each module, extracts `BaseTool` subclasses, builds a structured index.
- `discover` builtin augmented: `discover(intent="what tools do you have for X")` uses the AST index for retrieval-augmented matching, falls back to web search only when local index scores below threshold.
- `hive ask "what tools do you have for deploying?"` returns a concrete list pulled from the AST index, with `source: ast` attribution.

**Acceptance:**
- `python -c "from hive.tools.introspect import index; print(len(index()))"` returns ≥30 (current tool count)
- `discover` query "github pr list" returns `GitHubListPRs` with score > 0.8 from local AST (no web hit needed)
- 100% coverage on `tools/introspect.py`
- One negative test: malformed tool module is skipped with a logged warning, not crashed

---

### Phase I — Jarvis Front (FINAL — the daily-driver centre)

| Field | Value |
|---|---|
| Branch | `sprint6/jarvis-front` |
| Effort | ~900 LOC (frontend) + 250 LOC tests + 50 LOC backend gateway endpoints |
| Issue  | **#77** |
| Depends on | P-G (Kanban → live activity feed), P-C (streaming → live chat), all backend endpoints exist already |
| Goal   | The **last** development phase. HiveOS's permanent daily-driver UI — a premium, Jarvis-grade command centre that is *the* way Kamil interacts with his agent. After this phase, the system is feature-complete at the lib-class bar set by SPRINT 6. |

**Why this phase exists:** the current `dashboard/MissionControl.jsx` is functional but utilitarian — panels stacked in a grid, plain borders, no personality. The user said "aby zrobic centeum jego jak jarvis like proper main centree front for me" — they want a *centre*, a home, a place where Hive lives and you talk to it. This is the visual + UX culmination of SPRINT 6.

**Scope (frontend — `dashboard/src/`):**

1. **`Centre.jsx`** — root page replacing `MissionControl.jsx`. Three-zone layout:
   - **Top bar (h=56px):** `STATUS ORB` (animated: idle/working/error) · brand "HIVE" · surface pills (Telegram/Slack/Discord/Email green/red/grey) · voice-toggle button · settings cog
   - **Left rail (w=280px, collapsible):** `SKILLS` panel + `AGENTS` panel (5 named sub-agents with live status, depends on P-G Kanban events)
   - **Centre stage:** `CHAT` — large, immersive conversation with full-height scrollback, animated tool-call insertions (depends on P-C per-iteration SSE), Markdown rendering, voice waveform when recording
   - **Right rail (w=320px, collapsible):** `MEMORY PEEK` (recent memories), `TOOL TRACE` (last 10 tool calls with status), `SELF-IMPROVEMENT FEED` (last 5 self-improvement attempts with verdicts)
   - **Bottom strip:** approval toast (slides up when an approval is pending)

2. **`components/`** — split the monolith:
   - `StatusOrb.jsx` — animated SVG orb reflecting Hive's state (idle=cyan pulse, thinking=amber spin, error=red flash)
   - `ChatCenter.jsx` — message bubbles, tool-call chips, markdown render, voice button
   - `ActivityFeed.jsx` — real-time tool-call log via WebSocket
   - `MemoryPeek.jsx` — collapsible panel with `/memory/important` + `/memory/stats`
   - `SkillLauncher.jsx` — pinned skills grid, click → `/chat` with the skill prompt
   - `SurfaceBar.jsx` — channel status pills using `/health/summary`
   - `ApprovalModal.jsx` — modal dialog with full arg/reason display
   - `VoiceToggle.jsx` — wake-word status + start/stop listening button (uses voice surface if `HIVE_VOICE_ENABLED=true`)
   - `SelfImprovementFeed.jsx` — last 5 self-improve attempts via `/self-improve/history`

3. **`hooks/`** — WebSocket + polling abstracted:
   - `useWebSocket.js` — connects to `/ws/dashboard`, auto-reconnect, exponential backoff
   - `useGateway.js` — wraps `fetch` with token + error handling
   - `useVoice.js` — microphone permission + waveform sampling

4. **`styles/theme.css`** — premium design tokens (no Tailwind; pure CSS variables):
   - Palette: `--ink: #05080a`, `--hud: #0a1410`, `--neon-cyan: #39ff14`, `--neon-amber: #ff9f0a`, `--neon-rose: #ff3b30`, `--neon-violet: #c77dff`, `--text-dim: #4a6a5a`
   - Glass morphism: `backdrop-filter: blur(12px)`, `rgba(8, 17, 13, 0.85)` panels
   - Mono: `'JetBrains Mono', 'SF Mono', monospace`
   - Animations: pulse, scan-line, glow
   - Responsive: mobile breakpoints at 768px and 480px

5. **`__tests__/`** — Vitest tests for components + hooks

**Scope (backend — minimal, only what's missing):**
- New `GET /health/summary` enhancement: add per-channel online status for `SurfaceBar` (already returns ok/degraded — needs fields like `telegram_connected`, `slack_connected`)
- Verify `/ws/dashboard` broadcasts `a2a.call.*` events (depends on P-D)
- No new endpoints required otherwise — everything else exists

**Acceptance:**
- `npm run build` produces `dist/index.html` + bundled JS, no errors
- New `dashboard/src/Centre.jsx` mounts cleanly; old `MissionControl.jsx` removed or kept as legacy
- Manual test: typing in chat streams tokens live; tool calls appear as chips inline; memory peek shows real entries; approval modal blocks the chat until decided
- Vitest suite green; 100% coverage on `dashboard/src/hooks/` and 80%+ on `dashboard/src/components/`
- Visual: dark sci-fi theme, animated orb, glass panels, no jank on a fresh load
- `vite build` output <500KB gzipped
- Existing CI green (ruff + pytest)
- `hive doctor` green (no regressions on backend)

**Out of scope (deliberate non-goals):**
- Mobile-native apps (PWA later if requested)
- Multi-tenant UI (single-user)
- Drag-and-drop layout customization (fixed 3-zone)
- Plugin/theme marketplace (single theme)

**Sprint-completion milestone:** when P-I merges, HiveOS v1.0 ships. Future work is incremental refinement, not foundational.

---

### Phase J — CLI modernization + onboarding wizard (Hermes/OpenClaw parity)

| Field | Value |
|---|---|
| Branches | `sprint6/cli-j1-style` … `sprint6/cli-j8-groups-update` (8 PRs) |
| Effort | ~2 300 LOC prod + ~1 700 LOC tests + ~350 LOC docs = ~4 350 LOC total |
| Issue  | **#78** |
| Depends on | none (independent of P-I); consumes P-B (evals), P-H (AST introspect), P-G (Kanban) outputs |
| Goal   | `hive` becomes a premium, Jarvis-grade command surface — modern REPL, rich output, real onboarding wizard, discoverability. Backward-compatible with all 13 existing commands. |

**Why this phase exists:** current `surfaces/cli.py` (529 LOC, 13 commands) is functional but dated — no command groups, no `--json`, no themes, no real wizard, no autocomplete. Kamil asked: "dodaj tez ulepszenie onboarding i cli frontu dla hive bo jest starszny. tak aby miec podobnie jak openclaw i hermes itp. latwe komendy, onboardings itp itp". This phase delivers the modern CLI surface Hermes/OpenClaw users expect.

**Scope:**

PR breakdown (8 PRs, ~400 LOC each):

| PR | Branch | Scope | LOC |
|---|---|---|---|
| J1 | `sprint6/cli-j1-style` | ANSI tokens + 3 themes (neon/amber/minimal) + panel/table/sparkline renderers | ~280 + 250 tests |
| J2 | `sprint6/cli-j2-parser` | argparse factory + Output singleton (`--json`/`--quiet`/`--theme`/`--no-color`) + command registry + compat shim | ~350 + 250 tests |
| J3 | `sprint6/cli-j3-help-completion` | Rich `--help` (boxed, cyan command names in tty) + `hive completion {bash,zsh,fish}` + aliases (`h`/`q`/`s`/`v`/`ls`/`m`/`e`/`d`) | ~220 + 200 tests |
| J4 | `sprint6/cli-j4-onboarding` | Full 10-step `hive init` wizard (workspace → provider → creds → secret hardening → memory → channels → doctor → seed → done). `--non-interactive` mode for CI. | ~280 + 250 tests |
| J5 | `sprint6/cli-j5-rich-status` | Premium `hive status` (panels + 14-day sparkline + channel pills) + `budget` + `version` + `logs --follow` | ~480 + 350 tests |
| J6 | `sprint6/cli-j6-repl` | REPL overhaul: 16 slash commands (`/help /status /clear /compact /resume /mcp /tools /memory /theme /model /whoami /approvals /budget /doctor /quit`), readline history, Ctrl-C/D/L, statusbar, spinner | ~420 + 280 tests |
| J7 | `sprint6/cli-j7-formats` | Every command honors `--json`/`--quiet`; runtime theme switch (`/theme` + `set_theme()`); `--no-color` propagates | ~180 + 200 tests |
| J8 | `sprint6/cli-j8-groups-update` | New groups: `hive tools list/describe/search`, `hive memory search/show/stats`, `hive eval run/list` + lazy PyPI update check | ~280 + 220 tests |

**Backward compatibility (HARD constraint):**
- All 13 existing commands keep working unchanged
- `~/.local/bin/hive` wrapper unchanged
- All `test_surfaces.py` cases pass without modification (old symbols re-exported via `cli_old.py` compat shim)
- `pyproject.toml` entry point unchanged (`hive = "hive.surfaces.cli:main"`)
- `NO_COLOR` env respected (already is)

**Acceptance:**
- `hive --help` renders rich boxed help in tty; plain text when piped
- `hive completion bash|zsh|fish` produces valid scripts for all three shells
- `hive init` walks 10-step wizard end-to-end; `hive init --non-interactive --json` works in CI
- `hive status` renders panels + sparkline + channel pills; `--json` returns parseable JSON
- `hive chat` REPL has all 16 slash commands; old 4 (`/help /status /clear /quit`) byte-identical to current
- Readline history persists at `~/.hive/history`
- 13 old commands + old flags (`--tail`, `--fix`) keep working — verified by existing `test_surfaces.py`
- ~240 new tests, 100% coverage on every new module
- 2 new docs: `docs/CLI_REFERENCE.md`, `docs/ONBOARDING.md`
- `ruff check` + `pytest -q` green
- No changes to `Config/SOUL.md`, `core/approval_gate.py`, `core/config.py`
- No new deps in `pyproject.toml`

**Out of scope:**
- TUI/ncurses (curses-level interactivity — defer)
- Mouse support (terminal-only)
- Plugin system for custom commands (defer to Sprint 7)

---

---

## 2. Branching & PR conventions

```
Branch pattern: sprint6/<phase>-<short-name>
PR title pattern: feat(<scope>): <Phase letter> <short name> (SPRINT_6)
PR body template: see .github/PULL_REQUEST_TEMPLATE/sprint6.md (create in P-B)
Commit format: conventional commits (feat:, fix:, test:, docs:, refactor:)
```

Every PR in this sprint MUST:
1. Reference the issue number in the PR body (`Closes #71` etc.)
2. Include `SPRINT_6` in the PR title so the GitHub Project board can group them
3. Pass the existing CI (ruff + pytest) AND the new evals gate (after P-B lands)
4. Maintain 100% coverage on the touched module(s)
5. Update `docs/STATUS.md` capability table in the same PR

---

## 3. Definition of Done — sprint level

- [ ] All 9 phases merged to `main` via PRs (or explicitly descoped with Kamil's sign-off)
- [ ] Issue #39 updated: 8 items moved from `Deferred` to `Shipped` with PR links
- [ ] `docs/STATUS.md` reflects every new capability in the BUILT+WIRED table
- [ ] Test suite size: +800 to +1200 tests (backend) + ~30 Vitest tests (frontend, P-I)
- [ ] `hive doctor` reports new capabilities under "discovered surfaces"
- [ ] `hive eval` exits 0 on `golden_qa.jsonl` from a clean clone
- [ ] OpenAI SDK `client.chat.completions.create(...)` works against running gateway (regression-checked in CI smoke)
- [ ] Dashboard `Centre` page is the default landing; old `MissionControl.jsx` removed
- [ ] No `Config/SOUL.md` or `core/approval_gate.py` modifications (immutable, per CLAUDE.md)
- [ ] No direct merges to `main` (always branch → PR → human merge, per CLAUDE.md)
- [ ] **v1.0 ship:** all P-I acceptance criteria pass; HiveOS is feature-complete at lib-class bar

---

## 4. Risk register

| Risk | Mitigation |
|---|---|
| Evals (P-B) blocks P-F hard | Land P-B first, sprint skeleton enforces order |
| Slack/Discord/Email APIs drift | Pin SDK versions, test against recorded fixtures + one live smoke per release |
| AST introspect breaks on dynamic tools | Heuristic: only static `BaseTool` subclasses count; dynamic tools opt-in via `register_dynamic()` |
| SSE backpressure on tool-loop | `StreamingResponse` with `X-Accel-Buffering: no` already proven on /v1/; reuse pattern |
| A2A contract over-engineering | Stop at internal envelope + mock remote; no real external agent support this sprint |
| Kanban overwhelms dashboard | Limit to 5 named agents; per-agent column is hardcoded; no dynamic agent spawning UI yet |
| Self-improvement loop regresses itself | Evaluator runs BEFORE apply; any eval failure → revert + record |
| Coverage sprint arms race | After P-B lands, all new code lands at 100%; don't accumulate debt |

---

## 5. Out of scope this sprint (kept deferred)

D-3 (trajectory compressor), D-5 (Tauri shell), D-8 (hardware auto-detect), D-9
(DAG engine — but P-F implicitly handles eval flow), D-10 (recipes TOML), D-12
(multi-profile), D-13 (central command registry — already adequate), D-14
(connectors beyond Vault+Obsidian — selective 5 added in next sprint), D-16 (energy
telemetry), D-17 (NL cron), D-18 (insights — already covered by
`keeper.consolidate`), D-19 (Rust/PyO3), D-20 (PostHog), D-21 (LoopGuard Rust).

Plus D-23 to D-28 from issue #39 second audit (ACP, plugin system, agent-loop
hooks, security audit engine, bench stats, Mnemosyne import CLI exposure).

---

## 6. Communication cadence

- After each phase lands: comment on the issue, link the PR, update the phase status here
- Daily: rebuild this doc's phase checklist; close phases in real time
- Sprint end: post a `SPRINT_6_RETRO.md` in this dir with wins / misses / next-sprint carry

---

## 7. Phase status (live checklist)

- [x] **P-A** OpenAI-compat verification — DONE (issue #31 closed correctly)
- [ ] **P-B** Evals harness — issue #70, branch `sprint6/evals-harness`
- [ ] **P-C** Tool-loop streaming — issue #71, branch `sprint6/tool-loop-stream`
- [ ] **P-D** A2A protocol envelope — issue #72, branch `sprint6/a2a-envelope`
- [x] **P-E** Multi-channel inbound — issue #73, branch `sprint6/multi-channel-inbound`
- [ ] **P-F** Learning loop — issue #74, branch `sprint6/learning-loop`
- [ ] **P-G** Kanban board — issue #75, branch `sprint6/kanban-board`
- [ ] **P-H** AST tool discovery — issue #76, branch `sprint6/ast-tool-discovery`
- [ ] **P-I** Jarvis Front (FINAL — daily-driver centre) — issue #77, branch `sprint6/jarvis-front`
- [x] **P-J (J1+J2 foundation)** CLI themes + parser + Output + registry — issue #78, branch `sprint6/cli-foundation`, PR pending
  - J1 themes (neon/minimal/mono) + Output singleton landed; J2 parser + registry landed; J3-J8 deferred to future waves

**When P-I merges:** SPRINT 6 closes. HiveOS v1.0 ships. Future work = incremental refinement.

---