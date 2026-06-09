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
    """Try to build a live Mnemosyne provider; return None if unavailable.

    Args:
        home: Directory used as MNEMOSYNE_HOME (where the SQLite DB lives).
        session_id: Initial session to activate.
        mnemosyne_root: If the mnemosyne package is not installed, add this
            directory to sys.path so the local checkout is importable.
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
        inner = MnemosyneMemoryProvider()
        inner.initialize(session_id, hermes_home=str(home))
        provider = HiveMnemosyneProvider(inner)
        log.info("Mnemosyne provider active (home=%s)", home)
        return provider
    except Exception as exc:  # noqa: BLE001
        log.warning("Mnemosyne provider init failed; falling back to local: %s", exc)
        return None
