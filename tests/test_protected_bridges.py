"""The new package must reach SOUL.md + approval_gate.py verbatim, in place."""
from __future__ import annotations

from pathlib import Path

from hive.core import soul, approval, config


def test_soul_loaded_verbatim():
    raw = (config.ROOT / "Config" / "SOUL.md").read_text(encoding="utf-8")
    assert soul.SOUL == raw
    assert "I am **Hive**" in soul.SOUL


def test_soul_path_is_protected_location():
    assert soul.SOUL_PATH == config.ROOT / "Config" / "SOUL.md"


def test_approval_gate_bridge_exposes_canonical_symbols():
    assert hasattr(approval.gate, "is_dangerous")
    assert ("config/SOUL.md", "core/approval_gate.py") == approval.PROTECTED_PATHS
    # dangerous detection still works through the bridge
    assert approval.gate.is_dangerous("shell", {"cmd": "rm -rf /"}) is True
    assert approval.gate.is_dangerous("read_file", {"path": "x.txt"}) is False


def test_protected_files_untouched_on_disk():
    # sanity: the canonical files exist where the hard limit requires them.
    assert (config.ROOT / "Config" / "SOUL.md").is_file()
    assert (config.ROOT / "Core" / "approval_gate.py").is_file()
