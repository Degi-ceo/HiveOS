"""
mnemosyne_provider.py — HiveOS adapter for the real Mnemosyne memory engine.

Wraps mnemosyne.hermes_memory_provider.MnemosyneMemoryProvider (MNEMOSYNE_REFERENCE
§6 "shortest path") under HiveOS's MemoryProvider ABC so the runtime can swap in the
real Mnemosyne engine by setting MNEMOSYNE_HOME to a writable path. Falls back
gracefully if the `mnemosyne-memory` package is not installed.

Wire in HiveOS.build():
    provider = build_mnemosyne_provider(cfg) or LocalMemoryProvider(cfg.state_db)
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from hive.memory.provider import MemoryProvider

log = logging.getLogger("hive.memory.mnemosyne")


def _add_mnemosyne_to_path(mnemosyne_root: Path) -> None:
    s = str(mnemosyne_root)
    if s not in sys.path:
        sys.path.insert(0, s)


class HiveMnemosyneProvider(MemoryProvider):
    """Thin adapter: delegates to the real MnemosyneMemoryProvider.

    Responsibility split:
    - HiveOS MemoryProvider ABC: lifecycle hooks, fail-open contract.
    - Mnemosyne engine: storage, recall, embedding, sleep/consolidation.

    The adapter translates the minor interface differences:
    - `sync_turn` sends user+assistant content (Mnemosyne ignores `messages`).
    - `on_session_end` is called with no args (Mnemosyne's hook takes messages;
      we pass [] since session store owns the transcript).
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
            log.debug("sync_turn failed: %s", exc)

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
            # Mnemosyne's hook signature takes messages; pass empty list since
            # HiveOS's session_store owns the transcript.
            self._inner.on_session_end([])
        except Exception as exc:  # noqa: BLE001
            log.debug("on_session_end failed: %s", exc)

    def set_host_llm_backend(self, adapter: object, model: str, *,
                             api_key: str = "", timeout: float = 30.0) -> None:
        """Bridge the async LLM adapter to Mnemosyne's sync consolidation thread.

        Mnemosyne calls a sync `.complete(prompt) -> str` from its background
        consolidation thread.  The shared httpx client inside `adapter` lives on the
        main asyncio event loop and is not thread-safe across loops.  Solution: spin a
        private daemon event loop + thread and dispatch via run_coroutine_threadsafe so
        the adapter's client is created and used exclusively on that loop.
        """
        import asyncio
        import threading

        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True,
                         name="mnemosyne-llm-loop").start()

        def _sync_complete(prompt: str) -> str:
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

            fut = asyncio.run_coroutine_threadsafe(_call(), loop)
            return fut.result(timeout=timeout)

        if hasattr(self._inner, "set_host_llm_backend"):
            self._inner.set_host_llm_backend(_sync_complete)
            log.info("Mnemosyne host-LLM backend wired (model=%s)", model)
        else:
            log.debug("Mnemosyne inner provider has no set_host_llm_backend; skipping bridge")

    def close(self) -> None:
        close = getattr(self._inner, "close", None) or getattr(self._inner, "shutdown", None)
        if close is not None:
            try:
                close()
            except Exception as exc:  # noqa: BLE001
                log.debug("Mnemosyne close failed: %s", exc)


def _register_host_llm(backend: object) -> bool:
    """Register `backend` as Mnemosyne's host LLM so consolidation/extraction route
    through HiveOS (A3). Best-effort: returns False if the seam is unavailable."""
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
        log.debug("host LLM registration failed: %s", exc)
        return False


def build_mnemosyne_provider(
    *,
    home: Path,
    session_id: str = "default",
    mnemosyne_root: Path | None = None,
    host_llm: object | None = None,
) -> HiveMnemosyneProvider | None:
    """Try to build a live Mnemosyne provider; return None if unavailable.

    Args:
        home: Directory used as MNEMOSYNE_HOME (where the SQLite DB lives).
        session_id: Initial session to activate.
        mnemosyne_root: If the mnemosyne package is not installed, add this
            directory to sys.path so the local checkout is importable.
        host_llm: Optional host LLM backend (A3) — registered so Mnemosyne's
            consolidation reuses HiveOS's provider + budget instead of its own.
    """
    if mnemosyne_root is not None:
        _add_mnemosyne_to_path(mnemosyne_root)

    try:
        # The provider ships as part of the mnemosyne package.
        from mnemosyne.hermes_memory_provider import MnemosyneMemoryProvider  # type: ignore[import]
    except ImportError:
        # Try the flat import (installed from the local repo).
        try:
            from hermes_memory_provider import MnemosyneMemoryProvider  # type: ignore[import]
        except ImportError:
            log.info("mnemosyne-memory not installed; using LocalMemoryProvider fallback")
            return None

    try:
        home.mkdir(parents=True, exist_ok=True)
        if host_llm is not None and _register_host_llm(host_llm):
            log.info("Mnemosyne host LLM registered (consolidation uses HiveOS)")
        inner = MnemosyneMemoryProvider()
        inner.initialize(session_id, hermes_home=str(home))
        provider = HiveMnemosyneProvider(inner)
        log.info("Mnemosyne provider active (home=%s)", home)
        return provider
    except Exception as exc:  # noqa: BLE001
        log.warning("Mnemosyne provider init failed; falling back to local: %s", exc)
        return None
