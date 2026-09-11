from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

from hive.core.events import EventBus, EventType
from hive.core.secret_scan import scan_added_diff
from hive.core.self_mod import SelfModifier
from hive.core.spec_search import Edit, EditOp, RiskTier, SelfImprovement
from hive.observability.audit import AuditLog, _audit_broadcaster
from hive.observability.persistence import ObservabilityLedger


def _diff(value: str) -> str:
    return f"+++ b/demo.txt\n@@ -0,0 +1 @@\n+value = '{value}'\n"


def test_scanner_blocks_literal_configured_secret(monkeypatch):
    secret = "ordinary-value-with-no-vendor-prefix"
    monkeypatch.setenv("HIVE_SECRET", secret)

    findings = scan_added_diff(_diff(secret))

    assert any(item.rule == "known_secret_value" for item in findings)
    assert secret not in str(findings)


def test_scanner_blocks_high_entropy_value_and_ignores_placeholder():
    high_entropy = "VjNwQ8sT7aL2mR9xC4kY6uH1zF5b"

    findings = scan_added_diff(_diff(high_entropy))

    assert any("entropy" in item.rule.lower() for item in findings)
    assert scan_added_diff("+++ b/demo.env\n@@ -0,0 +1 @@\n+API_KEY='your_key_here'\n") == []


def test_candidate_allowlist_comment_cannot_disable_secret_scanner():
    high_entropy = "Z8rN4vQ2mL7xK5pT9cW3yF6sH1bD"

    findings = scan_added_diff(
        _diff(high_entropy).rstrip("\n") + "  # pragma: allowlist secret\n",
    )

    assert findings


def test_placeholder_does_not_hide_a_second_secret_on_same_line():
    high_entropy = "VjNwQ8sT7aL2mR9xC4kY6uH1zF5b"
    diff = (
        "+++ b/demo.py\n@@ -0,0 +1 @@\n"
        f"+password = 'example'; value = '{high_entropy}'\n"
    )

    assert scan_added_diff(diff)


def test_placeholder_does_not_hide_second_unquoted_assignment():
    high_entropy = "vjnwq8st7al2mr9xc4ky6uh1zf5b_extra"
    diff = (
        "+++ b/.env\n@@ -0,0 +1 @@\n"
        f"+password=example; API_KEY={high_entropy}\n"
    )

    assert scan_added_diff(diff)


def test_indirect_credential_assignments_do_not_block_selfmod():
    diff = (
        "+++ b/demo.py\n@@ -0,0 +1,2 @@\n"
        "+api_key = config.api_key\n"
        "+secret = generate_secret()\n"
    )

    assert scan_added_diff(diff) == []


def test_unquoted_config_and_shell_values_are_not_treated_as_indirect_references():
    values = (
        "VjNwQ8sT7aL2mR9xC4kY6uH1zF5b",
        "vjnw.q8st7al2mr9xc4ky6uh1zf5b",
        "vjnw_q8st7al2mr9xc4ky6uh1zf5b",
    )

    for path in (".env", "setup.sh", "demo.py", "demo.js"):
        for value in values:
            lines = (f"API_KEY={value}", f"# API_KEY={value}", f"// API_KEY={value}")
            for line in lines:
                diff = f"+++ b/{path}\n@@ -0,0 +1 @@\n+{line}\n"
                assert scan_added_diff(diff)


def test_prefixed_credential_names_are_scanned():
    value = "vjnwq8st7al2mr9xc4ky6uh1zf5b_extra"

    for name in (
        "HIVE_SECRET", "HIVE_APPROVER_KEY", "DATABASE_PASSWORD", "OPENAI_API_KEY",
    ):
        diff = f"+++ b/.env\n@@ -0,0 +1 @@\n+{name}={value}\n"
        assert scan_added_diff(diff)


