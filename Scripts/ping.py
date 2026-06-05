"""ping.py — Phase 1 smoke test. Run: python -m scripts.ping"""
import asyncio
from core import settings
from core.model_router import router, TaskKind


async def main() -> None:
    print(f"\u2713 SOUL loaded ({len(settings.SOUL)} chars)")
    print(f"  exec model: {settings.EXEC_MODEL}  endpoint: {settings.MINIMAX_ANTHROPIC_BASE}")
    reply = await router.complete(
        [{"role": "user", "content": "Reply in one short line confirming you are online."}],
        kind=TaskKind.EXECUTE, system=settings.SOUL, thinking=False, max_tokens=128)
    print("Hive:", reply)
    await router.aclose()


if __name__ == "__main__":
    asyncio.run(main())
