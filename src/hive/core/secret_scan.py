"""Fail-closed, redacted scanning for self-modification candidate diffs.

The scanner intentionally reports only a rule, path, and line number.  A
candidate may contain a real credential, so returning a matching value (or a
nearby source line) would turn the safety report into another disclosure path.
It is a local pre-commit boundary; repository push protection remains a useful
independent backstop, not a replacement.
"""
from __future__ import annotations

import re
import shlex
from collections import Counter, defaultdict
from dataclasses import dataclass
from math import log2

from detect_secrets.core.scan import _process_line_based_plugins
from detect_secrets.settings import transient_settings

from hive.core.redact import contains_known_secret, redact_known_secrets


@dataclass(frozen=True, slots=True)
class SecretFinding:
    """One redacted candidate-secret finding."""

    rule: str
    path: str
    line: int


_KNOWN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
)
_ASSIGNMENT = re.compile(
    r"(?i)\b(?:[a-z0-9]+[_-])*(?:api[_-]?key|approver[_-]?key|access[_-]?token|auth[_-]?token|secret|password)\b[\"']?"
    r"\s*[:=]\s*[\"']?([A-Za-z0-9_./+=-]{4,})"
)
_SAFE_PLACEHOLDERS = frozenset({"change_me", "your_key_here", "placeholder", "example"})
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    size = len(value)
    return -sum(
        (count / size) * log2(count / size)
        for count in Counter(value).values()
    )


def _safe_finding_path(path: str) -> str:
    if contains_known_secret(path):
        return "<redacted-path>"
    return redact_known_secrets(path)


def scan_candidate_paths(paths: list[str]) -> list[SecretFinding]:
    """Scan Git-sourced candidate paths, including paths without diff hunks."""
    findings: list[SecretFinding] = []
    for path in paths:
        if contains_known_secret(path):
            findings.append(SecretFinding(
                rule="known_secret_path", path="<redacted-path>", line=0,
            ))
        for rule, pattern in _KNOWN_PATTERNS:
            if pattern.search(path):
                findings.append(SecretFinding(
                    rule=f"{rule}_path", path="<redacted-path>", line=0,
                ))
    return list(dict.fromkeys(findings))


_DETECT_SECRETS_SETTINGS = {
    "plugins_used": [
        {"name": "AWSKeyDetector"},
        {"name": "AzureStorageKeyDetector"},
        {"name": "Base64HighEntropyString", "limit": 4.5},
        {"name": "BasicAuthDetector"},
        {"name": "DiscordBotTokenDetector"},
        {"name": "GitHubTokenDetector"},
        {"name": "GitLabTokenDetector"},
        {"name": "HexHighEntropyString", "limit": 3.0},
        {"name": "JwtTokenDetector"},
        {"name": "KeywordDetector"},
        {"name": "NpmDetector"},
        {"name": "OpenAIDetector"},
        {"name": "PrivateKeyDetector"},
        {"name": "PypiTokenDetector"},
        {"name": "SendGridDetector"},
        {"name": "SlackDetector"},
        {"name": "StripeDetector"},
        {"name": "TelegramBotTokenDetector"},
        {"name": "TwilioKeyDetector"},
    ],
    # Official offline false-positive filters, except the source-controlled
    # allowlist pragma: a candidate must not be able to exempt its own secret.
    "filters_used": [
        {"path": "detect_secrets.filters.heuristic.is_indirect_reference"},
        {"path": "detect_secrets.filters.heuristic.is_likely_id_string"},
        {"path": "detect_secrets.filters.heuristic.is_lock_file"},
        {"path": "detect_secrets.filters.heuristic.is_not_alphanumeric_string"},
        {"path": "detect_secrets.filters.heuristic.is_potential_uuid"},
        {"path": "detect_secrets.filters.heuristic.is_prefixed_with_dollar_sign"},
        {"path": "detect_secrets.filters.heuristic.is_sequential_string"},
        {"path": "detect_secrets.filters.heuristic.is_swagger_file"},
        {"path": "detect_secrets.filters.heuristic.is_templated_secret"},
    ],
}