def test_quoted_prefixed_credential_names_are_scanned():
    value = "vjnwq8st7al2mr9xc4ky6uh1zf5b_extra"

    for path, line in (
        ("config.json", f'"HIVE_APPROVER_KEY": "{value}"'),
        ("config.toml", f'"HIVE_APPROVER_KEY" = "{value}"'),
        ("config.py", f'{{"HIVE_APPROVER_KEY": "{value}"}}'),
    ):
        diff = f"+++ b/{path}\n@@ -0,0 +1 @@\n+{line}\n"
        assert scan_added_diff(diff)


def test_added_content_starting_with_double_plus_is_not_misparsed_as_header(monkeypatch):
    secret = "configured-double-plus-secret"
    monkeypatch.setenv("HIVE_SECRET", secret)
    diff = (
        "diff --git a/demo.txt b/demo.txt\n"
        "--- a/demo.txt\n"
        "+++ b/demo.txt\n"
        "@@ -0,0 +1 @@\n"
        f"+++ HIVE_SECRET={secret}\n"
    )

    findings = scan_added_diff(diff)

    assert any(item.rule == "known_secret_value" for item in findings)
    assert secret not in str(findings)


def test_configured_secret_in_candidate_path_is_blocked_and_redacted(monkeypatch):
    secret = "configured-path-secret-value"
    monkeypatch.setenv("HIVE_SECRET", secret)
    diff = (
        f"diff --git a/docs/{secret}.md b/docs/{secret}.md\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/docs/{secret}.md\n"
        "@@ -0,0 +1 @@\n"
        "+safe content\n"
    )

    findings = scan_added_diff(diff)

    assert any(item.rule == "known_secret_path" for item in findings)
    assert secret not in str(findings)


def test_url_encoded_configured_secret_is_blocked(monkeypatch):
    secret = "configured/secret+value"
    monkeypatch.setenv("HIVE_SECRET", secret)
    diff = (
        "diff --git a/demo.txt b/demo.txt\n"
        "--- a/demo.txt\n"
        "+++ b/demo.txt\n"
        "@@ -0,0 +1 @@\n"
        "+url=https://example.invalid/configured%2Fsecret%2Bvalue\n"
    )

    assert any(item.rule == "known_secret_value" for item in scan_added_diff(diff))


def test_empty_file_path_is_scanned_from_diff_git_header(monkeypatch):
    secret = "configured-empty-path-secret"
    monkeypatch.setenv("HIVE_SECRET", secret)
    diff = (
        f"diff --git a/docs/{secret}.md b/docs/{secret}.md\n"
        "new file mode 100644\n"
        "index 0000000..e69de29\n"
    )

    findings = scan_added_diff(diff)

    assert any(item.rule == "known_secret_path" for item in findings)
    assert secret not in str(findings)


def test_ansi_colored_diff_is_scanned(monkeypatch):
    secret = "configured-colored-diff-secret"
    monkeypatch.setenv("HIVE_SECRET", secret)
    diff = (
        "\x1b[1mdiff --git a/demo.txt b/demo.txt\x1b[m\n"
        "\x1b[1m--- a/demo.txt\x1b[m\n"
        "\x1b[1m+++ b/demo.txt\x1b[m\n"
        "\x1b[36m@@ -0,0 +1 @@\x1b[m\n"
        f"\x1b[32m+HIVE_SECRET={secret}\x1b[m\n"
    )

    assert any(item.rule == "known_secret_value" for item in scan_added_diff(diff))


