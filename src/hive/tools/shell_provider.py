"""
shell_provider.py — terminal-environment abstraction (M9-c, Hermes #11).

Decouples the Shell builtin from the local subprocess so execution can be
rerouted to a container or remote host without changing the tool contract.
Default: LocalShellProvider (wraps asyncio.create_subprocess_shell).
"""
from __future__ import annotations

import asyncio
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(slots=True)
class ShellResult:
    stdout: str
    returncode: int


class ShellProvider(ABC):
    """Execute a shell command and return its combined stdout/stderr."""

    @abstractmethod
    async def run(self, cmd: str, *, timeout: float = 30.0) -> ShellResult:
        ...


class LocalShellProvider(ShellProvider):
    """Run commands in the local process environment via asyncio subprocess."""

    async def run(self, cmd: str, *, timeout: float = 30.0) -> ShellResult:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return ShellResult(
            stdout=stdout.decode(errors="replace"),
            returncode=proc.returncode if proc.returncode is not None else 0,
        )
