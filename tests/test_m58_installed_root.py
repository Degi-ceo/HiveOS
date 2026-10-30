"""A wheel install must locate the host's protected source files explicitly."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hive.core.soul import resolve_repo_root


def _fake_wheel_module(tmp_path: Path) -> Path:
    module = tmp_path / "wheel" / "Lib" / "site-packages" / "hive" / "core" / "soul.py"
    module.parent.mkdir(parents=True)
    module.touch()
    return module


def _release_root(tmp_path: Path) -> Path:
    root = tmp_path / "release"
    (root / "Config").mkdir(parents=True)
    (root / "Core").mkdir()
    (root / "Config" / "SOUL.md").write_text("test fixture", encoding="utf-8")
    (root / "Core" / "approval_gate.py").write_text("test fixture", encoding="utf-8")
    return root


def test_installed_wheel_requires_explicit_protected_root(tmp_path):
    with pytest.raises(RuntimeError, match="HIVE_REPO_ROOT"):
        resolve_repo_root(_fake_wheel_module(tmp_path), None)


def test_installed_wheel_accepts_absolute_root_with_both_protected_files(tmp_path):
    root = _release_root(tmp_path)
    assert resolve_repo_root(_fake_wheel_module(tmp_path), str(root)) == root.resolve()


def test_fake_protected_files_beside_wheel_cannot_override_explicit_root(tmp_path):
    module = _fake_wheel_module(tmp_path)
    fake_root = module.parents[3]
    (fake_root / "Config").mkdir()
    (fake_root / "Core").mkdir()
    (fake_root / "Config" / "SOUL.md").write_text("spoofed", encoding="utf-8")
    (fake_root / "Core" / "approval_gate.py").write_text("spoofed", encoding="utf-8")
    trusted = _release_root(tmp_path)
    assert resolve_repo_root(module, str(trusted)) == trusted.resolve()
    with pytest.raises(RuntimeError, match="HIVE_REPO_ROOT"):
        resolve_repo_root(module, None)


def test_installed_wheel_rejects_incomplete_or_relative_root(tmp_path):
    module = _fake_wheel_module(tmp_path)
    with pytest.raises(RuntimeError, match="absolute"):
        resolve_repo_root(module, "relative/release")
    root = _release_root(tmp_path)
    (root / "Core" / "approval_gate.py").unlink()
    with pytest.raises(RuntimeError, match="approval_gate.py"):
        resolve_repo_root(module, str(root))


def test_source_checkout_rejects_foreign_configured_root(tmp_path):
    from hive.core import soul

    with pytest.raises(RuntimeError, match="conflicts with executing source tree"):
        resolve_repo_root(soul.__file__, str(_release_root(tmp_path)))


def test_incomplete_source_checkout_cannot_switch_to_foreign_root(tmp_path):
    module = tmp_path / "broken" / "src" / "hive" / "core" / "soul.py"
    module.parent.mkdir(parents=True)
    module.touch()
    trusted = _release_root(tmp_path)
    with pytest.raises(RuntimeError, match="Source checkout is missing protected"):
        resolve_repo_root(module, str(trusted))


def test_pytest_conftest_clears_foreign_root_before_gate_import(tmp_path):
    source_root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "HIVE_REPO_ROOT": str(tmp_path / "foreign-root"),
           "PYTHONPATH": str(source_root / "src")}
    result = subprocess.run(
        [sys.executable, "-c", "import tests.conftest; from hive.core.soul import REPO_ROOT; print(REPO_ROOT)"],
        cwd=source_root, env=env, capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == source_root


def test_protected_bridges_share_the_resolved_root():
    from hive.core import approval, soul
    from hive.tools import file_safety

    assert approval.REPO_ROOT == soul.REPO_ROOT == file_safety.REPO_ROOT
