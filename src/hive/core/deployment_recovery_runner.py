"""Single fixed-argv process boundary for same-revision gateway recovery."""

from __future__ import annotations

import asyncio

from hive.core.child_env import without_privileged_credentials


async def restart_gateway_systemctl(scope: str) -> bool:
    """Restart only the configured local gateway service without exposing output."""
    if scope not in {"system", "user"}:
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            "systemctl", f"--{scope}", "restart", "hiveos-gateway.service",
            env=without_privileged_credentials(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        return await asyncio.wait_for(process.wait(), timeout=30) == 0
    except asyncio.TimeoutError:
        process.kill()
        await asyncio.shield(process.wait())
        return False
