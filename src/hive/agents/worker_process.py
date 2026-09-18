"""Platform containment for supervisor-owned local specialist workers.

The worker protocol deliberately gives the child no credentials or direct tools.
This module supplies the separate lifecycle boundary: a worker is placed in a
platform process container and cancellation terminates that container, not only
the immediate Python process.
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import signal
import subprocess
from typing import Any

from hive.core.worker_isolation import (
    ISOLATION_MODES,
    WorkerIsolationCapability,
    worker_isolation_capability,
)

log = logging.getLogger(__name__)


class WorkerContainmentUnavailable(RuntimeError):
    """Raised when a required platform containment primitive is unavailable."""


class _WindowsJob:
    """Minimal Windows Job Object owner, loaded only on Windows hosts."""

    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self) -> None:
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        self._kernel32.OpenProcess.restype = ctypes.c_void_p
        self._job = self._kernel32.CreateJobObjectW(None, None)
        if not self._job:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        try:
            self._set_kill_on_close()
        except Exception:
            self.close()
            raise

    def _set_kill_on_close(self) -> None:
        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_ulong),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_ulong),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_ulong),
                ("SchedulingClass", ctypes.c_ulong),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        limits = _ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = self._kernel32.SetInformationJobObject(
            self._job, self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits), ctypes.sizeof(limits),
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def assign(self, pid: int) -> None:
        process = self._kernel32.OpenProcess(
            self._PROCESS_SET_QUOTA | self._PROCESS_TERMINATE, False, pid,
        )
        if not process:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            if not self._kernel32.AssignProcessToJobObject(self._job, process):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        finally:
            self._kernel32.CloseHandle(process)

    def terminate(self) -> None:
        if self._job and not self._kernel32.TerminateJobObject(self._job, 1):
            raise OSError(ctypes.get_last_error(), "TerminateJobObject failed")

    def close(self) -> None:
        if self._job:
            self._kernel32.CloseHandle(self._job)
            self._job = None


class WorkerProcessController:
    """Launch and stop a worker with an explicit isolation policy.

    ``required`` never starts an uncontained child.  ``preferred`` permits the
    historical direct-process fallback for supervised development only; callers
    must reject it before autonomy is enabled.  ``off`` is intentionally a
    development/test escape hatch and is likewise rejected by configuration for
    autonomy.
    """

    def __init__(self, mode: str = "preferred") -> None:
        normalized = str(mode).strip().lower()
        if normalized not in ISOLATION_MODES:
            raise ValueError("worker isolation mode must be required, preferred, or off")
        self.mode = normalized
        self.capability = worker_isolation_capability()
        self._job: _WindowsJob | None = None
        # A POSIX session leader may exit before a supervisor notices a failed
        # protocol/timeout.  Keep its PGID separately so cleanup still reaches
        # surviving descendants after ``proc.returncode`` becomes non-None.
        self._pgid: int | None = None

    @property
    def effective_level(self) -> str:
        if self.mode == "off":
            return "off"
        return self.capability.level if self.capability.available else "bounded"

    async def start(self, *args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        """Start a child and atomically attach it to containment when available."""
        if self.mode == "required" and not self.capability.available:
            raise WorkerContainmentUnavailable(self.capability.detail)
        if self.mode == "preferred" and not self.capability.available:
            log.warning("worker process-tree containment unavailable; supervised fallback is bounded")

        if self.capability.available and os.name == "posix":
            kwargs["start_new_session"] = True
        if self.capability.available and os.name == "nt":
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0,
            )
        proc = await asyncio.create_subprocess_exec(*args, **kwargs)
        if self.capability.available and os.name == "posix":
            self._pgid = proc.pid
        if self.capability.available and os.name == "nt":
            try:
                self._job = _WindowsJob()
                self._job.assign(proc.pid)
            except Exception as exc:
                if self.mode == "required":
                    # The Job Object exists but the child was not assigned to
                    # it.  Closing it first is essential: otherwise stop()
                    # would terminate the empty Job Object and wait forever on
                    # the uncontained child.
                    self.close()
                    await self.stop(proc)
                    raise WorkerContainmentUnavailable("could not attach worker to Windows Job Object") from exc
                self.close()
                log.warning("worker Job Object attachment failed; supervised fallback is bounded")
        return proc

    async def stop(self, proc: asyncio.subprocess.Process) -> None:
        """Idempotently stop the controlled worker and its contained descendants."""
        try:
            if self._job is not None:
                self._job.terminate()
            elif self.capability.available and os.name == "posix" and self._pgid is not None:
                try:
                    os.killpg(self._pgid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            elif proc.returncode is None:
                proc.terminate()
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    if self.capability.available and os.name == "posix" and self._pgid is not None:
                        try:
                            os.killpg(self._pgid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    elif self._job is not None:
                        self._job.terminate()
                    else:
                        proc.kill()
                    await proc.wait()
            # The leader may already be reaped while a descendant ignores
            # SIGTERM.  Escalate the saved process group without relying on
            # the leader's return code.
            if self.capability.available and os.name == "posix" and self._pgid is not None:
                try:
                    os.killpg(self._pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        finally:
            self.close()

    def close(self) -> None:
        if self._job is not None:
            self._job.close()
            self._job = None
        self._pgid = None


__all__ = [
    "ISOLATION_MODES", "WorkerContainmentUnavailable", "WorkerIsolationCapability",
    "WorkerProcessController", "worker_isolation_capability",
]
