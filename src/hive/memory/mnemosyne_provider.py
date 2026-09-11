"""
mnemosyne_provider.py — HiveOS-native Mnemosyne memory adapter.

Uses the mnemosyne-memory package (v3.x) directly — no Hermes glue.
Wraps `Mnemosyne` under HiveOS's MemoryProvider ABC.

Wiring (runtime.py):
    provider = build_mnemosyne_provider(home=cfg.mnemosyne_home)
               or LocalMemoryProvider(cfg.state_db)
"""
from __future__ import annotations

import json
import logging
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Protocol

from hive.core.types import ContentTrust
from hive.memory.entity_resolver import EntityResolver
from hive.memory.provider import MemoryProvider

log = logging.getLogger("hive.memory.mnemosyne")


def _trusted_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fail closed when a Mnemosyne result has no explicit trusted provenance."""
    return [
        item for item in results
        if item.get("veracity") == "stated" or item.get("trust_tier") == "trusted"
    ]


_MEMORY_V2_PREFIX = "[hive-memory-v2] "


def _encode_memory_payload(
    kind: str, topic: str, content: str, trust: ContentTrust,
    *, revision: str | None = None,
) -> str:
    """Encode memory identity without ambiguous presentation delimiters."""
    return _MEMORY_V2_PREFIX + json.dumps(
        {
            "kind": kind,
            "topic": topic,
            "content": content,
            "trust": trust.value,
            "revision": revision or uuid.uuid4().hex,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_memory_payload(item: dict[str, Any]) -> tuple[str, str, str] | None:
    """Decode the M2 envelope, with read compatibility for pre-M2 rows."""
    content = str(item.get("content", ""))
    if content.startswith(_MEMORY_V2_PREFIX):
        try:
            payload = json.loads(content[len(_MEMORY_V2_PREFIX):])
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        return (
            str(payload.get("kind", "")),
            str(payload.get("topic", "")),
            str(payload.get("content", "")),
        )
    match = re.match(r"^\[([^]]+)]\s+(.+?):\s+(.*)$", content)
    if match is None:
        return None
    return match.group(1), match.group(2), match.group(3)


def _matches_memory_identity(
    item: dict[str, Any], kind: str, topic: str,
) -> bool:
    """Match kind plus canonical topic from a structured host payload."""
    raw_content = str(item.get("content", ""))
    if not raw_content.startswith(_MEMORY_V2_PREFIX):
        legacy_prefix = f"[{kind}] {topic}: "
        if not raw_content.startswith(legacy_prefix):
            return False
        # The legacy format has no escaping. A second delimiter makes a
        # shorter-topic interpretation ambiguous, so preserve rather than
        # invalidating the wrong historical fact.
        return ": " not in raw_content[len(legacy_prefix):]
    decoded = _decode_memory_payload(item)
    if decoded is None:
        return False
    return (
        decoded[0] == kind
        and EntityResolver().canonical_key(decoded[1])
        == EntityResolver().canonical_key(topic)
    )


def _matches_memory_fact(
    item: dict[str, Any], kind: str, topic: str, content: str,
) -> bool:
    raw_content = str(item.get("content", ""))
    if not raw_content.startswith(_MEMORY_V2_PREFIX):
        legacy_prefix = f"[{kind}] {topic}: "
        if not raw_content.startswith(legacy_prefix):
            return False
        remainder = raw_content[len(legacy_prefix):]
        return ": " not in remainder and remainder == content
    decoded = _decode_memory_payload(item)
    return bool(
        decoded
        and decoded[0] == kind
        and EntityResolver().canonical_key(decoded[1])
        == EntityResolver().canonical_key(topic)
        and decoded[2] == content
    )


def _memory_trust_rank(item: dict[str, Any]) -> int:
    return int(
        item.get("trust_tier") == ContentTrust.TRUSTED.value
        or item.get("veracity") == "stated"
    )


def _memory_display_content(item: dict[str, Any]) -> str:
    raw_content = str(item.get("content", ""))
    if not raw_content.startswith(_MEMORY_V2_PREFIX):
        return raw_content
    decoded = _decode_memory_payload(item)
    return f"{decoded[1]}: {decoded[2]}" if decoded is not None else raw_content


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

    def close(self) -> None:
        """Release every SQLite connection held by Mnemosyne 3.x."""
        beam = getattr(self._beam, "beam", None)
        beam_conn = getattr(beam, "conn", None)
        root_conn = getattr(self._beam, "conn", None)
        first_error: Exception | None = None
        for connection in (beam_conn, root_conn):
            if connection is not None:
                try:
                    connection.close()
                except Exception as exc:  # noqa: BLE001 - close remaining handles
                    first_error = first_error or exc
                for module_name in ("mnemosyne.core.beam", "mnemosyne.core.memory"):
                    module = sys.modules.get(module_name)
                    thread_local = getattr(module, "_thread_local", None)
                    if getattr(thread_local, "conn", None) is connection:
                        thread_local.conn = None
                        thread_local.db_path = None
        self._beam = None
        if first_error is not None:
            raise first_error

    # ------------------------------------------------------------------
    # MemoryProvider surface
    # ------------------------------------------------------------------

    def system_prompt_block(self) -> str:
        if self._beam is None:
            return ""
        try:
            results = _trusted_results(list(self._beam.recall(
                "identity system facts goals", top_k=5,
            ) or []))
            if not results:
                return ""
            lines = ["## Persistent Memory (top facts)"]
            for r in results:
                score = r.get("score", 0)
                content = _memory_display_content(r)
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
            results = _trusted_results(list(self._beam.recall(
                query, top_k=self.PREFETCH_TOP_K,
            ) or []))
            if not results:
                return ""
            lines = ["<memory-context>"]
            for r in results:
                score = r.get("score", 0)
                content = _memory_display_content(r)
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
                self._beam.remember(
                    _encode_memory_payload(
                        "turn", "user", user_content, ContentTrust.TRUSTED,
                    ),
                    importance=0.7, source="user-turn",
                    trust_tier="trusted", veracity="stated",
                )
            if assistant_content:
                self._beam.remember(
                    _encode_memory_payload(
                        "turn", "assistant", assistant_content,
                        ContentTrust.UNTRUSTED,
                    ),
                    importance=0.5, source="hive-turn",
                    trust_tier="untrusted", veracity="inferred",
                )
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
                        "importance": {"type": "number", "default": 0.5,
                                       "description": "Advisory salience; inferred memory is capped at 0.5."},
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
                source = str(args.get("source", "agent"))
                content = _encode_memory_payload(
                    "agent-memory", source, str(args.get("content", "")),
                    ContentTrust.UNTRUSTED,
                )
                mem_id = self._beam.remember(
                    content,
                    importance=min(float(args.get("importance", 0.5)), 0.5),
                    source=source,
                    trust_tier=ContentTrust.UNTRUSTED.value,
                    veracity="inferred",
                )
                return f"stored: {str(mem_id)[:8]}"
            if tool_name == "hive_recall":
                results = self._beam.recall(
                    args.get("query", ""), top_k=int(args.get("top_k", 5))
                )
                if not results:
                    return "no memories found"
                return "\n".join(
                    f"[{r.get('score', 0):.2f}] {_memory_display_content(r)}"
                    for r in results
                )
            if tool_name == "hive_memory_sleep":
                return str(self._beam.sleep())
        except Exception as exc:  # noqa: BLE001
            return f"[memory error: {exc}]"
        return f"[unknown memory tool: {tool_name}]"

    def learn_memory(self, content: str, *, importance: float, source: str,
                     trust: ContentTrust) -> str:
        """Store one memory and return its unformatted id for supersession."""
        if self._beam is None:
            raise RuntimeError("memory not initialised")
        memory_id = self._beam.remember(
            content, importance=importance, source=source,
            trust_tier=trust.value,
            veracity="stated" if trust is ContentTrust.TRUSTED else "inferred",
        )
        return str(memory_id)

    def invalidate(self, memory_id: str, *, replacement_id: str) -> None:
        """Soft-supersede an old memory in the native backend."""
        if self._beam is None:
            raise RuntimeError("memory not initialised")
        self._beam.invalidate(memory_id, replacement_id=replacement_id)

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

    def __init__(self, inner: Any) -> None:
        self._inner = inner

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
        try:
            self._inner.sync_turn(user_content, assistant_content, session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("sync_turn failed — conversation turn not persisted to memory: %s", exc)

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        try:
            return self._inner.get_tool_schemas() or []
        except Exception as exc:  # noqa: BLE001
            log.debug("get_tool_schemas failed: %s", exc)
            return []

    def handle_tool_call(self, tool_name: str, args: dict[str, Any]) -> str:
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

    def recall(self, query: str, limit: int = 5, *,
               trusted_only: bool = False) -> list[dict[str, Any]]:
        try:
            kwargs: dict[str, Any] = {"top_k": limit}
            if hasattr(self._inner, "recall"):
                results = list(self._inner.recall(query, **kwargs) or [])
            elif hasattr(self._inner, "_beam") and self._inner._beam is not None:
                results = list(self._inner._beam.recall(query, **kwargs) or [])
            else:
                results = []
            return _trusted_results(results) if trusted_only else results
        except Exception as exc:  # noqa: BLE001
            log.debug("mnemosyne recall failed: %s", exc)
        return []

    def already_known(self, topic: str, *, content: str | None = None) -> bool:
        hits = self.recall(topic, limit=20)
        if content is None:
            return bool(hits)
        return any(
            hit.get("content") == content
            or (
                (decoded := _decode_memory_payload(hit)) is not None
                and EntityResolver().canonical_key(decoded[1])
                == EntityResolver().canonical_key(topic)
                and decoded[2] == content
            )
            for hit in hits
        )

    def learn(self, kind: str, topic: str, content: str, source: str = "", *,
              trust: ContentTrust = ContentTrust.UNTRUSTED,
              importance: float = 0.5, supersede: bool = False) -> str | None:
        try:
            payload = _encode_memory_payload(kind, topic, content, trust) if topic else content
            prior = self.recall(topic, limit=20)
            new_trust_rank = int(trust is ContentTrust.TRUSTED)
            duplicate = next(
                (
                    item for item in prior
                    if _matches_memory_fact(item, kind, topic, content)
                    and item.get("id")
                    and _memory_trust_rank(item) >= new_trust_rank
                ),
                None,
            )
            if duplicate is not None:
                if supersede:
                    invalidate = getattr(self._inner, "invalidate", None)
                    if invalidate is None and hasattr(self._inner, "_beam"):
                        invalidate = getattr(self._inner._beam, "invalidate", None)
                    if invalidate is None:
                        raise RuntimeError("Mnemosyne correction requires invalidate()")
                    for item in prior:
                        if (
                            item.get("id")
                            and str(item["id"]) != str(duplicate["id"])
                            and _matches_memory_identity(item, kind, topic)
                            and _memory_trust_rank(item) <= new_trust_rank
                        ):
                            invalidate(
                                str(item["id"]), replacement_id=str(duplicate["id"]),
                            )
                return str(duplicate["id"])
            old_ids = [
                str(item["id"])
                for item in prior
                if supersede
                and item.get("id")
                and _matches_memory_identity(item, kind, topic)
                and _memory_trust_rank(item) <= new_trust_rank
            ]
            if isinstance(self._inner, _HiveMnemosyneInner):
                memory_id = self._inner.learn_memory(
                    payload, importance=max(0.0, min(float(importance), 1.0)),
                    source=source or kind, trust=trust,
                )
            elif hasattr(self._inner, "handle_tool_call"):
                memory_id = self._inner.handle_tool_call(
                    "hive_remember",
                    {
                        "content": payload,
                        "importance": max(0.0, min(float(importance), 1.0)),
                        "source": source or kind,
                        "trust": trust.value,
                    },
                )
            else:
                return None
            if supersede and memory_id:
                invalidate = getattr(self._inner, "invalidate", None)
                if invalidate is None and hasattr(self._inner, "_beam"):
                    invalidate = getattr(self._inner._beam, "invalidate", None)
                if invalidate is None:
                    raise RuntimeError("Mnemosyne correction requires invalidate()")
                for old_id in old_ids:
                    invalidate(old_id, replacement_id=str(memory_id))
            return str(memory_id) if memory_id else None
        except Exception as exc:  # noqa: BLE001
            log.debug("mnemosyne learn failed: %s", exc)
        return None

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

    def disable_host_llm_backend(self) -> None:
        """Replace any process-global host backend with a no-network fallback.

        Mnemosyne's host-LLM registration is process-global.  Merely skipping a
        new registration would leave a previously built runtime's adapter active,
        which could bypass an enabled HiveOS spend cap.  Installing this inert
        backend makes consolidation use Mnemosyne's fallback without external LLM
        calls.
        """
        if hasattr(self._inner, "set_host_llm_backend"):
            self._inner.set_host_llm_backend(lambda _prompt: None)
            log.warning("Mnemosyne host-LLM backend disabled by active spend cap")
        else:
            log.debug("Mnemosyne inner has no host-LLM seam to disable")

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
        provider = HiveMnemosyneProvider(inner)
        log.info("Mnemosyne provider active (home=%s)", home)
        return provider
    except Exception as exc:  # noqa: BLE001
        log.warning("Mnemosyne provider init failed; falling back to local: %s", exc)
        return None
