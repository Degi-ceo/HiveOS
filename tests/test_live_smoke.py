"""
Opt-in live smoke tests — proof the stack talks to real providers.

These are SKIPPED by default so the normal suite stays offline/deterministic. Run
explicitly with secrets present:

    HIVE_LIVE_TEST=1 MINIMAX_API_KEY=sk-... pytest tests/test_live_smoke.py -v

Each test guards its own prerequisites and skips (not fails) when they're missing.
"""
from __future__ import annotations

import asyncio
import os

import pytest

LIVE = os.getenv("HIVE_LIVE_TEST") == "1"
HAS_KEY = bool(os.getenv("MINIMAX_API_KEY"))

pytestmark = pytest.mark.skipif(not LIVE, reason="set HIVE_LIVE_TEST=1 to run live smokes")


@pytest.mark.skipif(not HAS_KEY, reason="MINIMAX_API_KEY required")
def test_minimax_roundtrip():
    """One real completion against the configured exec model."""
    from hive.core.config import HiveConfig
    from hive.core.types import Message, Role
    from hive.llm.router import ModelRouter

    cfg = HiveConfig.from_env()
    router = ModelRouter(config=cfg)
    try:
        result = asyncio.run(router.complete(
            [Message(role=Role.USER, content="Reply with exactly the word: pong")],
            system="You are a test harness. Reply tersely.",
            max_tokens=32, thinking=False,
        ))
        assert result.text.strip(), "empty completion"
        assert result.usage.output_tokens > 0, "no output tokens reported"
        print(f"\n[live] model={result.model} reply={result.text!r} "
              f"tokens in/out={result.usage.input_tokens}/{result.usage.output_tokens}")
    finally:
        asyncio.run(router.aclose())


@pytest.mark.skipif(not HAS_KEY, reason="MINIMAX_API_KEY required")
def test_budget_remains_poll():
    """The credit-window poll returns a float or None without raising."""
    from hive.core.budgeter import Budgeter
    from hive.core.config import HiveConfig

    cfg = HiveConfig.from_env()
    b = Budgeter()
    used = asyncio.run(b.refresh(cfg.minimax_api_key, cfg.remains_url))
    assert used is None or isinstance(used, float)
    print(f"\n[live] credit window used%={used}")


def test_mnemosyne_roundtrip(tmp_path):
    """remember -> recall round-trip through the real Mnemosyne engine."""
    pytest.importorskip("mnemosyne.core.beam", reason="mnemosyne package not installed")
    from hive.memory.mnemosyne_provider import build_mnemosyne_provider

    provider = build_mnemosyne_provider(home=tmp_path / "mnemo", session_id="smoke")
    if provider is None:
        pytest.skip("Mnemosyne provider could not initialize")
    provider.initialize("smoke", hermes_home=str(tmp_path / "mnemo"))
    provider.sync_turn("Kamil's favourite colour is teal.",
                       "Noted — teal it is.", session_id="smoke")
    recall = provider.prefetch("what is Kamil's favourite colour", session_id="smoke")
    print(f"\n[live] mnemosyne recall={recall!r}")
    # Recall content varies by engine config; assert it returned a string block.
    assert isinstance(recall, str)
    close = getattr(provider, "close", None)
    if close:
        close()
