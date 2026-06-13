"""M9-c — terminal-environment abstraction: ShellProvider ABC + LocalShellProvider."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from hive.tools.shell_provider import LocalShellProvider, ShellProvider, ShellResult


# ---------------------------------------------------------------------------
# LocalShellProvider
# ---------------------------------------------------------------------------

def test_local_runs_echo():
    result = asyncio.run(LocalShellProvider().run("echo hello"))
    assert "hello" in result.stdout
    assert result.returncode == 0


def test_local_captures_stderr_via_stdout():
    result = asyncio.run(LocalShellProvider().run("echo err >&2"))
    assert result.returncode == 0  # stderr merged into stdout


def test_local_nonzero_returncode():
    result = asyncio.run(LocalShellProvider().run("exit 42"))
    assert result.returncode == 42


def test_local_is_shell_provider_subclass():
    assert issubclass(LocalShellProvider, ShellProvider)


# ---------------------------------------------------------------------------
# Shell tool delegates to injected provider
# ---------------------------------------------------------------------------

def test_shell_tool_delegates_to_provider():
    from hive.tools.builtins import Shell

    mock_provider = AsyncMock(spec=ShellProvider)
    mock_provider.run = AsyncMock(return_value=ShellResult(stdout="mocked\n", returncode=0))

    tool = Shell(provider=mock_provider)
    result = asyncio.run(tool.execute(cmd="anything"))

    mock_provider.run.assert_called_once_with("anything")
    assert result.content == "mocked\n"
    assert result.success is True


def test_shell_tool_reports_nonzero_as_failure():
    from hive.tools.builtins import Shell

    mock_provider = AsyncMock(spec=ShellProvider)
    mock_provider.run = AsyncMock(return_value=ShellResult(stdout="bad", returncode=1))

    tool = Shell(provider=mock_provider)
    result = asyncio.run(tool.execute(cmd="fail"))
    assert result.success is False


def test_shell_tool_default_provider_is_local():
    from hive.tools.builtins import Shell
    assert isinstance(Shell()._provider, LocalShellProvider)


# ---------------------------------------------------------------------------
# Custom provider (future: Docker, SSH)
# ---------------------------------------------------------------------------

def test_custom_provider_can_be_injected():
    """Shows the extension point works — a container provider would look like this."""
    from hive.tools.builtins import Shell

    class _EchoProvider(ShellProvider):
        async def run(self, cmd: str, *, timeout: float = 30.0) -> ShellResult:
            return ShellResult(stdout=f"[container] {cmd}", returncode=0)

    tool = Shell(provider=_EchoProvider())
    result = asyncio.run(tool.execute(cmd="ls /tmp"))
    assert result.content == "[container] ls /tmp"
    assert result.success is True