def test_form_feed_inside_added_line_does_not_detach_secret(monkeypatch):
    secret = "configured-form-feed-secret"
    monkeypatch.setenv("HIVE_SECRET", secret)
    diff = (
        "diff --git a/demo.py b/demo.py\n"
        "--- a/demo.py\n"
        "+++ b/demo.py\n"
        "@@ -0,0 +1 @@\n"
        f"+prefix = 'safe'\x0c; HIVE_SECRET='{secret}'\n"
    )

    assert any(item.rule == "known_secret_value" for item in scan_added_diff(diff))


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_utf16_added_content_is_scanned(monkeypatch, encoding):
    secret = "configured-utf16-secret-value"
    monkeypatch.setenv("HIVE_SECRET", secret)
    encoded_content = (
        f"HIVE_SECRET={secret}\n".encode(encoding).decode(errors="replace")
    )
    diff = (
        "diff --git a/demo.txt b/demo.txt\n"
        "--- a/demo.txt\n"
        "+++ b/demo.txt\n"
        "@@ -0,0 +1 @@\n"
        f"+{encoded_content}"
    )

    assert any(item.rule == "known_secret_value" for item in scan_added_diff(diff))


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_utf16_non_ascii_secret_fails_closed_on_nul_encoding(monkeypatch, encoding):
    secret = "VjNwQ8sT7aL2mR9xéC4kY6uH1zF5b_more"
    monkeypatch.setenv("HIVE_SECRET", secret)
    encoded_content = (
        f'HIVE_SECRET="{secret}"\n'.encode(encoding).decode(errors="replace")
    )
    diff = (
        "diff --git a/demo.txt b/demo.txt\n"
        "--- a/demo.txt\n"
        "+++ b/demo.txt\n"
        "@@ -0,0 +1 @@\n"
        f"+{encoded_content}"
    )

    assert any(
        item.rule == "unsupported_nul_encoding" for item in scan_added_diff(diff)
    )


def test_selfmod_redacts_metadata_from_events_history_audit_and_return(monkeypatch):
    secret = "metadata-secret-value-987654321"
    monkeypatch.setenv("HIVE_SECRET", secret)
    events = []
    audits = []
    bus = EventBus()
    bus.subscribe(EventType.SELFMOD_START, events.append)

    async def run(command, _cwd=None):
        text = " ".join(command) if isinstance(command, list) else command
        if text.startswith("git rev-parse"):
            return 0, "deadbeef\n"
        if text.startswith("git worktree add"):
            return 1, "cannot create"
        return 0, "ok"

    modifier = SelfModifier(
        repo_root="/tmp/hive", run=run, bus=bus, audit=audits.append,
    )
    result = asyncio.run(modifier.propose(
        f"repair {secret}", f"description {secret}", lambda _wt: None,
        run_id=f"run-{secret}",
    ))

    combined = str((result, events, audits, modifier.history()))
    assert secret not in combined


def test_selfmod_redacts_commit_and_pr_metadata(monkeypatch):
    secret = "pr-metadata-secret-value-987654321"
    monkeypatch.setenv("HIVE_SECRET", secret)
    commands = []
    opened = {}

    async def run(command, _cwd=None):
        text = " ".join(command) if isinstance(command, list) else command
        commands.append(text)
        if text == "git write-tree" or text == "git rev-parse HEAD^{tree}":
            return 0, "a" * 40 + "\n"
        if " commit-tree " in f" {text} ":
            return 0, "c" * 40 + "\n"
        if text.startswith("git rev-parse"):
            return 0, "deadbeef\n"
        if text.startswith("git diff --name-only"):
            return 0, "docs/demo.md\n"
        if text.startswith("git ls-files --others"):
            return 0, ""
        if text.startswith("git diff --cached"):
            return 0, "+++ b/docs/demo.md\n@@ -0,0 +1 @@\n+safe docs\n"
        if text.startswith("git status --porcelain"):
            return 0, "A  docs/demo.md\n"
        return 0, "ok"

    async def apply(_worktree):
        return ["docs/demo.md"]

    async def open_pr(branch, title, body):
        opened.update(branch=branch, title=title, body=body)
        return "https://example.invalid/pr/1"

    result = asyncio.run(SelfModifier(
        repo_root="/tmp/hive", run=run, open_pr=open_pr,
    ).propose(f"title {secret}", f"body {secret}", apply))

    assert result["ok"] is True
    assert secret not in str((commands, opened, result))


