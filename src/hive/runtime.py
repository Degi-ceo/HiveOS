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
from hive.context.session_store import SessionStore
from hive.core.budgeter import Budgeter
from hive.core.config import HiveConfig, set_config
from hive.core.events import EventBus, EventType
from hive.core.types import Message
from hive.llm.adapters.minimax import MiniMaxAdapter
from hive.llm.credential_pool import CredentialPool
from hive.llm.model_catalog import ModelCatalog
from hive.llm.router import ModelRouter, TaskKind
from hive.memory.keeper import MemoryKeeper
from hive.memory.local import LocalMemoryProvider
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
    memory: LocalMemoryProvider
    session_store: SessionStore
    keeper: MemoryKeeper
    planner: Planner
    orchestrator: ConversationOrchestrator
    budgeter: Budgeter
    telemetry: Telemetry
    traces: TraceCollector
    audit_log: AuditLog

    async def ask(self, message: str, *, session_id: str = "default") -> str:
        """End-to-end turn; returns the final assistant text."""
        result = await self.orchestrator.ask(message, session_id=session_id)
        return result.content

    async def consolidate(self, session_id: str = "default") -> int:
        return await self.keeper.consolidate(session_id)

    async def aclose(self) -> None:
        close = getattr(self.router, "aclose", None)
        if close is not None:
            await close()
        self.memory.close()
        self.session_store.close()
        self.audit_log.close()

    @classmethod
    def build(cls, config: HiveConfig | None = None, *,
              router: ModelRouter | None = None) -> "HiveOS":
        """Construct + wire every subsystem. Inject `router` to bypass the network in tests."""
        cfg = config or HiveConfig.from_env()
        cfg.ensure_dirs()
        set_config(cfg)                       # make get_config() return the built config (D1)
        events = EventBus()                    # each assembled HiveOS owns its bus (no cross-talk)

        # Budget guard: sync gate for the router; record_call on every successful call.
        budgeter = Budgeter(daily_cap=cfg.daily_call_cap, warn_pct=cfg.window_warn_pct)
        events.subscribe(EventType.INFERENCE_END, budgeter.record_call)
        telemetry = Telemetry().attach(events)
        traces = TraceCollector().attach(events)

        catalog = ModelCatalog()
        router = router or ModelRouter(
            config=cfg,
            adapter=MiniMaxAdapter(cfg.minimax_anthropic_base, catalog),
            credential_pool=CredentialPool([cfg.minimax_api_key]),
            catalog=catalog,
            events=events,
            budget=budgeter.gate,
        )

        # Fresh per-build tool registry so repeated build() calls don't collide.
        class _Registry(ToolRegistry):
            pass
        tools = register_builtins(_Registry)
        audit_log = AuditLog(cfg.data_dir / "audit.sqlite")
        tool_executor = ToolExecutor(tools, events=events, audit=audit_log.record)

        # Shared state DB holds both memory tables and session tables (OpenClaw:
        # one shared state DB for global runtime state).
        memory = LocalMemoryProvider(cfg.state_db, vault=ObsidianVault(cfg.obsidian_vault))
        session_store = SessionStore(cfg.state_db)

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

        log.info("HiveOS built (tools=%d, exec_model=%s)", len(tools), cfg.exec_model)
        return cls(
            config=cfg, events=events, router=router, tools=tools,
            tool_executor=tool_executor, memory=memory, session_store=session_store,
            keeper=keeper, planner=planner, orchestrator=orchestrator,
            budgeter=budgeter, telemetry=telemetry, traces=traces, audit_log=audit_log,
        )
