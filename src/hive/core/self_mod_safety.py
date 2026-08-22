"""
self_mod_safety.py — Pillar 4 helpers for code-level safety checks.

Two pure-Python helpers used by the learned-skills smoke runner (Pillar 3,
sprint7 Batch B) and by any other code path that needs to vet python source
before executing it. The intent is:

  - ``check_python_syntax(code)``  -> ``(ok: bool, error: str|None)``
  - ``check_dangerous_patterns(code)`` -> ``(ok: bool, reason: str|None)``

The dangerous-pattern list is kept in sync with Pillar 4's canonical list
(``sprint7/selfmod-safety`` branch, ``src/hive/core/self_mod_safety.py``).
Batch B is self-contained — it inlines the pattern set rather than importing
across branches, so a stale Pillar 4 import can't break the smoke runner.
After the two branches merge, the duplication here collapses back to a single
re-export.

This module is intentionally leaf — no project imports. Both helpers are
synchronous and dependency-free so they can be reused from the smoke runner,
the curator, or a future sandbox without dragging DAG weight.
"""
from __future__ import annotations

import ast
import re
from typing import Tuple

# Patterns that should never auto-pass a smoke test. Mirrors the canonical
# Pillar 4 list at ``sprint7/selfmod-safety`` so anything that triggers
# the human gate at runtime also triggers an immediate smoke-test denial
# here. Adding a pattern is a one-line change, but removing one requires
# Kamil's approval.
#
# NOTE: the regexes below intentionally match dangerous string fragments in
# candidate source code — we never call any of the matched APIs ourselves.
# This is a pure detection check; the smoke runner rejects any generated
# body that contains these patterns. Syntax checking uses ``ast.parse``
# (safe).
_DANGEROUS_CODE_PATTERNS = (
    (re.compile(r"\brm\s+-rf?\b", re.I),                "rm -rf / destructive delete"),
    (re.compile(r"\bdd\s+if=", re.I),                   "dd raw disk write"),
    (re.compile(r"\bmkfs\b", re.I),                     "mkfs filesystem format"),
    (re.compile(r"curl\s+[^|]*\|\s*sh\b", re.I),        "curl | sh remote script exec"),
    (re.compile(r"wget\s+[^|]*\|\s*sh\b", re.I),        "wget | sh remote script exec"),
    (re.compile(r"\beval\s*\(", re.I),                  "eval() dynamic execution"),
    (re.compile(r"\bexec\s*\(", re.I),                  "exec() dynamic execution"),
    (re.compile(r"\b__import__\s*\(", re.I),            "dynamic __import__()"),
    (re.compile(
        r"subprocess\.(Popen|call|run|check_output|check_call)\s*\(",
        re.I,
    ),                                                 "subprocess call"),
    (re.compile(r"os\.system\s*\(", re.I),             "os.system() shell call"),
    (re.compile(r"shutil\.rmtree\s*\(", re.I),          "shutil.rmtree recursive delete"),
    (re.compile(r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;\s*:", re.I), "fork bomb pattern"),
)


def check_python_syntax(code: str) -> Tuple[bool, str | None]:
    """Return ``(True, None)`` if ``code`` parses, otherwise ``(False, message)``."""
    if not isinstance(code, str) or not code.strip():
        return False, "empty code body"
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return False, f"SyntaxError: {exc.msg} (line {exc.lineno})"
    return True, None


def check_dangerous_patterns(code: str) -> Tuple[bool, str | None]:
    """Return ``(True, None)`` if ``code`` is clean, else ``(False, reason)``.

    A reason is the first matched pattern's friendly description, e.g.
    ``"subprocess call"``. Returns ``True`` for empty code (syntax is
    checked elsewhere) — this helper is purely about content.

    The match runs line-by-line so a multi-line candidate only flags the
    specific offending line; the returned reason is the friendly label
    (not the matched text) so callers can show it directly to operators.
    """
    if not isinstance(code, str) or not code:
        return True, None
    for line in code.splitlines():
        for pat, label in _DANGEROUS_CODE_PATTERNS:
            if pat.search(line):
                return False, label
    return True, None