def test_audit_and_persistent_selfmod_sinks_redact_known_values(tmp_path, monkeypatch):
    secret = "sink-secret-value-987654321"
    monkeypatch.setenv("HIVE_SECRET", secret)
    _audit_broadcaster.reset()
    queue = _audit_broadcaster.subscribe()
    audit = AuditLog(tmp_path / "audit.db")
    ledger = ObservabilityLedger(tmp_path / "state.db")
    try:
        audit.record({"tool": "demo", "status": "error", "error": secret,
                      "args": {"detail": secret}})
        ledger.record_selfmod({"run_id": secret, "title": secret, "branch": secret,
                               "pr_url": secret, "outcome": secret})

        assert secret not in str(audit.recent())
        assert secret not in str(queue.get_nowait())
        assert secret not in str(ledger.selfmod_history())
        ledger.close()
        ledger = ObservabilityLedger(tmp_path / "state.db")
        assert secret not in str(ledger.selfmod_history())
    finally:
        audit.close()
        ledger.close()
        _audit_broadcaster.reset()


def test_secret_scan_failure_escalates_auto_and_approved_paths_to_manual():
    class Modifier:
        async def propose(self, *_args, **_kwargs):
            return {"ok": False, "stage": "secret_scan", "msg": "blocked"}

        async def propose_approved(self, *_args, **_kwargs):
            return {"ok": False, "stage": "secret_scan_error", "msg": "blocked"}

    async def apply(_worktree):
        return ["tests/demo.py"]

    improvement = SelfImprovement(Modifier())
    auto = Edit(op=EditOp.ADD_TEST, summary="test", apply=apply,
                target_files=["tests/demo.py"])
    review = Edit(op=EditOp.PATCH_CODE, summary="code", apply=apply,
                  target_files=["src/demo.py"])

    auto_outcome = asyncio.run(improvement.run([auto]))[0]
    approved_outcome = asyncio.run(improvement.apply_approved(review))

    assert (auto_outcome.status, auto_outcome.tier) == ("blocked_safety", RiskTier.MANUAL)
    assert (approved_outcome.status, approved_outcome.tier) == ("blocked_safety", RiskTier.MANUAL)


@pytest.mark.parametrize("invalid_result", [{}, "", ()])
def test_invalid_empty_scanner_result_fails_closed(invalid_result):
    async def run(command, _cwd=None):
        text = " ".join(command) if isinstance(command, list) else command
        if text == "git write-tree" or text == "git rev-parse HEAD^{tree}":
            return 0, "a" * 40 + "\n"
        if " commit-tree " in f" {text} ":
            return 0, "c" * 40 + "\n"
        if text.startswith("git rev-parse"):
            return 0, "deadbeef\n"
        if text.startswith("git diff --name-only"):
            return 0, "docs/demo.md\n"
        if text.startswith("git ls-files --others"):
            return 0, ""
        if text.startswith("git diff --cached --name-only"):
            return 0, "docs/demo.md\n"
        if text.startswith("git diff --cached"):
            return 0, "+++ b/docs/demo.md\n@@ -0,0 +1 @@\n+safe docs\n"
        if text.startswith("git status --porcelain"):
            return 0, "A  docs/demo.md\n"
        return 0, "ok"

    async def apply(_worktree):
        return ["docs/demo.md"]

    result = asyncio.run(SelfModifier(
        repo_root="/tmp/hive", run=run,
        secret_scanner=lambda _diff: invalid_result,
    ).propose("candidate", "invalid scanner", apply, dry_run=True))

    assert result["stage"] == "secret_scan_error"


