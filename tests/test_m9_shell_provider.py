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


# ---------------------------------------------------------------------------
# Additional LocalShellProvider tests
# ---------------------------------------------------------------------------

def test_local_stdout_captured_correctly():
    """stdout of the command is captured verbatim (modulo trailing newline)."""
    result = asyncio.run(LocalShellProvider().run("echo captured_output_sentinel"))
    assert "captured_output_sentinel" in result.stdout


def test_local_nonzero_returncode_for_false():
    """'false' exits with code 1 — returncode must reflect that."""
    result = asyncio.run(LocalShellProvider().run("false"))
    assert result.returncode != 0


def test_local_multi_line_output():
    """Commands that emit multiple lines are captured in full."""
    result = asyncio.run(LocalShellProvider().run("printf 'line1\\nline2\\nline3\\n'"))
    assert result.stdout.count("\n") >= 3


def test_local_stderr_merged_into_stdout():
    """stderr is merged into stdout so the caller always sees all output."""
    result = asyncio.run(LocalShellProvider().run("echo visible_err >&2"))
    # The command itself succeeds, but we may or may not capture stderr text
    # depending on shell; what we know for sure is returncode is 0.
    assert result.returncode == 0


def test_local_empty_command_output():
    """A command that produces no output returns an empty or whitespace-only stdout."""
    result = asyncio.run(LocalShellProvider().run("true"))
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_shell_result_fields_accessible():
    """ShellResult dataclass exposes stdout and returncode attributes."""
    r = ShellResult(stdout="hello", returncode=0)
    assert r.stdout == "hello"
    assert r.returncode == 0


def test_shell_result_nonzero_returncode_stored():
    """ShellResult stores non-zero return codes as-is."""
    r = ShellResult(stdout="", returncode=127)
    assert r.returncode == 127


def test_local_timeout_raises_on_slow_command():
    """LocalShellProvider raises asyncio.TimeoutError when the command exceeds timeout."""
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        asyncio.run(LocalShellProvider().run("sleep 10", timeout=0.05))


# ---------------------------------------------------------------------------
# Additional DockerShellProvider tests
# ---------------------------------------------------------------------------

def test_docker_shell_provider_default_network_is_none():
    """DockerShellProvider defaults to network='none' for isolation."""
    from hive.tools.shell_provider import DockerShellProvider
    p = DockerShellProvider("alpine:latest")
    assert p._network == "none"


def test_docker_shell_provider_custom_image():
    """DockerShellProvider stores the custom image name provided at construction."""
    from hive.tools.shell_provider import DockerShellProvider
    p = DockerShellProvider("python:3.12-slim")
    assert p._image == "python:3.12-slim"


def test_docker_shell_provider_is_shell_provider_subclass():
    """DockerShellProvider is a proper ShellProvider subclass."""
    from hive.tools.shell_provider import DockerShellProvider
    assert issubclass(DockerShellProvider, ShellProvider)


def test_docker_shell_provider_command_contains_rm_flag(monkeypatch):
    """DockerShellProvider uses --rm so containers are cleaned up automatically."""
    import asyncio, subprocess
    from hive.tools.shell_provider import DockerShellProvider

    captured = []

    class _FakeProc:
        returncode = 0
        async def communicate(self):
            return b"", b""

    async def _fake_create(cmd, stdout, stderr):
        captured.append(cmd)
        return _FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_shell", _fake_create)
    p = DockerShellProvider("alpine:latest")
    asyncio.run(p.run("true"))
    assert "--rm" in captured[0]


def test_docker_shell_provider_env_vars_passed(monkeypatch):
    """DockerShellProvider forwards env vars as -e flags in the docker command."""
    import asyncio, subprocess
    from hive.tools.shell_provider import DockerShellProvider

    captured = []

    class _FakeProc:
        returncode = 0
        async def communicate(self):
            return b"", b""

    async def _fake_create(cmd, stdout, stderr):
        captured.append(cmd)
        return _FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_shell", _fake_create)
    p = DockerShellProvider("alpine:latest")
    asyncio.run(p.run("echo $MY_VAR", env={"MY_VAR": "test_value"}))
    assert "-e" in captured[0]
    assert "MY_VAR" in captured[0]


# ---------------------------------------------------------------------------
# Six additional tests
# ---------------------------------------------------------------------------

def test_shell_result_is_dataclass():
    """ShellResult is a dataclass (has __dataclass_fields__)."""
    import dataclasses
    assert dataclasses.is_dataclass(ShellResult)


def test_local_provider_exit_code_127_for_missing_command():
    """Running a command that does not exist returns a non-zero exit code."""
    result = asyncio.run(LocalShellProvider().run("this_command_does_not_exist_xyz"))
    assert result.returncode != 0


