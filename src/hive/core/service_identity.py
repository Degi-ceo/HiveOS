"""Read-only systemd ownership check for the local gateway process."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable

from hive.core.child_env import minimal_worker_environment


def is_managed_gateway_process(
    *, pid: int | None = None, platform: str | None = None,
    scope: str = "system",
    runner: Callable = subprocess.run,
) -> bool:
    """Accept only a process that systemd names as gateway.service MainPID.

    Probe only the explicitly selected manager, matching the restart command.
    Missing systemd, inaccessible managers, and malformed output fail closed.
    No shell is involved and the child sees only bus/launch variables.
    """
    if (platform or sys.platform) != "linux" or scope not in {"user", "system"}:
        return False
    actual_pid = os.getpid() if pid is None else pid
    if isinstance(actual_pid, bool) or not isinstance(actual_pid, int) or actual_pid <= 0:
        return False
    env = minimal_worker_environment()
    for name in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        if name in os.environ:
            env[name] = os.environ[name]
    try:
        result = runner(
            ("systemctl", f"--{scope}", "show", "--property=MainPID", "--value",
             "hiveos-gateway.service"),
            capture_output=True, text=True, encoding="ascii", errors="replace",
            timeout=2, check=False, env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    value = result.stdout.strip() if result.returncode == 0 else ""
    return value.isascii() and value.isdecimal() and int(value) == actual_pid