def test_real_git_candidate_blocks_literal_env_secret_before_commit(tmp_path, monkeypatch):
    secret = "ordinary-live-secret-value-987654321"
    monkeypatch.setenv("HIVE_SECRET", secret)
    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "hive-test@localhost"],
        ["git", "config", "user.name", "Hive Test"],
    ):
        subprocess.run(command, cwd=repo, check=True)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()

    async def apply(worktree):
        (Path(worktree) / "candidate.txt").write_text(secret + "\n", encoding="utf-8")
        return ["candidate.txt"]

    result = asyncio.run(SelfModifier(
        repo_root=str(repo), test_cmd=f'"{sys.executable}" -c "pass"',
    ).propose("candidate", "literal env secret", apply))

    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    assert result["stage"] == "secret_scan"
    assert secret not in str(result)
    assert before == after
    assert subprocess.run(
        ["git", "branch", "--list", "hive/auto-*"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip() == ""


@pytest.mark.parametrize(
    "case", [
        "double-plus", "path", "empty-path", "url-encoded", "colored", "form-feed",
        "utf16", "utf16-le", "utf16-be", "utf16-unicode",
    ],
)
def test_real_git_candidate_blocks_diff_and_path_secret_bypasses(
    tmp_path, monkeypatch, case,
):
    if case == "url-encoded":
        secret = "configured/secret+value-987654321"
    elif case == "utf16-unicode":
        secret = "VjNwQ8sT7aL2mR9xéC4kY6uH1zF5b_more"
    else:
        secret = f"configured-{case}-secret-value-987654321"
    monkeypatch.setenv("HIVE_SECRET", secret)
    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "hive-test@localhost"],
        ["git", "config", "user.name", "Hive Test"],
    ):
        subprocess.run(command, cwd=repo, check=True)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)

    async def apply(worktree):
        if case in {"path", "empty-path"}:
            relative = Path("docs") / f"{secret}.md"
            (Path(worktree) / "docs").mkdir()
            content = "" if case == "empty-path" else "safe content\n"
        else:
            relative = Path("candidate.txt")
            if case == "double-plus":
                content = f"++ HIVE_SECRET={secret}\n"
            elif case == "url-encoded":
                content = f"url=https://example.invalid/{quote(secret, safe='')}\n"
            elif case == "form-feed":
                content = f"prefix = 'safe'\x0c; HIVE_SECRET='{secret}'\n"
            else:
                content = f"HIVE_SECRET={secret}\n"
        candidate = Path(worktree) / relative
        if case.startswith("utf16"):
            encoding = {
                "utf16": "utf-16", "utf16-le": "utf-16-le", "utf16-be": "utf-16-be",
                "utf16-unicode": "utf-16",
            }[case]
            candidate.write_bytes(f"HIVE_SECRET={secret}\n".encode(encoding))
        else:
            candidate.write_text(content, encoding="utf-8")
        return [str(relative)]

    if case == "colored":
        subprocess.run(
            ["git", "config", "color.ui", "always"], cwd=repo, check=True,
        )

    result = asyncio.run(SelfModifier(
        repo_root=str(repo), test_cmd=f'"{sys.executable}" -c "pass"',
    ).propose("candidate", "secret bypass regression", apply))

    assert result["stage"] == "secret_scan"
    assert secret not in str(result)
    assert subprocess.run(
        ["git", "branch", "--list", "hive/auto-*"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip() == ""


@pytest.mark.parametrize("operation", ["delete", "rename"])
def test_real_git_candidate_can_remove_secret_from_existing_path(
    tmp_path, monkeypatch, operation,
):
    secret = "legacy-path-secret-value-987654321"
    monkeypatch.setenv("HIVE_SECRET", secret)
    repo = tmp_path / "repo"
    source = repo / "docs" / f"{secret}.md"
    source.parent.mkdir(parents=True)
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "hive-test@localhost"],
        ["git", "config", "user.name", "Hive Test"],
    ):
        subprocess.run(command, cwd=repo, check=True)
    source.write_text("safe content\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)

    async def apply(worktree):
        candidate_source = Path(worktree) / "docs" / f"{secret}.md"
        if operation == "delete":
            candidate_source.unlink()
            return [f"docs/{secret}.md"]
        target = Path(worktree) / "docs" / "safe-name.md"
        candidate_source.rename(target)
        return [f"docs/{secret}.md", "docs/safe-name.md"]

    result = asyncio.run(SelfModifier(
        repo_root=str(repo), test_cmd=f'"{sys.executable}" -c "pass"',
    ).propose("candidate", "remove legacy secret path", apply, dry_run=True))

    assert result["ok"] is True
    assert result["stage"] == "dry_run"
    assert secret not in str(result)
