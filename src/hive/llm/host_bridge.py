"""
host_bridge.py — let Mnemosyne's consolidation use HiveOS's LLM (A3).

Mnemosyne exposes a host-LLM seam (`mnemosyne.core.llm_backends.set_host_llm_backend`):
register a backend with a SYNC `complete(prompt, *, max_tokens, temperature, timeout,
provider, model) -> str|None` and Mnemosyne routes its consolidation/extraction through
the host instead of its own LLM config — so there's one auth + one budget, not two.

The hazard this solves carefully: Mnemosyne calls `complete()` synchronously, often from
a daemon thread (sleep/consolidation). HiveOS's router + its httpx client are bound to the
main event loop; calling them from another thread/loop corrupts the client. So this bridge
owns a DEDICATED event loop in its own daemon thread, with its OWN adapter (its httpx
client binds to that loop on first use). Mnemosyne's thread submits the coroutine via
`run_coroutine_threadsafe` and blocks for the result — no shared client crosses loops.

Lives in `llm` (it builds an adapter); the memory layer receives it as an opaque backend
object and never imports llm (DAG preserved).
"""
from __future__ import annotations

import asyncio
import logging
import threading

from hive.core.types import Message, Role
from hive.llm.adapters import make_adapter
from hive.llm.adapters.base import CompletionRequest, LLMAdapter

log = logging.getLogger("hive.llm.host_bridge")


class HostLLMBridge:
    """A sync LLMBackend (Mnemosyne shape) backed by an isolated loop + own adapter."""

    name = "hiveos"

    def __init__(self, *, provider: str, base_url: str, api_key: str, model: str,
                 catalog: object | None = None, adapter: LLMAdapter | None = None) -> None:
        self._provider = provider
        self._base = base_url
        self._key = api_key
        self._model = model
        self._catalog = catalog
        self._adapter = adapter            # injectable for tests; else built lazily on the loop
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._loop.run_forever, name="hive-hostllm", daemon=True)
                self._thread.start()
            return self._loop

    async def _acomplete(self, prompt: str, max_tokens: int) -> str:
        # Built on the dedicated loop so the httpx client binds here, never the main loop.
        if self._adapter is None:
            self._adapter = make_adapter(self._provider, base_url=self._base,
                                         catalog=self._catalog)
        req = CompletionRequest(
            model=self._model, messages=[Message(role=Role.USER, content=prompt)],
            max_tokens=max_tokens, thinking=False)
        res = await self._adapter.complete(req, api_key=self._key)
        return res.text

    def complete(self, prompt: str, *, max_tokens: int = 1024, temperature: float = 0.0,
                 timeout: float = 60.0, provider: str | None = None,
                 model: str | None = None) -> str | None:
        """Sync entry point Mnemosyne calls (possibly from its own thread). Best-effort:
        returns None on any failure so consolidation degrades, never crashes."""
        if not self._key:
            return None
        loop = self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(self._acomplete(prompt, max_tokens), loop)
        try:
            return fut.result(timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - host LLM is best-effort for Mnemosyne
            log.debug("host LLM complete failed: %s", exc)
            return None

    def close(self) -> None:
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
