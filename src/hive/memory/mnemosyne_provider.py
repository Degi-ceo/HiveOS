"""
mnemosyne_provider.py — HiveOS-native Mnemosyne memory adapter.

Uses the mnemosyne-memory package (v3.x) directly — no Hermes glue.
Wraps `Mnemosyne` under HiveOS's MemoryProvider ABC.

Wiring (runtime.py):
    provider = build_mnemosyne_provider(home=cfg.mnemosyne_home)
               or LocalMemoryProvider(cfg.state_db)
"""
from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path
from typing import Any, Protocol

from hive.memory.ledger import MemoryLedger
from hive.memory.obsidian_projector import ObsidianShadowProjector
from hive.memory.provider import MemoryProvider

log = logging.getLogger("hive.memory.mnemosyne")


def _add_mnemosyne_to_path(mnemosyne_root: Path) -> None:
    s = str(mnemosyne_root)
    if s not in sys.path:
        sys.path.insert(0, s)


class _HostLLMBackend(Protocol):
    name: str

    def complete(self, prompt: str, **kwargs: Any) -> str | None: ...


def _register_host_llm(backend: _HostLLMBackend) -> bool:
    """Register backend with Mnemosyne's global LLM seam (A3).

    Best-effort: returns False if the seam is unavailable.
    Backend must have a .complete(prompt, **kwargs) -> str | None method.
    """
    try:
        from mnemosyne.core.llm_backends import set_host_llm_backend
    except ImportError:
        try:
            from core.llm_backends import set_host_llm_backend  # type: ignore[import]
        except ImportError:
            return False
    try:
        set_host_llm_backend(backend)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("host LLM registration failed (seam exists but rejected backend): %s", exc)
        return False