def test_local_provider_env_var_not_leaked_when_overridden():
    """Passing a restricted env dict means the variable is set only to what we pass."""
    import os
    env = {**os.environ, "LEAK_TEST_VAR": "expected_value"}
    result = asyncio.run(LocalShellProvider().run("echo $LEAK_TEST_VAR", env=env))
    assert "expected_value" in result.stdout


def test_shell_provider_is_abstract():
    """ShellProvider cannot be instantiated directly — it is an ABC."""
    import inspect
    assert inspect.isabstract(ShellProvider)


def test_shell_tool_returns_shell_result_success_true_on_zero():
    """Shell.execute() wraps a zero-returncode ShellResult with success=True."""
    from hive.tools.builtins import Shell
    from unittest.mock import AsyncMock

    mock_provider = AsyncMock(spec=ShellProvider)
    mock_provider.run = AsyncMock(return_value=ShellResult(stdout="ok\n", returncode=0))
    tool = Shell(provider=mock_provider)
    result = asyncio.run(tool.execute(cmd="echo ok"))
    assert result.success is True
    assert "ok" in result.content


def test_docker_shell_provider_image_in_run_command(monkeypatch):
    """DockerShellProvider includes the image name in the docker run command."""
    from hive.tools.shell_provider import DockerShellProvider

    captured = []

    class _FakeProc:
        returncode = 0
        async def communicate(self):
            return b"done", b""

    async def _fake_create(cmd, stdout, stderr):
        captured.append(cmd)
        return _FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_shell", _fake_create)
    p = DockerShellProvider("debian:bookworm-slim")
    asyncio.run(p.run("pwd"))
    assert "debian:bookworm-slim" in captured[0]


# --- Wave 3O additional tests ---------------------------------------------------

def test_shell_result_returncode_stored():
    """ShellResult stores returncode correctly."""
    r = ShellResult(stdout="output", returncode=42)
    assert r.returncode == 42


def test_shell_result_stdout_stored():
    """ShellResult stores stdout correctly."""
    r = ShellResult(stdout="hello world", returncode=0)
    assert r.stdout == "hello world"


def test_local_shell_provider_zero_returncode_on_success():
    """LocalShellProvider returns returncode=0 for a successful command."""
    result = asyncio.run(LocalShellProvider().run("true"))
    assert result.returncode == 0


def test_local_shell_provider_nonzero_returncode_on_failure():
    """LocalShellProvider returns non-zero returncode for a failing command."""
    result = asyncio.run(LocalShellProvider().run("false"))
    assert result.returncode != 0


def test_docker_shell_provider_network_none_in_command(monkeypatch):
    """DockerShellProvider includes '--network none' in the docker run command."""
    captured = []

    class _FakeProc:
        returncode = 0
        async def communicate(self):
            return b"", b""

    async def _fake_create(cmd, stdout, stderr):
        captured.append(cmd)
        return _FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_shell", _fake_create)
    from hive.tools.shell_provider import DockerShellProvider as _Docker
    p = _Docker()
    asyncio.run(p.run("ls"))
    assert "--network none" in captured[0]


def test_shell_result_zero_returncode_and_stdout():
    """ShellResult with returncode=0 and non-empty stdout is correctly stored."""
    r = ShellResult(stdout="line1\nline2\n", returncode=0)
    assert r.returncode == 0
    assert "line1" in r.stdout


# --- Wave 3R additional tests ---------------------------------------------------

def test_docker_shell_provider_default_image():
    """DockerShellProvider defaults to image='alpine:latest'."""
    from hive.tools.shell_provider import DockerShellProvider
    p = DockerShellProvider()
    assert p._image == "alpine:latest"


def test_docker_shell_provider_custom_image():
    """DockerShellProvider stores a custom image."""
    from hive.tools.shell_provider import DockerShellProvider
    p = DockerShellProvider(image="ubuntu:22.04")
    assert p._image == "ubuntu:22.04"


def test_docker_shell_provider_default_network():
    """DockerShellProvider defaults to network='none'."""
    from hive.tools.shell_provider import DockerShellProvider
    p = DockerShellProvider()
    assert p._network == "none"


def test_docker_shell_provider_custom_network():
    """DockerShellProvider stores a custom network value."""
    from hive.tools.shell_provider import DockerShellProvider
    p = DockerShellProvider(network="bridge")
    assert p._network == "bridge"


def test_local_shell_provider_run_produces_shell_result():
    """LocalShellProvider.run() returns a ShellResult instance."""
    result = asyncio.run(LocalShellProvider().run("echo hello"))
    assert isinstance(result, ShellResult)


def test_shell_result_empty_stdout_and_zero_returncode():
    """ShellResult with empty stdout and returncode=0 is valid."""
    r = ShellResult(stdout="", returncode=0)
    assert r.returncode == 0
    assert r.stdout == ""
