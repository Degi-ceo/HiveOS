"""
runtime.py — HiveOS, the assembled runtime (composition root).

HiveOS = "hive operating system": the system that hosts the agent **Hive**. One
dataclass holds every wired subsystem; `HiveOS.build()` constructs them from the
typed config and injects the dependencies — clean DI without a container (pattern
from OpenJarvis system/{core,builder}.py, SYNTHESIS Part B; named to our identity
model — Hive is the agent, HiveOS is the system).

Placement note (deliberate refinement of the plan's `core/system.py` path): the
composition root imports every layer (llm/tools/memory/context/agents), so it CANNOT
live in `core` without breaking the core-is-a-leaf invariant the architecture test
enforces and that SYNTHESIS's own DAG mandates. It lives at the top level (peer of
the layers it wires); the DAG already treats this as what gateway/autonomy/surfaces
depend on.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from hive.agents.orchestrator import ConversationOrchestrator
from hive.agents.planner import Planner
from hive.autonomy.commitments import CommitmentBook
from hive.autonomy.cron import CronScheduler
from hive.autonomy.tasks import TaskBoard
from hive.context.session_store import SessionStore
from hive.core.budgeter import Budgeter
from hive.core import credentials
from hive.core.config import HiveConfig, set_config
from hive.core.events import EventBus, EventType
from hive.core.sandbox import make_sandbox_runner
from hive.core.self_mod import SelfModifier, github_pr_opener
from hive.core.spec_search import Edit, EditOutcome, SelfImprovement
from hive.core.types import Message
from hive.llm.adapters import make_adapter
from hive.llm.credential_pool import CredentialPool
from hive.llm.model_catalog import ModelCatalog
from hive.llm.router import ModelRouter, TaskKind
from hive.memory.curator import Curator
from hive.memory.keeper import MemoryKeeper
from hive.memory.local import LocalMemoryProvider
from hive.memory.mnemosyne_provider import build_mnemosyne_provider
from hive.memory.provider import MemoryProvider
from hive.memory.skill_usage import SkillUsageStore
from hive.memory.vault import ObsidianVault
from hive.observability.audit import AuditLog
from hive.observability.telemetry import Telemetry
from hive.observability.traces import TraceCollector
from hive.tools.base import BaseTool
from hive.tools.builtins import register_builtins
from hive.tools.executor import ToolExecutor
from hive.tools.registry import ToolRegistry

log = logging.getLogger("hive.runtime")


@dataclass(slots=True)
class HiveOS:
    config: HiveConfig
    events: EventBus
    router: ModelRouter
    tools: dict[str, BaseTool]
    tool_executor: ToolExecutor
    memory: MemoryProvider
    session_store: SessionStore
    keeper: MemoryKeeper
    planner: Planner
    orchestrator: ConversationOrchestrator
    budgeter: Budgeter
    telemetry: Telemetry
    traces: TraceCollector
    audit_log: AuditLog
    skill_usage: SkillUsageStore
    curator: Curator
    self_modifier: SelfModifier
    improver: SelfImprovement
    task_board: TaskBoard
    cron: CronScheduler
    commitments: CommitmentBook
    agents_registry: dict  # name → AgentFactory; populated at build time
    edit_pending: dict    # approval_id → Edit; REVIEW-tier edits awaiting human approval

    async def ask(self, message: str, *, session_id: str = "default") -> str:
        """End-to-end turn; returns the final assistant text."""
        result = await self.orchestrator.ask(message, session_id=session_id)
        return result.content

    async def consolidate(self, session_id: str = "default") -> int:
        return await self.keeper.consolidate(session_id)

    async def title_session(self, session_id: str = "default") -> str | None:
        """Generate + store a short title from the session's first message (B3).
        Out-of-band (not in the hot turn path); idempotent; best-effort."""
        existing = self.session_store.get_title(session_id)
        if existing:
            return existing
        msgs = self.session_store.messages(session_id, limit=1)
        if not msgs:
            return None
        from hive.context.title import generate_title

        async def _summarize(m: list[Message], system: str) -> str:
            r = await self.router.complete(m, kind=TaskKind.AUX, system=system,
                                           thinking=False, max_tokens=64)
            return r.text

        title = await generate_title(msgs[0].content, _summarize)
        self.session_store.set_title(session_id, title)
        return title

    async def ask_stream(self, message: str, *, session_id: str = "default"):
        """Stream a conversational reply token-by-token (SSE surface, M4 #sf-1).

        Direct model stream (SOUL + memory recall as context) — NOT the agentic tool
        loop, which stays on ask(). Persists the completed turn to the session store +
        memory after the stream finishes."""
        from hive.context.prompt_builder import build_messages, system_prompt

        mem_block = self.memory.system_prompt_block() if self.memory else ""
        recall = self.memory.prefetch(message, session_id=session_id) if self.memory else ""
        history = self.session_store.messages(session_id, limit=40) if self.session_store else []
        messages = build_messages(history, message, recall_block=recall)
        chunks: list[str] = []
        async for delta in self.router.stream(messages, system=system_prompt(mem_block)):
            chunks.append(delta)
            yield delta
        final = "".join(chunks)
        # Persist the turn (best-effort; never break a delivered stream).
        try:
            from hive.core.types import Role
            self.session_store.append(session_id, Role.USER, message)
            self.session_store.append(session_id, Role.ASSISTANT, final)
            self.memory.sync_turn(message, final, session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("ask_stream persist failed: %s", exc)

    def curate(self) -> dict:
        """Run the skill-lifecycle Curator (deterministic, safe). No-op until
        agent-created skills exist; built-in tools are exempt (registered as bundled)."""
        return self.curator.run()

    async def discover(self, need: str) -> dict:
        """Discovery-first (HARD SOUL rule): search official sources for an existing
        solution before building; cached via memory when supported (A1)."""
        from hive.tools import discovery
        mem = self.memory if (hasattr(self.memory, "recall")
                              and hasattr(self.memory, "learn")) else None
        return await discovery.discover(need, memory=mem,
                                        github_token=self.config.github_token)

    async def load_mcp_servers(self) -> int:
        """Connect configured stdio MCP servers (HIVE_MCP_SERVERS) and register their
        tools into the live registry. Best-effort, per-server isolated (A2). Returns
        the number of tools loaded. Called at gateway/heartbeat startup."""
        import shlex
        from hive.tools.mcp.client import MCPClient

        loaded = 0
        for spec in self.config.mcp_servers:
            parts = shlex.split(spec)
            if not parts:
                continue
            client = MCPClient(parts[0], parts[1:])
            try:
                await client.connect()
                descriptors = await client.list_tools()
                for tool in client.as_tools(descriptors, prefix=f"{parts[0]}."):
                    self.tools[tool.spec.name] = tool
                    self.tool_executor.add_tool(tool)
                    loaded += 1
            except Exception as exc:  # noqa: BLE001 - one bad server must not block startup
                log.warning("MCP server %r failed to load: %s", spec, exc)
        if loaded:
            log.info("loaded %d MCP tool(s) from %d server(s)", loaded,
                     len(self.config.mcp_servers))
        return loaded

    async def self_improve_from_symptom(self, symptom: str) -> list:
        """Run a diagnosis-and-edit cycle for a detected symptom.

        Builds a minimal LLM-backed diagnoser from the current router, then runs
        the full spec_search loop. REVIEW/MANUAL tier edits are also enqueued as
        self_improve tasks so they appear in /tasks and /approvals."""
        from hive.core.spec_search import Edit, EditOp, SelfImprovement, diagnose_and_run
        improver = SelfImprovement(self.self_modifier, pending_store=self.edit_pending)

        _OP_VALUES = {e.value for e in EditOp}
        _SCHEMA = (
            "Each edit must be a JSON object with: "
            '"op" (one of: ' + ", ".join(sorted(_OP_VALUES)) + "), "
            '"summary" (str), "rationale" (str). '
            'For file edits also include "path" (repo-relative), '
            '"old_text" (str), "new_text" (str).'
        )

        async def _diagnoser(context: str) -> list[Edit]:
            try:
                import json as _json
                res = await self.router.complete(
                    [{"role": "user", "content": (
                        "You are Hive's self-improvement diagnoser. "
                        "Analyse this symptom and propose zero or more typed edits "
                        "as a JSON array. Symptom:\n" + context
                    )}],
                    system=f"Return ONLY a JSON array of edit objects or []. {_SCHEMA}",
                )
                raw = _json.loads(res.text or "[]")
                if not isinstance(raw, list):
                    return []
                edits: list[Edit] = []
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    try:
                        op = EditOp(item["op"])
                    except (KeyError, ValueError):
                        log.warning("diagnoser: unknown op %r — skipping", item.get("op"))
                        continue
                    path = item.get("path", "")
                    old_text = item.get("old_text", "")
                    new_text = item.get("new_text", "")

                    async def _apply(wt: str, _p: str = path,
                                     _old: str = old_text, _new: str = new_text) -> list[str]:
                        if not (_p and _old):
                            return []
                        from pathlib import Path as _Path
                        target = _Path(wt) / _p
                        if not target.exists():
                            return []
                        content = target.read_text(encoding="utf-8")
                        if _old not in content:
                            return []
                        target.write_text(content.replace(_old, _new, 1), encoding="utf-8")
                        return [_p]

                    edits.append(Edit(
                        op=op,
                        summary=item.get("summary", f"auto-edit: {op.value}"),
                        rationale=item.get("rationale", ""),
                        apply=_apply,
                    ))
                return edits
            except Exception:  # noqa: BLE001
                return []

        outcomes = await diagnose_and_run(_diagnoser, symptom, improver)
        from hive.core.spec_search import RiskTier
        for outcome in outcomes:
            if outcome.tier in (RiskTier.REVIEW, RiskTier.MANUAL):
                self.task_board.enqueue(
                    "self_improve",
                    {"symptom": symptom[:200], "tier": outcome.tier.value,
                     "detail": outcome.detail[:300],
                     "edit_id": outcome.edit_id},
                    source="heartbeat",
                )
        return outcomes

    def mcp_server(self, *, name: str = "hive") -> "MCPServer":
        """Return an MCPServer that exposes the live tool registry over MCP stdio.
        Lazy import keeps the mcp SDK optional at runtime."""
        from hive.tools.mcp.server import MCPServer
        return MCPServer(self.tools, name=name)

    async def self_improve(self, edits: list[Edit], *, dry_run: bool = False,
                           ) -> list[EditOutcome]:
        """Drive proposed edits through the risk gate (AUTO->PR / REVIEW->approval /
        MANUAL->recorded). Hive NEVER merges; AUTO edits open a draft PR for a human."""
        return await self.improver.run(edits, dry_run=dry_run)

    async def aclose(self) -> None:
        close_router = getattr(self.router, "aclose", None)
        if close_router is not None:
            await close_router()
        # Graceful memory shutdown (both provider types expose close/on_session_end).
        mem_close = getattr(self.memory, "close", None)
        if mem_close is not None:
            mem_close()
        self.session_store.close()
        self.skill_usage.close()
        self.task_board.close()
        self.cron.close()
        self.commitments.close()
        self.audit_log.close()

    @classmethod
    def build(cls, config: HiveConfig | None = None, *,
              router: ModelRouter | None = None) -> "HiveOS":
        """Construct + wire every subsystem. Inject `router` to bypass the network in tests."""
        cfg = config or HiveConfig.from_env()
        cfg.ensure_dirs()
        set_config(cfg)                       # make get_config() return the built config (D1)
        credentials.inject()                   # populate env from the 0o600 vault (A4)
        events = EventBus()                    # each assembled HiveOS owns its bus (no cross-talk)

        # Budget guard: sync gate for the router; record_call on every successful call.
        budgeter = Budgeter(daily_cap=cfg.daily_call_cap, warn_pct=cfg.window_warn_pct)
        events.subscribe(EventType.INFERENCE_END, budgeter.record_call)
        events.subscribe(EventType.INFERENCE_END, budgeter.record_usage)  # per-token cost
        telemetry = Telemetry().attach(events)
        traces = TraceCollector().attach(events)

        catalog = ModelCatalog()
        # M8: pick the executor provider (minimax|anthropic) from config; A4: pool keys
        # from the 0o600 vault merged with env, comma-split for multi-key failover.
        if cfg.exec_provider.lower() == "anthropic":
            exec_base, key_env, key_default = (cfg.anthropic_base, "ANTHROPIC_API_KEY",
                                               cfg.anthropic_api_key)
        else:
            exec_base, key_env, key_default = (cfg.minimax_anthropic_base, "MINIMAX_API_KEY",
                                               cfg.minimax_api_key)
        raw_key = credentials.get(key_env, key_default) or ""
        exec_keys = [k.strip() for k in raw_key.split(",") if k.strip()] or [key_default]
        router = router or ModelRouter(
            config=cfg,
            adapter=make_adapter(cfg.exec_provider, base_url=exec_base, catalog=catalog),
            credential_pool=CredentialPool(exec_keys),
            catalog=catalog,
            events=events,
            budget=budgeter.gate,
        )

        # Shared state DB holds memory + session tables (OpenClaw: one shared state DB).
        # Memory provider: real Mnemosyne when installed/configured, else local SQLite.
        memory: MemoryProvider = (
            build_mnemosyne_provider(home=cfg.mnemosyne_home)
            or LocalMemoryProvider(cfg.state_db, vault=ObsidianVault(cfg.obsidian_vault))
        )
        # M9-b: wire host-LLM backend so Mnemosyne consolidation gets LLM backing.
        # A dedicated asyncio loop + daemon thread avoids cross-loop httpx reuse.
        from hive.memory.mnemosyne_provider import HiveMnemosyneProvider
        if isinstance(memory, HiveMnemosyneProvider):
            aux_adapter = make_adapter(cfg.exec_provider, base_url=exec_base, catalog=catalog)
            memory.set_host_llm_backend(
                aux_adapter, cfg.aux_model,
                api_key=exec_keys[0] if exec_keys else "",
            )
        session_store = SessionStore(cfg.state_db)

        # Fresh per-build tool registry so repeated build() calls don't collide.
        class _Registry(ToolRegistry):
            pass
        # A1: the discovery-first tool gets memory (for caching) + Hive's GitHub token.
        tools = register_builtins(_Registry, memory=memory, github_token=cfg.github_token,
                                  telegram_token=cfg.telegram_token)
        audit_log = AuditLog(cfg.data_dir / "audit.sqlite")
        tool_executor = ToolExecutor(tools, events=events, audit=audit_log.record)

        # Aux-model summarizer wired here so memory/context never import llm (strict DAG).
        async def summarize(messages: list[Message], system: str) -> str:
            result = await router.complete(messages, kind=TaskKind.AUX, system=system,
                                           thinking=False, max_tokens=2048)
            return result.text

        keeper = MemoryKeeper(summarize, memory)
        planner = Planner(router)
        orchestrator = ConversationOrchestrator(
            router, tools=tools, tool_executor=tool_executor,
            memory=memory, session_store=session_store, events=events,
            summarizer=summarize,
        )

        # M2 self-improvement: skill lifecycle + risk-gated self-mod (all on the
        # existing safety spine — AUTO opens a draft PR, REVIEW hits the approval
        # gate, Hive never merges).
        skill_usage = SkillUsageStore(cfg.state_db)
        # Built-in tools are bundled, not agent-created — register them as such so the
        # Curator's lifecycle (stale->archived) can NEVER touch them.
        for name in tools:
            skill_usage.register(name, agent_created=False)

        def _record_skill_use(event: object) -> None:
            data = getattr(event, "data", event) or {}
            if data.get("status") == "ok" and data.get("tool"):
                skill_usage.record_use(str(data["tool"]))
        events.subscribe(EventType.TOOL_CALL_END, _record_skill_use)

        curator = Curator(skill_usage, backup_dir=cfg.data_dir / "backups" / "skills")
        # Real PR opener only when Hive's GitHub identity is configured; else None
        # (SelfModifier still pushes the branch — a human opens the PR).
        opener = None
        if cfg.github_token and cfg.github_owner and cfg.github_repo:
            opener = github_pr_opener(cfg.github_token, cfg.github_owner, cfg.github_repo)
        # Optional sandbox: run candidate test suites in a container (HIVE_SANDBOX_IMAGE).
        # With no image this is the plain local runner.
        sandbox_run = make_sandbox_runner(cfg.sandbox_image or None, repo_root=str(cfg.root))
        self_modifier = SelfModifier(repo_root=str(cfg.root), open_pr=opener, run=sandbox_run)
        edit_pending: dict = {}
        improver = SelfImprovement(self_modifier, pending_store=edit_pending)

        # M3 autonomy: durable task board + cron + commitments (all SQLite-first).
        task_board = TaskBoard(cfg.state_db)
        cron = CronScheduler(cfg.state_db, task_board)
        commitments = CommitmentBook(cfg.state_db, task_board)

        # Named agent registry: allows delegate_named(task, "researcher") by name.
        from hive.agents.delegate import register_agent

        def _leaf_factory(agent_name: str):
            def factory() -> ConversationOrchestrator:  # type: ignore[name-defined]
                return ConversationOrchestrator(
                    router, tools=tools, tool_executor=tool_executor,
                    memory=memory, session_store=session_store, events=events,
                )
            factory.__name__ = agent_name
            return factory

        _specialist_names = [
            "researcher", "coder", "reviewer", "memory-keeper", "security-reviewer",
        ]
        agents_registry: dict = {}
        for _name in _specialist_names:
            _factory = _leaf_factory(_name)
            register_agent(_name, _factory)
            agents_registry[_name] = _factory

        log.info("HiveOS built (tools=%d, exec_model=%s)", len(tools), cfg.exec_model)
        return cls(
            config=cfg, events=events, router=router, tools=tools,
            tool_executor=tool_executor, memory=memory, session_store=session_store,
            keeper=keeper, planner=planner, orchestrator=orchestrator,
            budgeter=budgeter, telemetry=telemetry, traces=traces, audit_log=audit_log,
            skill_usage=skill_usage, curator=curator, self_modifier=self_modifier,
            improver=improver, task_board=task_board, cron=cron, commitments=commitments,
            agents_registry=agents_registry, edit_pending=edit_pending,
        )
