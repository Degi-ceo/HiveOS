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


def test_local_provider_passes_env():
    """env kwarg overrides the process environment for the subprocess."""
    import os
    result = asyncio.run(LocalShellProvider().run(
        "echo $HIVE_TEST_VAR",
        env={**os.environ, "HIVE_TEST_VAR": "sentinel_value"},
    ))
    assert "sentinel_value" in result.stdout


def test_local_provider_none_env_inherits_parent():
    """env=None (default) inherits the parent process environment."""
    import os
    os.environ["HIVE_INHERIT_TEST"] = "inherited"
    result = asyncio.run(LocalShellProvider().run("echo $HIVE_INHERIT_TEST"))
    assert "inherited" in result.stdout


# ---------------------------------------------------------------------------
# Custom provider (future: Docker, SSH)
# ---------------------------------------------------------------------------

def test_custom_provider_can_be_injected():
    """Shows the extension point works — a container provider would look like this."""
    from hive.tools.builtins import Shell

    class _EchoProvider(ShellProvider):
        async def run(self, cmd: str, *, timeout: float = 30.0,
                      env: dict | None = None) -> ShellResult:
            return ShellResult(stdout=f"[container] {cmd}", returncode=0)

    tool = Shell(provider=_EchoProvider())
    result = asyncio.run(tool.execute(cmd="ls /tmp"))
    assert result.content == "[container] ls /tmp"
    assert result.success is True


# --- N-2: DockerShellProvider -------------------------------------------------

def test_docker_shell_provider_builds():
    from hive.tools.shell_provider import DockerShellProvider
    p = DockerShellProvider("python:3.11", network="bridge")
    assert p._image == "python:3.11"
    assert p._network == "bridge"


def test_docker_shell_provider_network_isolation(monkeypatch):
    """DockerShellProvider issues 'docker run --network none' in the shell command."""
    import asyncio, subprocess
    from hive.tools.shell_provider import DockerShellProvider, ShellResult

    captured_cmds = []

    class _FakeProc:
        returncode = 0
        async def communicate(self):
            return b"output", b""

    async def _fake_create(cmd, stdout, stderr):
        captured_cmds.append(cmd)
        return _FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_shell", _fake_create)

    p = DockerShellProvider("alpine:latest", network="none")
    result = asyncio.run(p.run("echo hello"))
    assert len(captured_cmds) == 1
    assert "--network none" in captured_cmds[0]
    assert "alpine:latest" in captured_cmds[0]
    assert result.returncode == 0
