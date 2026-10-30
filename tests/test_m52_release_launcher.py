"""A selected release must be the code a managed launcher actually imports."""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from hive.core.deployment_ledger import DeployLedger
from hive.core.config import HiveConfig
from hive.core.installed_release import InstalledReleaseSelector
from hive.core.deployment_identity import deployment_host_key
from hive.core.release_launcher import (
    gateway_environment,
    main,
    prepare_gateway_launch,
    validate_gateway_paths,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _releases(tmp_path: Path, *, include_hive_init: bool = True) -> tuple[Path, InstalledReleaseSelector, str, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "core.autocrlf", "false")
    _git(source, "config", "user.name", "Hive test")
    _git(source, "config", "user.email", "hive@example.invalid")
    package = source / "src" / "hive" / "surfaces" / "cli"
    package.mkdir(parents=True)
    for parent in (source / "src" / "hive", source / "src" / "hive" / "surfaces", package):
        if parent.name == "hive" and not include_hive_init:
            continue
        (parent / "__init__.py").write_text("", encoding="utf-8")
    entrypoint = package / "__main__.py"
    entrypoint.write_text("print('release-one')\n", encoding="utf-8")
    _git(source, "add", "src")
    _git(source, "commit", "-qm", "release one")
    first = _git(source, "rev-parse", "HEAD")
    entrypoint.write_text("print('release-two')\n", encoding="utf-8")
    _git(source, "commit", "-qam", "release two")
    second = _git(source, "rev-parse", "HEAD")
    root = tmp_path / "installed"
    (root / "releases").mkdir(parents=True)
    for sha in (first, second):
        _git(source, "worktree", "add", "--detach", "-q", str(root / "releases" / sha), sha)
    selector = InstalledReleaseSelector(root, DeployLedger(tmp_path / "state.sqlite"),
                                        host_key="host-a", scope="user")
    return root, selector, first, second


def test_gateway_runs_exact_selected_release_in_real_child(tmp_path):
    _root, selector, first, second = _releases(tmp_path)
    for sha, expected in ((first, "release-one"), (second, "release-two")):
        selector.select_candidate(sha, expected_selected=None if sha == first else first)
        plan = prepare_gateway_launch(selector)
        assert plan.sha == sha
        assert plan.root.name == sha
        assert plan.argv[1:3] == ("-I", "-B")
        result = subprocess.run(plan.argv, cwd=plan.root, env=gateway_environment(),
                                capture_output=True, text=True, timeout=10, check=True)
        assert result.stdout.strip() == expected


def test_missing_hive_package_cannot_fall_back_to_host_install(tmp_path):
    _root, selector, first, _second = _releases(tmp_path, include_hive_init=False)
    selector.select_candidate(first, expected_selected=None)
    with pytest.raises(ValueError, match="complete gateway CLI package"):
        prepare_gateway_launch(selector)


def test_missing_or_dirty_selection_refuses_launch(tmp_path):
    root, selector, first, _second = _releases(tmp_path)
    with pytest.raises(ValueError, match="no installed release"):
        prepare_gateway_launch(selector)
    selector.select_candidate(first, expected_selected=None)
    (root / "releases" / first / "src" / "hive" / "surfaces" / "cli" / "__main__.py").write_text(
        "print('changed')\n", encoding="utf-8",
    )
    with pytest.raises(ValueError, match="clean installed release"):
        prepare_gateway_launch(selector)


def test_interpreter_must_be_absolute_and_outside_releases(tmp_path):
    root, selector, first, _second = _releases(tmp_path)
    selector.select_candidate(first, expected_selected=None)
    with pytest.raises(ValueError, match="installed absolute file"):
        prepare_gateway_launch(selector, interpreter="python")
    with pytest.raises(ValueError, match="outside installed releases"):
        prepare_gateway_launch(selector, interpreter=root / "releases" / first / "src" /
                               "hive" / "surfaces" / "cli" / "__main__.py")


def test_symlinked_venv_interpreter_keeps_venv_path(tmp_path):
    _root, selector, first, _second = _releases(tmp_path)
    selector.select_candidate(first, expected_selected=None)
    venv_root = tmp_path / "host-venv"
    try:
        venv.EnvBuilder(with_pip=False, symlinks=True).create(venv_root)
    except (OSError, NotImplementedError):
        pytest.skip("symlinked virtual environments unavailable to this test account")
    executable = venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not executable.is_symlink():
        pytest.skip("this platform copied the virtualenv interpreter")
    plan = prepare_gateway_launch(selector, interpreter=executable)
    assert plan.executable == executable
    assert plan.argv[0] == str(executable)
    result = subprocess.run([str(executable), "-I", "-B", "-c", "import sys; print(sys.prefix)"],
                            capture_output=True, text=True, timeout=10, check=True)
    assert Path(result.stdout.strip()) == venv_root


def test_gateway_env_preserves_service_config_but_drops_loader_overrides():
    env = gateway_environment({
        "HIVE_HOST": "127.0.0.1", "HIVE_PORT": "8088",
        "PYTHONPATH": "malicious", "PythonHome": "malicious", "LD_PRELOAD": "malicious",
        "DYLD_INSERT_LIBRARIES": "malicious", "PATH": "safe-path",
    })
    assert env == {
        "HIVE_HOST": "127.0.0.1", "HIVE_PORT": "8088",
        "PATH": "safe-path", "PYTHONDONTWRITEBYTECODE": "1",
    }


def test_launcher_execs_selected_plan_without_shell(tmp_path, monkeypatch):
    root, selector, first, _second = _releases(tmp_path)
    selector.select_candidate(first, expected_selected=None)
    _gateway_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("HIVE_REPO_ROOT", str(tmp_path / "foreign-root"))
    calls = []
    monkeypatch.setattr(os, "chdir", lambda path: calls.append(("cwd", Path(path))))

    class ExecCalled(Exception):
        pass

    def fake_exec(executable, argv, env):
        calls.append(("exec", Path(executable), tuple(argv), env))
        raise ExecCalled

    monkeypatch.setattr(os, "execve", fake_exec)
    with pytest.raises(ExecCalled):
        main(["--release-root", str(root), "--state-db", str(tmp_path / "state.sqlite"),
              "--host-key", "host-a", "--scope", "user"])
    assert calls[0] == ("cwd", root / "releases" / first)
    assert calls[1][1] == Path(sys.executable)
    assert calls[1][2][1:3] == ("-I", "-B")
    assert calls[1][2][-2:] == (
        str(root / "releases" / first / "src"),
        str(root / "releases" / first / "src" / "hive" / "__init__.py"),
    )
    assert calls[1][3]["MNEMOSYNE_HOME"] == str((tmp_path / "data" / "mnemosyne").resolve())
    assert calls[1][3]["HIVE_REPO_ROOT"] == str(root / "releases" / first)


def _gateway_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HIVE_STATE_DB", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("HIVE_ENV_FILE", str(tmp_path / ".env"))


def test_gateway_data_paths_must_match_and_stay_outside_release(tmp_path, monkeypatch):
    root, _selector, _first, _second = _releases(tmp_path)
    _gateway_paths(monkeypatch, tmp_path)
    assert validate_gateway_paths(root, tmp_path / "state.sqlite", os.environ) == {
        "MNEMOSYNE_HOME": str((tmp_path / "data" / "mnemosyne").resolve()),
    }
    monkeypatch.delenv("HIVE_DATA_DIR")
    with pytest.raises(ValueError, match="explicitly configured"):
        validate_gateway_paths(root, tmp_path / "state.sqlite", os.environ)
    monkeypatch.setenv("HIVE_DATA_DIR", str(root / "data"))
    with pytest.raises(ValueError, match="outside installed releases"):
        validate_gateway_paths(root, tmp_path / "state.sqlite", os.environ)
    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HIVE_STATE_DB", str(tmp_path / "other.sqlite"))
    with pytest.raises(ValueError, match="databases must match"):
        validate_gateway_paths(root, tmp_path / "state.sqlite", os.environ)
    monkeypatch.setenv("HIVE_STATE_DB", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("MNEMOSYNE_HOME", str(root / "mnemosyne"))
    with pytest.raises(ValueError, match="memory path"):
        validate_gateway_paths(root, tmp_path / "state.sqlite", os.environ)
    monkeypatch.setenv("MNEMOSYNE_HOME", "")
    with pytest.raises(ValueError, match="memory path"):
        validate_gateway_paths(root, tmp_path / "state.sqlite", os.environ)


def test_external_dotenv_cannot_redirect_pinned_memory_path(tmp_path, monkeypatch):
    root, _selector, first, _second = _releases(tmp_path)
    _gateway_paths(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text(f"MNEMOSYNE_HOME={root / 'releases' / first}\n", encoding="utf-8")
    monkeypatch.delenv("MNEMOSYNE_HOME", raising=False)
    pinned = validate_gateway_paths(root, tmp_path / "state.sqlite", os.environ)
    monkeypatch.setenv("MNEMOSYNE_HOME", pinned["MNEMOSYNE_HOME"])
    cfg = HiveConfig.from_env(root=root / "releases" / first)
    assert cfg.mnemosyne_home == tmp_path / "data" / "mnemosyne"


def test_launcher_refuses_database_inside_release_root(tmp_path, monkeypatch, capsys):
    root, _selector, _first, _second = _releases(tmp_path)
    monkeypatch.setattr(os, "execve", lambda *_args: pytest.fail("must not exec"))
    with pytest.raises(SystemExit) as exc:
        main(["--release-root", str(root), "--state-db", str(root / "state.sqlite"),
              "--host-key", "host-a", "--scope", "user"])
    assert exc.value.code == 1
    assert capsys.readouterr().err == "selected release launch refused\n"


def test_launcher_refuses_symlinked_release_root(tmp_path, monkeypatch, capsys):
    root, selector, first, _second = _releases(tmp_path)
    selector.select_candidate(first, expected_selected=None)
    alias = tmp_path / "release-alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable to this test account")
    _gateway_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(os, "execve", lambda *_args: pytest.fail("must not exec"))
    with pytest.raises(SystemExit) as exc:
        main(["--release-root", str(alias), "--state-db", str(tmp_path / "state.sqlite"),
              "--host-key", "host-a", "--scope", "user"])
    assert exc.value.code == 1
    assert capsys.readouterr().err == "selected release launch refused\n"


def test_host_credential_pins_selected_release_and_child_mode(tmp_path, monkeypatch):
    root, _selector, first, _second = _releases(tmp_path)
    host_key = deployment_host_key(root)
    selector = InstalledReleaseSelector(
        root, DeployLedger(tmp_path / "state.sqlite"), host_key=host_key, scope="user",
    )
    selector.select_candidate(first, expected_selected=None)
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "selected_release").write_bytes(first.encode("ascii") + b"\n")
    _gateway_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    captured = []

    class ExecCalled(Exception):
        pass

    def fake_exec(_executable, _argv, env):
        captured.append(env)
        raise ExecCalled

    monkeypatch.setattr(os, "execve", fake_exec)
    with pytest.raises(ExecCalled):
        main(["--release-root", str(root), "--state-db", str(tmp_path / "state.sqlite"),
              "--host-key", "auto", "--scope", "user",
              "--selection-credential", "selected_release"])
    assert captured[0]["HIVE_INSTALLED_RELEASE_ROOT"] == str(root)
    assert captured[0]["HIVE_INSTALLED_RELEASE_CREDENTIAL"] == "selected_release"
    assert captured[0]["HIVE_DEPLOY_SYSTEMCTL_SCOPE"] == "user"


def test_host_credential_mismatch_refuses_exec(tmp_path, monkeypatch, capsys):
    root, _selector, first, second = _releases(tmp_path)
    selector = InstalledReleaseSelector(
        root, DeployLedger(tmp_path / "state.sqlite"),
        host_key=deployment_host_key(root), scope="user",
    )
    selector.select_candidate(first, expected_selected=None)
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "selected_release").write_bytes(second.encode("ascii") + b"\n")
    _gateway_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    monkeypatch.setattr(os, "execve", lambda *_args: pytest.fail("must not exec"))
    with pytest.raises(SystemExit) as exc:
        main(["--release-root", str(root), "--state-db", str(tmp_path / "state.sqlite"),
              "--host-key", "auto", "--scope", "user",
              "--selection-credential", "selected_release"])
    assert exc.value.code == 1
    assert capsys.readouterr().err == "selected release launch refused\n"