def scan_added_diff(diff: str) -> list[SecretFinding]:
    """Find likely credentials in added lines of a unified Git diff.

    Deleted context is ignored, which prevents an old leak being treated as a
    new candidate leak.  Findings are deduplicated by safe metadata.
    """
    path = ""
    line = 0
    in_hunk = False
    pending_diff_path = ""
    findings: list[SecretFinding] = []
    added_by_path: dict[str, list[tuple[int, str]]] = defaultdict(list)

    def scan_path(candidate_path: str) -> None:
        findings.extend(scan_candidate_paths([candidate_path]))

    def flush_pending_path() -> None:
        nonlocal pending_diff_path
        if pending_diff_path:
            scan_path(pending_diff_path)
            pending_diff_path = ""

    # Split only at the unified diff's record delimiter. ``str.splitlines``
    # also treats form-feed and other valid source bytes as separators, which
    # can detach content from its leading ``+`` marker.
    for encoded_line in str(diff).split("\n"):
        has_nul = "\x00" in encoded_line
        raw_line = _ANSI_ESCAPE.sub("", encoded_line).replace("\x00", "").rstrip("\r")
        if raw_line.startswith("diff --git "):
            flush_pending_path()
            in_hunk = False
            try:
                fields = shlex.split(raw_line)
            except ValueError:
                fields = []
            if len(fields) >= 4:
                candidate = fields[-1]
                pending_diff_path = (
                    candidate[2:] if candidate.startswith("b/") else candidate
                )
            continue
        if not in_hunk and raw_line.startswith("deleted file mode "):
            pending_diff_path = ""
            continue
        if not in_hunk and raw_line.startswith("--- "):
            continue
        if not in_hunk and raw_line.startswith("+++ "):
            candidate = raw_line[4:].strip()
            path = candidate[2:] if candidate.startswith("b/") else candidate
            if path != "/dev/null":
                scan_path(path)
            pending_diff_path = ""
            line = 0
            continue
        if not in_hunk and raw_line.startswith(("rename to ", "copy to ")):
            path = raw_line.split(" to ", 1)[1].strip()
            scan_path(path)
            pending_diff_path = ""
            continue
        if raw_line.startswith("@@"):
            match = re.search(r"\+(\d+)", raw_line)
            line = int(match.group(1)) if match else 0
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if raw_line.startswith(" "):
            line += 1
            continue
        if not raw_line.startswith("+"):
            continue
        content = raw_line[1:]
        if has_nul:
            findings.append(SecretFinding(
                rule="unsupported_nul_encoding",
                path=_safe_finding_path(path), line=line,
            ))
        for assignment in _ASSIGNMENT.finditer(content):
            assigned_value = assignment.group(1)
            if assigned_value.casefold() in _SAFE_PLACEHOLDERS:
                continue
            if len(assigned_value) >= 20 and _shannon_entropy(assigned_value) >= 4.2:
                findings.append(SecretFinding(
                    rule="high_entropy_assignment", path=_safe_finding_path(path), line=line,
                ))
        if contains_known_secret(content):
            findings.append(SecretFinding(
                rule="known_secret_value", path=_safe_finding_path(path), line=line,
            ))
        for rule, pattern in _KNOWN_PATTERNS:
            if pattern.search(content):
                findings.append(SecretFinding(
                    rule=rule, path=_safe_finding_path(path), line=line,
                ))
        # Keep scanning the complete line even when one assignment is a safe
        # placeholder: a second value on the same line may still be a secret.
        added_by_path[path].append((line, content))
        line += 1

    # ``scan_line`` enables eager ad-hoc matching and treats many ordinary long
    # identifiers as Base64. Process each virtual file with the same non-eager
    # path used by detect-secrets file scans. The dependency is bounded to v1,
    # and this adapter is regression-tested so an upstream API change fails CI.
    with transient_settings(_DETECT_SECRETS_SETTINGS):
        for candidate_path, added_lines in added_by_path.items():
            for potential in _process_line_based_plugins(added_lines, candidate_path):
                if str(potential.secret_value).casefold() in _SAFE_PLACEHOLDERS:
                    continue
                rule = re.sub(r"[^a-z0-9]+", "_", potential.type.lower()).strip("_")
                findings.append(SecretFinding(
                    rule=rule, path=_safe_finding_path(candidate_path),
                    line=int(potential.line_number),
                ))
    flush_pending_path()
    return list(dict.fromkeys(findings))
