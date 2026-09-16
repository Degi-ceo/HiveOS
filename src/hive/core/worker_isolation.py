"""Core-only capability detection for local worker process containment."""
from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass

ISOLATION_MODES = frozenset({"required", "preferred", "off"})


@dataclass(frozen=True, slots=True)
class WorkerIsolationCapability:
    """Safe operator-facing description of the local worker boundary."""

    available: bool
    level: str
    backend: str
    detail: str


def worker_isolation_capability() -> WorkerIsolationCapability:
    """Report whether this host can contain a worker process tree."""
    if os.name == "nt":
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            for name in ("CreateJobObjectW", "AssignProcessToJobObject", "TerminateJobObject"):
                if not getattr(kernel32, name, None):
                    raise OSError(f"missing {name}")
        except (AttributeError, OSError):
            return WorkerIsolationCapability(False, "unavailable", "windows-job-object",
                                             "Windows Job Objects are unavailable")
        return WorkerIsolationCapability(True, "strong", "windows-job-object",
                                         "process-tree termination via Windows Job Object")
    if os.name == "posix" and hasattr(os, "killpg"):
        return WorkerIsolationCapability(True, "strong", "posix-process-group",
                                         "process-tree termination via POSIX process group")
    return WorkerIsolationCapability(False, "unavailable", "none",
                                     "no supported worker process-tree containment backend")


__all__ = ["ISOLATION_MODES", "WorkerIsolationCapability", "worker_isolation_capability"]
