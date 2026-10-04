"""Bounded, redacted Telegram alert for a verified local deploy failure."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable

import httpx

from hive.core.deployment_ledger import MAX_ALERT_CLAIMS, DeployRecord
from hive.gateway.channels.base import OutgoingMessage, SendResult
from hive.gateway.channels.telegram import TelegramChannel

log = logging.getLogger("hive.autonomy.deployment_alert")
_REPO_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


def render_degraded_deploy_alert(record: DeployRecord, *, owner: str, repo: str) -> str:
    """Never include raw patches, paths, prompts, errors, credentials or chat IDs."""
    signals = ", ".join(record.failed_signals) or "unknown"
    lines = [
        "HiveOS deployment DEGRADED",
        f"Receipt: {record.id}",
        f"Expected commit: {record.expected_sha}",
        f"Failing signals: {signals}",
    ]
    if (record.baseline_sha and _SHA.fullmatch(record.baseline_sha)
            and _SHA.fullmatch(record.expected_sha)
            and _REPO_PART.fullmatch(owner) and _REPO_PART.fullmatch(repo)):
        lines.append(
            f"Diff: https://github.com/{owner}/{repo}/compare/"
            f"{record.baseline_sha}...{record.expected_sha}"
        )
    else:
        lines.append("Diff: unavailable (no verified baseline or repository)")
    lines.append("Review the receipt and incident before acting; no rollback was attempted.")
    return "\n".join(lines)


class DeploymentAlert:
    """Claim at most one degraded, process-bound gateway receipt per check."""

    def __init__(
        self, verifier, *, chat_id: str, owner: str, repo: str,
        sender: Callable[[OutgoingMessage], Awaitable[SendResult]],
    ) -> None:
        self._verifier = verifier
        self._chat_id = chat_id
        self._owner = owner
        self._repo = repo
        self._sender = sender
        self._claim_owner = f"deploy-alert:{uuid.uuid4().hex}"

    async def check(self) -> bool:
        receipt = self._verifier.claim_alert(self._claim_owner, lease_seconds=60)
        if receipt is None:
            return False
        message = OutgoingMessage(
            chat_id=self._chat_id,
            text=render_degraded_deploy_alert(receipt, owner=self._owner, repo=self._repo),
        )
        try:
            result = await asyncio.wait_for(self._sender(message), timeout=20)
            acknowledged = bool(result.ok and result.message_id)
        except Exception as exc:  # noqa: BLE001 - transport outcome may be uncertain
            log.warning("deployment alert transport failed (%s), receipt=%s",
                        type(exc).__name__, receipt.id)
            acknowledged = False
        if acknowledged:
            self._verifier.mark_alert_sent(
                receipt.id, self._claim_owner, claim_count=receipt.alert_claim_count,
            )
            log.info("deployment alert acknowledged, receipt=%s", receipt.id)
            return True
        if receipt.alert_claim_count >= MAX_ALERT_CLAIMS:
            self._verifier.mark_alert_exhausted(
                receipt.id, self._claim_owner, claim_count=receipt.alert_claim_count,
            )
            log.error("deployment alert delivery exhausted, receipt=%s", receipt.id)
        else:
            log.warning("deployment alert not acknowledged, receipt=%s", receipt.id)
        return False


def make_deployment_alert(hive) -> DeploymentAlert | None:
    """Create an opt-in sender with a fresh short-lived HTTP client per attempt."""
    cfg = hive.config
    if not cfg.deploy_alert_chat_id or hive.deploy_verifier is None:
        return None

    async def send(message: OutgoingMessage) -> SendResult:
        async with httpx.AsyncClient(timeout=15, trust_env=False,
                                     follow_redirects=False) as client:
            channel = TelegramChannel(cfg.telegram_token, client=client)
            return await channel.send(message)

    return DeploymentAlert(
        hive.deploy_verifier, chat_id=cfg.deploy_alert_chat_id,
        owner=cfg.github_owner, repo=cfg.github_repo, sender=send,
    )
