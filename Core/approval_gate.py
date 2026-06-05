"""
approval_gate.py — the danger firewall. NEVER weaken this file.

Hive acts freely. An action only pauses for Kamil's approval if it matches a
DANGEROUS pattern, requests MONEY, or touches IDENTITY/GATE. Everything else
runs. This is an allowlist of danger, not a nag.

Approvals queue here; surfaces (dashboard/Telegram/terminal) show them and the
owner approves or rejects by id.
"""
from __future__ import annotations
import re
import uuid
import logging

log = logging.getLogger("hiveos.gate")

DANGEROUS = [
    re.compile(r"\brm\s+-rf\b", re.I),
    re.compile(r"\bDROP\s+(TABLE|DATABASE)\b", re.I),
    re.compile(r"\bdelete\b.*\b(all|bulk|\*)\b", re.I),
    re.compile(r"\b(pay|payment|purchase|subscribe|transfer|trade|buy|sell)\b", re.I),
    re.compile(r"\b(send|post|tweet|publish|email)\b.*\b(external|public|customer)\b", re.I),
    re.compile(r"\bmerge\b.*\bmain\b", re.I),
    re.compile(r"\b(deploy|production|prod)\b", re.I),
    re.compile(r"\b(secret|credential|api[_-]?key|token|password)\b", re.I),
    re.compile(r"\b(shutdown|reboot|format|mkfs)\b", re.I),
]

DANGEROUS_TOOLS = {
    "shell_destructive", "payment", "spend_money",
    "external_message", "deploy", "merge_main",
}

# These two are ALWAYS human-approve regardless of context.
PROTECTED_PATHS = ("config/SOUL.md", "core/approval_gate.py")


class ApprovalGate:
    def __init__(self) -> None:
        self._pending: dict[str, dict] = {}

    def is_dangerous(self, tool: str, args: dict) -> bool:
        if tool in DANGEROUS_TOOLS:
            return True
        blob = f"{tool} {args}"
        if any(p in str(args.get('path', '')) for p in PROTECTED_PATHS):
            return True
        return any(p.search(blob) for p in DANGEROUS)

    def request(self, tool: str, args: dict, reason: str, kind: str = "danger") -> str:
        aid = uuid.uuid4().hex[:8]
        self._pending[aid] = {
            "id": aid, "tool": tool, "args": args, "reason": reason, "kind": kind,
        }
        log.warning("APPROVAL NEEDED [%s] %s (%s) %s", aid, tool, kind, args)
        return aid

    def request_money(self, what: str, amount: str, reason: str) -> str:
        return self.request("spend_money", {"what": what, "amount": amount}, reason, "money")

    def pending(self) -> list[dict]:
        return list(self._pending.values())

    def resolve(self, aid: str, approved: bool) -> dict | None:
        item = self._pending.pop(aid, None)
        if item:
            item["approved"] = approved
            log.info("Approval %s -> %s", aid, approved)
        return item


gate = ApprovalGate()