class _HiveMnemosyneInner:
    """Native Mnemosyne backend for HiveOS.

    Wraps mnemosyne.Mnemosyne (v3.x API) directly.  Provides the lifecycle
    interface expected by HiveMnemosyneProvider without Hermes glue.

    Agent identity: author_id="hive", bank="hive-main".
    Cron/sleep: driven by hiveos-keeper.service — not per-session.
    Tool names: hive_remember / hive_recall / hive_memory_sleep (HiveOS namespace).
    """

    BANK = "hive-main"
    AUTHOR_ID = "hive"
    PREFETCH_TOP_K = 5
    PREFETCH_MIN_SCORE = 0.30

    def __init__(self) -> None:
        self._beam: Any = None          # mnemosyne.Mnemosyne instance
        self._home: str = ""
        self._session_id: str = "hive-default"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        from mnemosyne import Mnemosyne  # type: ignore[import]
        self._session_id = session_id
        self._home = kwargs.get("hermes_home", "") or self._home
        db_path = str(Path(self._home) / "hive.db") if self._home else ":memory:"
        self._beam = Mnemosyne(
            session_id=session_id,
            db_path=db_path,
            bank=self.BANK,
            author_id=self.AUTHOR_ID,
            author_type="agent",
        )
        log.info("Mnemosyne beam active (session=%s, db=%s)", session_id, db_path)

    def on_session_end(self, messages: list) -> None:  # noqa: ARG002
        pass  # sleep is driven by hiveos-keeper.service, not per-session

    # ------------------------------------------------------------------
    # MemoryProvider surface
    # ------------------------------------------------------------------

    def system_prompt_block(self) -> str:
        if self._beam is None:
            return ""
        try:
            results = self._beam.recall("identity system facts goals", top_k=5)
            if not results:
                return ""
            lines = ["## Persistent Memory (top facts)"]
            for r in results:
                score = r.get("score", 0)
                content = r.get("content", "")
                if score >= self.PREFETCH_MIN_SCORE and content:
                    lines.append(f"- {content[:150]}")
            if len(lines) <= 1:
                return ""
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            log.warning("system_prompt_block failed — memory context skipped: %s", exc)
            return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:  # noqa: ARG002
        if self._beam is None or not query:
            return ""
        try:
            results = self._beam.recall(query, top_k=self.PREFETCH_TOP_K)
            if not results:
                return ""
            lines = ["<memory-context>"]
            for r in results:
                score = r.get("score", 0)
                content = r.get("content", "")
                if score >= self.PREFETCH_MIN_SCORE and content:
                    lines.append(f"- [{score:.2f}] {content}")
            if len(lines) <= 1:
                return ""
            lines.append("</memory-context>")
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            log.warning("prefetch failed — no memory context injected: %s", exc)
            return ""

    def sync_turn(self, user_content: str, assistant_content: str,
                  *, session_id: str = "") -> None:  # noqa: ARG002
        if self._beam is None:
            return
        try:
            if user_content:
                self._beam.remember(user_content, importance=0.6, source="user-turn")
            if assistant_content:
                self._beam.remember(assistant_content, importance=0.5, source="hive-turn")
        except Exception as exc:  # noqa: BLE001
            log.warning("sync_turn failed — conversation turn not persisted to memory: %s", exc)

    # ------------------------------------------------------------------
    # Memory tools (hive namespace)
    # ------------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "hive_remember",
                "description": "Store a durable memory in Hive's persistent memory layer.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "Memory text to store."},
                        "importance": {"type": "number", "default": 0.7,
                                       "description": "0.0–1.0. Higher = retained longer."},
                        "source": {"type": "string", "default": "agent",
                                   "description": "Tag for provenance (e.g. 'preference', 'fact')."},
                    },
                    "required": ["content"],
                },
            },
            {
                "name": "hive_recall",
                "description": "Search Hive's memory for relevant context.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "top_k": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "hive_memory_sleep",
                "description": "Consolidate working memory into long-term episodic memory.",
                "parameters": {"type": "object", "properties": {}},
            },
        ]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any]) -> str:
        if self._beam is None:
            return "[memory not initialised]"
        try:
            if tool_name == "hive_remember":
                mem_id = self._beam.remember(
                    args.get("content", ""),
                    importance=float(args.get("importance", 0.7)),
                    source=args.get("source", "agent"),
                )
                return f"stored: {str(mem_id)[:8]}"
            if tool_name == "hive_recall":
                results = self._beam.recall(
                    args.get("query", ""), top_k=int(args.get("top_k", 5))
                )
                if not results:
                    return "no memories found"
                return "\n".join(
                    f"[{r.get('score', 0):.2f}] {r.get('content', '')}"
                    for r in results
                )
            if tool_name == "hive_memory_sleep":
                return str(self._beam.sleep())
        except Exception as exc:  # noqa: BLE001
            return f"[memory error: {exc}]"
        return f"[unknown memory tool: {tool_name}]"

    def project_ledger(self, ledger: MemoryLedger) -> list[object]:
        if self._beam is None:
            return []
        from hive.memory.mnemosyne_projector import MnemosyneProjector

        return MnemosyneProjector(ledger, self._beam).project_pending()

    # ------------------------------------------------------------------
    # Host-LLM bridge (A3)
    # ------------------------------------------------------------------

    def set_host_llm_backend(self, sync_fn: Any) -> None:
        """Register a sync str->str callable as Mnemosyne's LLM backend.

        sync_fn is created by HiveMnemosyneProvider.set_host_llm_backend and
        bridges async HiveOS adapter → sync call safe from Mnemosyne's
        consolidation thread.  Wrap it in an object with .complete() so
        Mnemosyne's llm_backends seam is satisfied.
        """
        class _SyncBackend:
            name = "hive-host-llm"

            def complete(self, prompt: str, **_: Any) -> str | None:
                try:
                    return sync_fn(prompt)
                except Exception as exc:  # noqa: BLE001
                    log.warning("host LLM sync_fn failed (consolidation uses fallback): %s", exc)
                    return None

        _register_host_llm(_SyncBackend())


