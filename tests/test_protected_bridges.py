"""The new package must reach SOUL.md + approval_gate.py verbatim, in place."""
from __future__ import annotations

from hive.core import soul, approval
from hive.core.soul import REPO_ROOT


def test_soul_loaded_verbatim():
    raw = (REPO_ROOT / "Config" / "SOUL.md").read_text(encoding="utf-8")
    assert soul.SOUL == raw
    assert "I am **Hive**" in soul.SOUL


def test_soul_path_is_protected_location():
    assert soul.SOUL_PATH == REPO_ROOT / "Config" / "SOUL.md"


def test_approval_gate_bridge_exposes_canonical_symbols():
    assert hasattr(approval.gate, "is_dangerous")
    assert ("config/SOUL.md", "core/approval_gate.py") == approval.PROTECTED_PATHS
    # dangerous detection still works through the bridge
    assert approval.gate.is_dangerous("shell", {"cmd": "rm -rf /"}) is True
    assert approval.gate.is_dangerous("read_file", {"path": "x.txt"}) is False


def test_protected_files_untouched_on_disk():
    # sanity: the canonical files exist where the hard limit requires them.
    assert (REPO_ROOT / "Config" / "SOUL.md").is_file()
    assert (REPO_ROOT / "Core" / "approval_gate.py").is_file()


# --- Additional protected bridge tests -----------------------------------------

def test_soul_content_contains_identity_section():
    """SOUL.md must contain the core identity declaration."""
    assert "Hive" in soul.SOUL


def test_soul_content_not_empty():
    """SOUL.md is non-trivially long (at least 100 chars)."""
    assert len(soul.SOUL) > 100


def test_approval_gate_shell_rm_rf_is_dangerous():
    """Shell rm -rf / must always be detected as dangerous."""
    assert approval.gate.is_dangerous("shell", {"cmd": "rm -rf /"}) is True


def test_approval_gate_read_file_not_dangerous():
    """Reading a normal file must NOT be flagged as dangerous."""
    assert approval.gate.is_dangerous("read_file", {"path": "/tmp/x.txt"}) is False


def test_approval_gate_is_callable():
    """approval.gate is a real object with is_dangerous method."""
    assert callable(approval.gate.is_dangerous)


def test_repo_root_is_directory():
    """REPO_ROOT must point to an existing directory."""
    assert REPO_ROOT.is_dir()
