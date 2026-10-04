"""A running gateway must never claim a commit it cannot attest locally."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from hive.core.child_env import minimal_worker_environment
from hive.core.config import HiveConfig
from hive.core.revision import detect_source_revision
from hive.gateway.app import create_app
from hive.runtime import HiveOS


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *args), capture_output=True, text=True,
        check=True, env=minimal_worker_environment(),
    )
    return result.stdout.strip()


def _source_repo(tmp_path: Path) -> Path:
    source = tmp_path / "src" / "hive"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("revision = 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'hive'\n", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "src/hive/__init__.py", "pyproject.toml")
    _git(tmp_path, "-c", "user.name=Hive", "-c", "user.email=hive@example.invalid",
         "commit", "-qm", "initial")
    return tmp_path


def test_clean_source_checkout_reports_exact_head(tmp_path):
    root = _source_repo(tmp_path)
    assert detect_source_revision(root) == _git(root, "rev-parse", "HEAD")


def test_dirty_or_untracked_source_never_reports_head(tmp_path):
    root = _source_repo(tmp_path)
    source = root / "src" / "hive" / "__init__.py"
    source.write_text("revision = 2\n", encoding="utf-8")
    assert detect_source_revision(root) is None
    source.write_text("revision = 1\n", encoding="utf-8")
    (root / "src" / "hive" / "injected.py").write_text("unsafe = True\n", encoding="utf-8")
    assert detect_source_revision(root) is None


def test_ignored_source_cannot_hide_from_revision_probe(tmp_path):
    root = _source_repo(tmp_path)
    (root / ".gitignore").write_text("src/hive/ignored.py\n", encoding="utf-8")
    _git(root, "add", ".gitignore")
    _git(root, "-c", "user.name=Hive", "-c", "user.email=hive@example.invalid",
         "commit", "-qm", "ignore marker")
    (root / "src" / "hive" / "ignored.py").write_text("revision = 999\n", encoding="utf-8")
    assert detect_source_revision(root) is None


def test_ignored_bytecode_cache_does_not_count_as_clean_source(tmp_path):
    root = _source_repo(tmp_path)
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    _git(root, "add", ".gitignore")
    _git(root, "-c", "user.name=Hive", "-c", "user.email=hive@example.invalid",
         "commit", "-qm", "ignore cache")
    cache = root / "src" / "hive" / "__pycache__"
    cache.mkdir()
    (cache / "__init__.cpython-312.pyc").write_bytes(b"untrusted bytecode")
    assert detect_source_revision(root) is None


def test_assume_unchanged_source_cannot_hide_from_revision_probe(tmp_path):
    root = _source_repo(tmp_path)
    _git(root, "update-index", "--assume-unchanged", "src/hive/__init__.py")
    (root / "src" / "hive" / "__init__.py").write_text("revision = 999\n", encoding="utf-8")
    assert detect_source_revision(root) is None


def test_skip_worktree_source_cannot_hide_from_revision_probe(tmp_path):
    root = _source_repo(tmp_path)
    _git(root, "update-index", "--skip-worktree", "src/hive/__init__.py")
    (root / "src" / "hive" / "__init__.py").write_text("revision = 999\n", encoding="utf-8")
    assert detect_source_revision(root) is None


def test_packaged_or_nested_checkout_never_reports_head(tmp_path):
    assert detect_source_revision(tmp_path) is None
    root = _source_repo(tmp_path)
    nested = root / "nested" / "src" / "hive"
    nested.mkdir(parents=True)
    assert detect_source_revision(root / "nested") is None


def test_git_probe_does_not_inherit_approver_or_repository_credentials(tmp_path, monkeypatch):
    root = _source_repo(tmp_path)
    monkeypatch.setenv("HIVE_APPROVER_KEY", "approver-test-secret")
    monkeypatch.setenv("HIVE_GITHUB_TOKEN", "github-test-secret")
    original_run = subprocess.run
    observed: list[dict[str, str]] = []

    def checked_run(*args, **kwargs):
        observed.append(kwargs["env"])
        return original_run(*args, **kwargs)

    with patch("hive.core.revision.subprocess.run", side_effect=checked_run):
        assert detect_source_revision(root) == _git(root, "rev-parse", "HEAD")
    assert observed
    assert all("HIVE_APPROVER_KEY" not in env and "HIVE_GITHUB_TOKEN" not in env for env in observed)


def test_head_change_during_probe_fails_closed(tmp_path):
    root = _source_repo(tmp_path)
    heads = iter(("a" * 40, "b" * 40))

    def moving_head(argv, **_kwargs):
        if argv[-1] == "--show-toplevel":
            output = str(root)
        elif argv[-2:] == ("--verify", "HEAD"):
            output = next(heads)
        else:
            output = ""
        return subprocess.CompletedProcess(argv, 0, output, "")

    with patch("hive.core.revision.subprocess.run", side_effect=moving_head):
        assert detect_source_revision(root) is None


def test_git_timeout_fails_closed(tmp_path):
    root = _source_repo(tmp_path)
    with patch("hive.core.revision.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 2)):
        assert detect_source_revision(root) is None


def test_fresh_no_bytecode_process_reports_its_clean_source_head(tmp_path):
    root = _source_repo(tmp_path)
    core = root / "src" / "hive" / "core"
    core.mkdir()
    (core / "__init__.py").write_text("", encoding="utf-8")
    actual_core = Path(__file__).resolve().parents[1] / "src" / "hive" / "core"
    for name in ("revision.py", "child_env.py"):
        (core / name).write_bytes((actual_core / name).read_bytes())
    _git(root, "add", "src/hive/core")
    _git(root, "-c", "user.name=Hive", "-c", "user.email=hive@example.invalid",
         "commit", "-qm", "add probe")
    env = minimal_worker_environment()
    env["PYTHONPATH"] = str(root / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        (sys.executable, "-c", "from hive.core.revision import detect_source_revision; "
         "print(detect_source_revision())"),
        cwd=root, env=env, capture_output=True, text=True, timeout=10, check=True,
    )
    assert result.stdout.strip() == _git(root, "rev-parse", "HEAD")
    assert not list((root / "src" / "hive").rglob("__pycache__"))


def test_gateway_systemd_unit_disables_bytecode_writes():
    unit = Path(__file__).resolve().parents[1] / "deploy" / "hiveos-gateway.service"
    content = unit.read_text(encoding="utf-8")
    assert "Environment=PYTHONDONTWRITEBYTECODE=1" in content
    assert ".venv/bin/python -B scripts/seed_memories.py" in content
    assert ".venv/bin/python -B -m hive.surfaces.cli serve" in content


def test_gateway_unit_module_entrypoint_is_runnable_without_bytecode(tmp_path):
    env = minimal_worker_environment()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        (sys.executable, "-B", "-m", "hive.surfaces.cli", "--help"),
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr[:300]
    assert "hive" in result.stdout.lower()


def test_gateway_stamps_revision_once_at_app_creation(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg)
    with patch("hive.gateway.app.detect_source_revision", return_value="a" * 40) as detector:
        app = create_app(hive)
    with TestClient(app) as client:
        assert client.get("/health").json()["source_revision"] == "a" * 40
        assert client.get("/health/summary", headers={"X-Hive-Token": cfg.secret}).json()[
            "source_revision"
        ] == "a" * 40
    detector.assert_called_once()


def test_gateway_reports_unverified_revision_as_null(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg)
    with patch("hive.gateway.app.detect_source_revision", return_value=None):
        app = create_app(hive)
    with TestClient(app) as client:
        assert client.get("/health").json()["source_revision"] is None


def test_gateway_instance_id_is_stable_per_app_and_changes_on_app_recreation(tmp_path):
    cfg = HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    hive = HiveOS.build(cfg)
    first_app = create_app(hive)
    second_app = create_app(hive)
    with TestClient(first_app) as first, TestClient(second_app) as second:
        first_id = first.get("/health").json()["runtime_instance_id"]
        assert len(first_id) == 32
        assert int(first_id, 16) >= 0
        assert first.get("/health").json()["runtime_instance_id"] == first_id
        assert first.get("/health/full", headers={"X-Hive-Token": cfg.secret}).json()[
            "runtime_instance_id"
        ] == first_id
        assert first.get("/health/summary", headers={"X-Hive-Token": cfg.secret}).json()[
            "runtime_instance_id"
        ] == first_id
        assert second.get("/health").json()["runtime_instance_id"] != first_id
