"""Safe CLI onboarding through the public command and configuration seam."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from hive.core.child_env import minimal_worker_environment
from hive.core.config import HiveConfig
from hive.surfaces.cli import _init, main
from hive.surfaces.cli.init_config import _unsafe_link


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_noninteractive_json_init_roundtrips_from_other_cwd(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    env_file = config_dir / ".env"
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    monkeypatch.chdir(other_dir)
    monkeypatch.setenv("HIVE_ENV_FILE", str(env_file))
    monkeypatch.setenv("MINIMAX_API_KEY", "test-minimax-key")
    monkeypatch.delenv("HIVE_SECRET", raising=False)
    monkeypatch.delenv("MNEMOSYNE_HOME", raising=False)

    assert main(["init", "--non-interactive", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"ok": True, "changed": True, "env_file": str(env_file)}
    assert env_file.is_file()
    assert not (other_dir / ".env").exists()
    assert "test-minimax-key" in env_file.read_text(encoding="utf-8")

    before = env_file.read_bytes()
    assert main(["init", "--non-interactive", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] is False
    assert env_file.read_bytes() == before

    monkeypatch.delenv("MINIMAX_API_KEY")
    monkeypatch.delenv("HIVE_SECRET", raising=False)
    config = HiveConfig.from_env(root=tmp_path)
    assert config.minimax_api_key == "test-minimax-key"
    assert config.secret not in {"", "change_me", "change-me"}


def test_default_path_uses_repo_root_not_current_directory(tmp_path, monkeypatch, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("HIVE_ENV_FILE", raising=False)
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    monkeypatch.setattr("hive.surfaces.cli.init_config.REPO_ROOT", root)
    assert main(["init", "--non-interactive", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["env_file"] == str(root / ".env")
    assert (root / ".env").is_file()
    assert not (elsewhere / ".env").exists()


def test_interactive_cancel_leaves_no_partial_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    (tmp_path / ".env.example").write_text("HIVE_SECRET=change_me\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIVE_ENV_FILE", str(env_file))
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.setattr(sys, "stdin", _TTY())

    def cancel(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("getpass.getpass", cancel)
    assert _init() == 130
    assert not env_file.exists()


def test_init_does_not_run_doctor_or_seed(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / ".env"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIVE_ENV_FILE", str(env_file))
    monkeypatch.setenv("MINIMAX_API_KEY", "test-minimax-key")
    monkeypatch.setattr("hive.core.doctor.run", lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("init must not run doctor")))
    assert main(["init", "--non-interactive", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_noninteractive_missing_key_does_not_write(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / ".env"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIVE_ENV_FILE", str(env_file))
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    assert main(["init", "--non-interactive", "--json"]) != 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert not env_file.exists()


def test_noninteractive_subprocess_needs_no_tty(tmp_path):
    env_file = tmp_path / ".env"
    env = minimal_worker_environment()
    env["HIVE_ENV_FILE"] = str(env_file)
    env["MINIMAX_API_KEY"] = "subprocess-test-key"
    env.pop("HIVE_SECRET", None)
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    result = subprocess.run(
        (sys.executable, "-B", "-m", "hive.surfaces.cli", "init", "--non-interactive", "--json"),
        cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr[:300]
    assert json.loads(result.stdout)["ok"] is True
    assert "subprocess-test-key" not in result.stdout + result.stderr
    assert "\x1b[" not in result.stdout


def test_placeholder_secret_is_replaced_and_other_lines_preserved(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"# keep this\r\nHIVE_SECRET=change_me\r\nCUSTOM=keep\r\n")
    monkeypatch.setenv("HIVE_ENV_FILE", str(env_file))
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    monkeypatch.delenv("HIVE_SECRET", raising=False)
    assert main(["init", "--non-interactive", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] is True
    content = env_file.read_bytes()
    assert b"# keep this\r\n" in content
    assert b"CUSTOM=keep\r\n" in content
    assert b"HIVE_SECRET=change_me" not in content
    assert b"\n" not in content.replace(b"\r\n", b"")


def test_duplicate_target_key_fails_without_rewrite(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / ".env"
    original = b"MINIMAX_API_KEY=first\nexport MINIMAX_API_KEY=second\n"
    env_file.write_bytes(original)
    monkeypatch.setenv("HIVE_ENV_FILE", str(env_file))
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    assert main(["init", "--non-interactive", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "duplicate_config_key"
    assert env_file.read_bytes() == original


def test_symlinked_env_or_ancestor_is_rejected(tmp_path, monkeypatch, capsys):
    real = tmp_path / "real"
    real.mkdir()
    env_file = real / ".env"
    env_file.write_text("CUSTOM=keep\n", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this host")
    monkeypatch.setenv("HIVE_ENV_FILE", str(link / ".env"))
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    assert main(["init", "--non-interactive", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "unsafe_env_path"
    assert env_file.read_text(encoding="utf-8") == "CUSTOM=keep\n"


def test_replace_failure_preserves_existing_file(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / ".env"
    original = b"CUSTOM=keep\n"
    env_file.write_bytes(original)
    monkeypatch.setenv("HIVE_ENV_FILE", str(env_file))
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    monkeypatch.setattr("hive.surfaces.cli.init_config.os.replace", lambda *_args: (_ for _ in ()).throw(
        OSError("simulated replacement failure")))
    assert main(["init", "--non-interactive", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "io_error"
    assert env_file.read_bytes() == original
    assert list(tmp_path.glob(".hive-env-*")) == []


def test_invalid_env_override_fails_closed(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIVE_ENV_FILE", "relative/.env")
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    assert main(["init", "--non-interactive", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "io_error"
    assert not (tmp_path / "relative" / ".env").exists()


def test_unknown_flag_still_returns_one_json_error(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / ".env"
    monkeypatch.setenv("HIVE_ENV_FILE", str(env_file))
    assert main(["init", "--non-interactive", "--json", "--bad"]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": False, "error": "invalid_arguments"}
    assert captured.err == ""
    assert not env_file.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_windows_junction_is_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    result = subprocess.run(
        ("cmd", "/c", "mklink", "/J", str(link), str(real)),
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        pytest.skip("junction creation is unavailable on this host")
    assert _unsafe_link(link)


def test_interactive_without_tty_fails_without_write(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / ".env"
    monkeypatch.setenv("HIVE_ENV_FILE", str(env_file))
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO("injected-key\n"))
    assert main(["init"]) == 1
    assert "tty_required" in capsys.readouterr().err
    assert not env_file.exists()


def test_invalid_api_key_value_cannot_inject_newline(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / ".env"
    monkeypatch.setenv("HIVE_ENV_FILE", str(env_file))
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key\nHIVE_AUTONOMY_ENABLED=true")
    assert main(["init", "--non-interactive", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_config_value"
    assert not env_file.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits do not validate Windows ACLs")
def test_new_dotenv_is_private_on_posix(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / ".env"
    monkeypatch.setenv("HIVE_ENV_FILE", str(env_file))
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    assert main(["init", "--non-interactive", "--json"]) == 0
    capsys.readouterr()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
