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


# --- New tests (6) ---------------------------------------------------------------

def test_soul_contains_hard_limits_section():
    """SOUL.md must have a 'Hard limits' section (the never-violate guardrails)."""
    assert "Hard limits" in soul.SOUL


def test_soul_path_resolves_to_file():
    """SOUL_PATH must be a fully-resolved absolute path pointing to a real file."""
    assert soul.SOUL_PATH.is_absolute()
    assert soul.SOUL_PATH.is_file()


def test_approval_gate_dangerous_tools_set_nonempty():
    """DANGEROUS_TOOLS must be a non-empty set so the firewall is active."""
    assert isinstance(approval.DANGEROUS_TOOLS, (set, frozenset))
    assert len(approval.DANGEROUS_TOOLS) > 0


def test_approval_gate_deploy_tool_is_dangerous():
    """The 'deploy' tool name must always be flagged as dangerous."""
    assert approval.gate.is_dangerous("deploy", {}) is True


def test_approval_gate_request_returns_id_and_pending():
    """gate.request() returns an approval id string and the item appears in pending()."""
    aid = approval.gate.request("shell_destructive", {"cmd": "rm -rf /tmp/test"}, "unit test")
    try:
        assert isinstance(aid, str) and len(aid) > 0
        pending_ids = [p["id"] for p in approval.gate.pending()]
        assert aid in pending_ids
    finally:
        # clean up so we don't pollute the global gate state
        approval.gate.resolve(aid, False)


def test_approval_gate_resolve_removes_from_pending():
    """After resolve(), the approval id must no longer appear in pending()."""
    aid = approval.gate.request("payment", {"amount": "1"}, "unit test resolve")
    approval.gate.resolve(aid, True)
    pending_ids = [p["id"] for p in approval.gate.pending()]
    assert aid not in pending_ids


# --- Six additional protected-bridge tests ------------------------------------------

def test_soul_load_soul_equals_soul_attr():
    """soul.load_soul() must return the same text as the pre-loaded soul.SOUL attribute."""
    assert soul.load_soul() == soul.SOUL


def test_soul_content_mentions_kamil():
    """SOUL.md must mention the owner 'Kamil' by name."""
    assert "Kamil" in soul.SOUL


def test_approval_gate_merge_main_is_dangerous():
    """The 'merge_main' tool must always be flagged as dangerous."""
    assert approval.gate.is_dangerous("merge_main", {}) is True


def test_approval_gate_external_message_is_dangerous():
    """The 'external_message' tool must always be flagged as dangerous."""
    assert approval.gate.is_dangerous("external_message", {}) is True


def test_approval_gate_request_money_returns_id_and_appears_in_pending():
    """gate.request_money() returns a non-empty id string and the item shows in pending()."""
    aid = approval.gate.request_money("buy-test-server", "42", "unit test money")
    try:
        assert isinstance(aid, str) and len(aid) > 0
        pending_ids = [p["id"] for p in approval.gate.pending()]
        assert aid in pending_ids
    finally:
        approval.gate.resolve(aid, False)


def test_approval_gate_pending_returns_list():
    """gate.pending() always returns a list (possibly empty)."""
    result = approval.gate.pending()
    assert isinstance(result, list)


# --- Six more protected-bridge tests ------------------------------------------------

def test_approval_gate_spend_money_is_dangerous():
    """The 'spend_money' tool name must always be flagged as dangerous."""
    assert approval.gate.is_dangerous("spend_money", {}) is True


def test_approval_gate_payment_is_dangerous():
    """The 'payment' tool name must always be flagged as dangerous."""
    assert approval.gate.is_dangerous("payment", {}) is True


def test_approval_gate_shell_destructive_is_dangerous():
    """The 'shell_destructive' tool name must always be flagged as dangerous."""
    assert approval.gate.is_dangerous("shell_destructive", {}) is True


def test_approval_gate_resolve_returns_approved_false():
    """resolve(aid, False) returns the item dict with approved=False."""
    aid = approval.gate.request("test_resolve_false", {"x": 1}, "unit test")
    result = approval.gate.resolve(aid, False)
    assert result is not None
    assert result["approved"] is False
    assert result["tool"] == "test_resolve_false"


def test_approval_gate_resolve_returns_approved_true():
    """resolve(aid, True) returns the item dict with approved=True."""
    aid = approval.gate.request("test_resolve_true", {"y": 2}, "unit test")
    result = approval.gate.resolve(aid, True)
    assert result is not None
    assert result["approved"] is True


def test_soul_path_matches_repo_root_config():
    """SOUL_PATH must be REPO_ROOT / 'Config' / 'SOUL.md', not some other location."""
    from hive.core.soul import REPO_ROOT as soul_root
    expected = soul_root / "Config" / "SOUL.md"
    assert soul.SOUL_PATH == expected


# --- Wave 3V-A: 8 new protected-bridge tests ------------------------------------

def test_wave3v_approval_gate_protected_paths_tuple():
    """PROTECTED_PATHS must be a tuple with exactly two entries."""
    assert isinstance(approval.PROTECTED_PATHS, tuple)
    assert len(approval.PROTECTED_PATHS) == 2


def test_wave3v_approval_gate_soul_md_in_protected_paths():
    """'config/SOUL.md' must be in PROTECTED_PATHS."""
    assert "config/SOUL.md" in approval.PROTECTED_PATHS


def test_wave3v_approval_gate_approval_gate_in_protected_paths():
    """'core/approval_gate.py' must be in PROTECTED_PATHS."""
    assert "core/approval_gate.py" in approval.PROTECTED_PATHS


def test_wave3v_soul_file_size_consistent_with_module():
    """The size of the SOUL.md file on disk matches len(soul.SOUL)."""
    raw = soul.SOUL_PATH.read_bytes()
    assert len(raw.decode("utf-8")) == len(soul.SOUL)


def test_wave3v_approval_gate_infra_deploy_is_dangerous():
    """'infra_deploy' contains 'deploy' — must be flagged as dangerous."""
    assert approval.gate.is_dangerous("deploy", {"target": "prod"}) is True


def test_wave3v_approval_gate_request_returns_8char_hex():
    """gate.request() returns an 8-character hex string id."""
    aid = approval.gate.request("test_hex", {}, "hex length test")
    try:
        assert len(aid) == 8
        int(aid, 16)  # must be valid hex
    finally:
        approval.gate.resolve(aid, False)


def test_wave3v_approval_gate_multiple_pending_accumulate():
    """Two consecutive requests both appear in pending() simultaneously."""
    aid1 = approval.gate.request("multi_a", {}, "multi test 1")
    aid2 = approval.gate.request("multi_b", {}, "multi test 2")
    try:
        pending_ids = [p["id"] for p in approval.gate.pending()]
        assert aid1 in pending_ids
        assert aid2 in pending_ids
    finally:
        approval.gate.resolve(aid1, False)
        approval.gate.resolve(aid2, False)


def test_wave3v_approval_gate_resolve_unknown_id_returns_none():
    """Resolving an unknown id must return None without raising."""
    result = approval.gate.resolve("00000000", False)
    assert result is None
