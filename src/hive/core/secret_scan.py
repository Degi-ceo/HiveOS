"""Fail-closed, redacted scanning for self-modification candidate diffs.

The scanner intentionally reports only a rule, path, and line number.  A
candidate may contain a real credential, so returning a matching value (or a
nearby source line) would turn the safety report into another disclosure path.
It is a local pre-commit boundary; repository push protection remains a useful
independent backstop, not a replacement.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


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
    r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|password)\b"
    r"\s*[:=]\s*[\"']?([A-Za-z0-9_./+=-]{16,})"
)
_SAFE_PLACEHOLDERS = frozenset({"change_me", "your_key_here", "placeholder", "example"})


def scan_added_diff(diff: str) -> list[SecretFinding]:
    """Find likely credentials in added lines of a unified Git diff.

    Deleted context is ignored, which prevents an old leak being treated as a
    new candidate leak.  Findings are deduplicated by safe metadata.
    """
    path = ""
    line = 0
    findings: list[SecretFinding] = []
    for raw_line in str(diff).splitlines():
        if raw_line.startswith("+++ "):
            candidate = raw_line[4:].strip()
            path = candidate[2:] if candidate.startswith("b/") else candidate
            line = 0
            continue
        if raw_line.startswith("@@"):
            match = re.search(r"\+(\d+)", raw_line)
            line = int(match.group(1)) if match else 0
            continue
        # Only ``+++ `` denotes a unified-diff file header.  Content itself may
        # legitimately begin with ``++`` and must still be scanned.
        if not raw_line.startswith("+"):
            continue
        content = raw_line[1:]
        for rule, pattern in _KNOWN_PATTERNS:
            if pattern.search(content):
                findings.append(SecretFinding(rule=rule, path=path, line=line))
        assignment = _ASSIGNMENT.search(content)
        if assignment and assignment.group(1).casefold() not in _SAFE_PLACEHOLDERS:
            findings.append(SecretFinding(rule="credential_assignment", path=path, line=line))
        line += 1
    return list(dict.fromkeys(findings))