class HiveMnemosyneProvider(MemoryProvider):
    """Thin lifecycle wrapper over _HiveMnemosyneInner.

    Keeps the fail-open contract: every call is guarded so a Mnemosyne
    error never crashes the gateway.  The inner can be any object with the
    same surface (used directly in tests with MagicMock).
    """

    name = "mnemosyne"

    def __init__(
        self,
        inner: Any,
        *,
        ledger: MemoryLedger | None = None,
        shadow_root: Path | None = None,
    ) -> None:
        self._inner = inner
        self._ledger = ledger
        self._shadow = (ObsidianShadowProjector(ledger, shadow_root)
                        if ledger is not None and shadow_root is not None else None)

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        try:
            self._inner.initialize(session_id, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("Mnemosyne initialize failed (continuing without memory): %s", exc)

    def system_prompt_block(self) -> str:
        try:
            return self._inner.system_prompt_block() or ""
        except Exception as exc:  # noqa: BLE001
            log.debug("system_prompt_block failed: %s", exc)
            return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        try:
            return self._inner.prefetch(query, session_id=session_id) or ""
        except Exception as exc:  # noqa: BLE001
            log.debug("prefetch failed: %s", exc)
            return ""

    def sync_turn(self, user_content: str, assistant_content: str,
                  *, session_id: str = "", messages: list | None = None) -> None:
        """Persist a turn through the canonical ledger when it is available.

        The session store already owns the chronological transcript. This memory
        record gives the external recall layer one durable, idempotent projection
        boundary instead of two unjournaled remote ``remember`` calls.
        """
        if self._ledger is not None:
            if not user_content and not assistant_content:
                return
            session = session_id or "default"
            digest = hashlib.sha256(
                f"{session}\0{user_content}\0{assistant_content}".encode("utf-8")
            ).hexdigest()
            try:
                self._ledger.remember(
                    kind="session",
                    stable_key=f"session-turn:{digest}",
                    content=f"User:\n{user_content}\n\nAssistant:\n{assistant_content}",
                    source=f"conversation:{session}",
                    idempotency_key=f"mnemosyne-turn:{digest}",
                )
            except Exception as exc:  # noqa: BLE001 - never bypass canonical persistence
                log.warning("canonical conversation-memory ledger write failed: %s", exc)
                return
            self._project_canonical_ledger()
            return
        try:
            self._inner.sync_turn(user_content, assistant_content, session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("sync_turn failed — conversation turn not persisted to memory: %s", exc)

    def _project_canonical_ledger(self) -> None:
        """Attempt projections; the ledger remains the recovery record on failure."""
        try:
            if hasattr(self._inner, "project_ledger"):
                self._inner.project_ledger(self._ledger)
        except Exception as exc:  # noqa: BLE001 - outbox state remains the recovery record
            log.warning("canonical Mnemosyne projection failed; review the outbox: %s", exc)
        try:
            if self._shadow is not None:
                self._shadow.project_pending()
        except Exception as exc:  # noqa: BLE001 - local retry remains in the outbox
            log.warning("Obsidian memory projection failed; retry remains durable: %s", exc)

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        try:
            return self._inner.get_tool_schemas() or []
        except Exception as exc:  # noqa: BLE001
            log.debug("get_tool_schemas failed: %s", exc)
            return []

    def handle_tool_call(self, tool_name: str, args: dict[str, Any]) -> str:
        if tool_name == "hive_remember" and self._ledger is not None:
            content = str(args.get("content", "")).strip()
            source = str(args.get("source", "agent")).strip() or "agent"
            if not content:
                return "[memory error: content is required]"
            digest = hashlib.sha256(f"{source}\0{content}".encode("utf-8")).hexdigest()
            try:
                memory = self._ledger.remember(
                    kind="memory", stable_key=f"tool-memory:{digest}", content=content,
                    source=source, idempotency_key=f"mnemosyne-tool:{digest}",
                    provenance_kind="agent", confidence=0.5, veracity="stated",
                )
            except Exception as exc:  # noqa: BLE001 - canonical persistence must never be bypassed
                log.warning("canonical Mnemosyne tool-memory write failed: %s", exc)
                return f"[memory error: {exc}]"
            self._project_canonical_ledger()
            return f"stored: {memory.memory_id[:8]}"
        try:
            return str(self._inner.handle_tool_call(tool_name, args) or "")
        except Exception as exc:  # noqa: BLE001
            log.warning("handle_tool_call(%s) failed: %s", tool_name, exc)
            return f"[memory error: {exc}]"

    def on_session_end(self) -> None:
        try:
            self._inner.on_session_end([])
        except Exception as exc:  # noqa: BLE001
            log.debug("on_session_end failed: %s", exc)

    def recall(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        try:
            if hasattr(self._inner, "recall"):
                return list(self._inner.recall(query, top_k=limit) or [])
            if hasattr(self._inner, "_beam") and self._inner._beam is not None:
                return list(self._inner._beam.recall(query, top_k=limit) or [])
        except Exception as exc:  # noqa: BLE001
            log.debug("mnemosyne recall failed: %s", exc)
        return []

    def already_known(self, topic: str) -> bool:
        return bool(self.recall(topic, limit=1))

    def learn(self, kind: str, topic: str, content: str, source: str = "") -> None:
        if self._ledger is not None:
            idempotency_key = hashlib.sha256(
                f"{kind}\0{topic}\0{content}\0{source}".encode("utf-8")
            ).hexdigest()
            try:
                self._ledger.remember(
                    kind=kind,
                    stable_key=f"{kind}:{topic}",
                    content=content,
                    source=source or kind,
                    idempotency_key=f"mnemosyne:{idempotency_key}",
                )
            except Exception as exc:  # noqa: BLE001 - canonical persistence must never be bypassed
                log.warning("canonical Mnemosyne ledger write failed; memory was not projected: %s", exc)
                return
            self._project_canonical_ledger()
            return
        try:
            payload = f"[{kind}] {topic}: {content}" if topic else content
            if hasattr(self._inner, "handle_tool_call"):
                self._inner.handle_tool_call(
                    "hive_remember",
                    {"content": payload, "importance": 0.7, "source": source or kind},
                )
        except Exception as exc:  # noqa: BLE001
            log.debug("mnemosyne learn failed: %s", exc)

    def set_host_llm_backend(self, adapter: object, model: str, *,
                             api_key: str = "", timeout: float = 30.0) -> None:
        """Bridge the async LLM adapter → Mnemosyne's sync consolidation thread.

        Spins a private daemon event loop so the adapter's httpx client is
        never accessed across asyncio loops (cross-loop safety, A3).
        """
        import asyncio
        import threading

        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True,
                         name="mnemosyne-llm-loop").start()

        def _sync_complete(prompt: str) -> str | None:
            from hive.core.types import Message, Role
            from hive.llm.adapters.base import CompletionRequest

            async def _call() -> str:
                req = CompletionRequest(
                    model=model,
                    messages=[Message(role=Role.USER, content=prompt)],
                    thinking=False,
                    max_tokens=2048,
                )
                result = await adapter.complete(req, api_key=api_key)  # type: ignore[attr-defined]
                return result.text

            import concurrent.futures
            fut = asyncio.run_coroutine_threadsafe(_call(), loop)
            try:
                return fut.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                fut.cancel()
                log.warning("host LLM call timed out after %ss — cancelling", timeout)
                return None
            except Exception as exc:  # noqa: BLE001
                log.warning("host LLM call failed (%s): %s", type(exc).__name__, exc)
                return None

        if hasattr(self._inner, "set_host_llm_backend"):
            self._inner.set_host_llm_backend(_sync_complete)
            log.info("Mnemosyne host-LLM backend wired (model=%s)", model)
        else:
            log.debug("Mnemosyne inner has no set_host_llm_backend; skipping bridge")

    def close(self) -> None:
        close = getattr(self._inner, "close", None) or getattr(self._inner, "shutdown", None)
        if close is not None:
            try:
                close()
            except Exception as exc:  # noqa: BLE001
                log.debug("Mnemosyne close failed: %s", exc)


def build_mnemosyne_provider(
    *,
    home: Path,
    session_id: str = "default",
    mnemosyne_root: Path | None = None,
    ledger: MemoryLedger | None = None,
    shadow_root: Path | None = None,
) -> HiveMnemosyneProvider | None:
    """Build a live HiveMnemosyneProvider or return None if unavailable.

    Host-LLM wiring (A3) is done separately by the caller via
    provider.set_host_llm_backend() — that path builds the required sync
    bridge (daemon loop + run_coroutine_threadsafe) and is always called
    after build by runtime.py.
    """
    if mnemosyne_root is not None:
        _add_mnemosyne_to_path(mnemosyne_root)

    try:
        import mnemosyne as _mnemo  # type: ignore[import]  # noqa: F401
        del _mnemo
    except ImportError:
        log.info("mnemosyne-memory not installed; using LocalMemoryProvider fallback")
        return None

    try:
        home.mkdir(parents=True, exist_ok=True)
        # host_llm wiring is done AFTER construction via set_host_llm_backend(),
        # which builds the required sync bridge (daemon loop + run_coroutine_threadsafe).
        # Calling _register_host_llm(host_llm) here would pass the raw async adapter
        # object directly to Mnemosyne's consolidation thread — cross-loop hazard.
        inner = _HiveMnemosyneInner()
        inner.initialize(session_id, hermes_home=str(home))
        provider = HiveMnemosyneProvider(inner, ledger=ledger, shadow_root=shadow_root)
        log.info("Mnemosyne provider active (home=%s)", home)
        return provider
    except Exception as exc:  # noqa: BLE001
        log.warning("Mnemosyne provider init failed; falling back to local: %s", exc)
        return None
