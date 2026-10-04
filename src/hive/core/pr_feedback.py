"""Conservative decision seam for existing Hive-created PR feedback.

The decision is not an authorization token: the caller must re-fetch and
authenticate the PR creation receipt, head, sandbox, and policy gates before
every external write. Text from GitHub is never an instruction here.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any, Awaitable, Callable

from hive.core.redact import redact_known_secrets, register_secret_values

_MAX_FEEDBACK_ROUNDS = 2
_STANDDOWN_REASONS = {
    "round_cap": "CI repair limit reached",
    "repair_failed": "The local candidate repair failed",
    "feedback_ambiguous": "The repair outcome could not be confirmed",
    "review_ambiguous": "Review feedback needs a human decision",
    "policy_blocked": "Candidate safety policy declined the repair",
}
Poster = Callable[[int, str], Awaitable[int]]
Repairer = Callable[[str, str], Awaitable[dict[str, Any]]]


def _safe_failed_checks(snapshot: dict[str, Any]) -> tuple[str, ...]:
    checks = snapshot.get("checks")
    if not isinstance(checks, list):
        return ()
    names: list[str] = []
    for check in checks:
        if not isinstance(check, dict) or check.get("conclusion") not in {
            "failure", "timed_out", "cancelled", "action_required", "startup_failure",
        }:
            continue
        name = check.get("name")
        if not isinstance(name, str) or redact_known_secrets(name) != name:
            continue
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._/-]{0,79}", name) is None:
            continue
        if name not in names:
            names.append(name)
        if len(names) == 3:
            break
    return tuple(names)


def _failed_ci_status(snapshot: dict[str, Any]) -> bool:
    """Accept complete failed CI on a draft without relaxing other CI states."""
    draft = snapshot.get("draft", False)
    return (
        type(draft) is bool
        and snapshot.get("ci_state") == "failed"
        and (
            (snapshot.get("status") == "draft" and draft)
            or (snapshot.get("status") == "checks_failed" and not draft)
        )
    )


def feedback_action(
    snapshot: dict[str, Any], *, authenticated_creation: bool,
    rounds_used: object,
) -> str:
    """Return ``repair``, ``stand_down`` or ``wait`` from conclusive evidence."""
    if (
        authenticated_creation is not True
        or not isinstance(snapshot, dict)
        or snapshot.get("ownership_verified") is not True
        or snapshot.get("state") != "open"
        or not _failed_ci_status(snapshot)
        or type(snapshot.get("number")) is not int
        or snapshot["number"] <= 0
        or type(rounds_used) is not int
        or not 0 <= rounds_used <= _MAX_FEEDBACK_ROUNDS
    ):
        return "wait"
    return "stand_down" if rounds_used == _MAX_FEEDBACK_ROUNDS else "repair"


def _matching_new_head(
    identity: dict[str, Any], snapshot: dict[str, Any], *,
    pr_url: str, new_sha: str,
) -> bool:
    """Check immutable PR identity while allowing only its head commit to move."""
    if not isinstance(identity, dict) or not isinstance(snapshot, dict):
        return False
    if snapshot.get("state") != "open" or snapshot.get("url") != pr_url:
        return False
    if snapshot.get("head_sha") != new_sha:
        return False
    return all(
        snapshot.get(observed) == identity.get(stored)
        for observed, stored in (
            ("number", "pr_number"), ("pr_id", "pr_id"),
            ("author_id", "author_id"), ("head_repo_id", "head_repo_id"),
            ("base_repo_id", "base_repo_id"), ("head_ref", "branch"),
            ("base_ref", "base_ref"),
        )
    ) and identity.get("bound") is True


async def repair_failed_ci_once(
    ledger: Any, observer: Any, repairer: Repairer, *, run_id: str,
    pr_url: str, snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Reserve one CI round, invoke a gated repairer, and verify the moved PR head.

    The repairer is a trusted local adapter to the sandboxed SelfModifier. It
    receives only branch and exact expected SHA, never GitHub review text. Any
    ambiguous push/GET leaves the reservation spent and needs human inspection.
    """
    if (
        not isinstance(snapshot, dict) or snapshot.get("url") != pr_url
        or snapshot.get("ownership_verified") is not True
    ):
        return {"status": "wait"}
    try:
        authenticated = ledger.validate_pr_identity(run_id, pr_url, snapshot)
        identity = ledger.get_pr_identity(pr_url)
        rounds = ledger.get_pr_feedback_rounds(pr_url)
    except Exception:  # noqa: BLE001 - storage failure cannot authorize a push
        return {"status": "wait"}
    if not isinstance(rounds, list) or any(
        not isinstance(row, dict)
        or type(row.get("round")) is not int or row["round"] != index + 1
        or row.get("state") != "pushed"
        for index, row in enumerate(rounds)
    ):
        return {"status": "wait"}
    if feedback_action(
        snapshot, authenticated_creation=authenticated, rounds_used=len(rounds),
    ) != "repair" or not isinstance(identity, dict):
        return {"status": "wait"}
    try:
        observed = await observer.observe(snapshot["number"])
        live = observed.as_dict() if hasattr(observed, "as_dict") else observed
        if not isinstance(live, dict):
            return {"status": "wait"}
        live = dict(live)
        live_authenticated = ledger.validate_pr_identity(run_id, pr_url, live)
        live["ownership_verified"] = live_authenticated
        if (
            live.get("url") != pr_url
            or live.get("head_sha") != snapshot.get("head_sha")
            or not live_authenticated
            or feedback_action(
                live, authenticated_creation=live_authenticated, rounds_used=len(rounds),
            ) != "repair"
        ):
            return {"status": "wait"}
    except Exception:  # noqa: BLE001 - fresh GET required before reservation
        return {"status": "wait"}
    old_sha = live.get("head_sha")
    branch = identity.get("branch")
    if (
        not isinstance(old_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", old_sha) is None
        or identity.get("pushed_sha") != old_sha
        or not isinstance(branch, str)
        or re.fullmatch(r"hive/auto-[a-z0-9-]+", branch) is None
    ):
        return {"status": "wait"}
    try:
        reservation = ledger.reserve_pr_feedback_round(
            run_id, pr_url, live, feedback_key=f"ci:{old_sha}",
        )
    except Exception:  # noqa: BLE001 - no reservation means no external write
        return {"status": "wait"}
    if reservation is None:
        return {"status": "already_reserved"}
    round_number = reservation.get("round") if isinstance(reservation, dict) else None
    if (
        type(round_number) is not int
        or round_number != len(rounds) + 1
        or reservation.get("expected_sha") != old_sha
    ):
        return {"status": "uncertain"}

    def finish(state: str, new_sha: str = "") -> bool:
        try:
            return bool(ledger.finish_pr_feedback_round(
                pr_url, round_number, state=state, new_sha=new_sha,
            ))
        except Exception:  # noqa: BLE001 - reservation remains one-shot
            return False

    try:
        result = await repairer(branch, old_sha)
    except asyncio.CancelledError:
        finish("uncertain")
        raise
    except Exception:  # noqa: BLE001 - repair error must not disclose secrets
        finish("uncertain")
        return {"status": "uncertain"}
    if not isinstance(result, dict) or result.get("ok") is not True:
        stage = result.get("stage") if isinstance(result, dict) else None
        state = "uncertain" if stage in {"push", "push_uncertain", "post_push"} else "failed"
        finish(state)
        return {"status": state}
    new_sha = result.get("head_sha")
    if (
        result.get("stage") != "pushed" or not isinstance(new_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", new_sha) is None
        or new_sha == old_sha
    ):
        finish("uncertain")
        return {"status": "uncertain"}
    try:
        observed = await observer.observe(snapshot["number"])
        live = observed.as_dict() if hasattr(observed, "as_dict") else observed
    except asyncio.CancelledError:
        finish("uncertain")
        raise
    except Exception:  # noqa: BLE001 - GET outcome may be ambiguous
        finish("uncertain")
        return {"status": "uncertain"}
    if not _matching_new_head(identity, live, pr_url=pr_url, new_sha=new_sha):
        finish("uncertain")
        return {"status": "uncertain"}
    if not finish("pushed", new_sha):
        return {"status": "uncertain"}
    return {"status": "pushed", "round": round_number, "head_sha": new_sha}


class GitHubPRCommenter:
    """Post one deterministic stand-down message after a durable reservation.

    This transport does not decide whether a PR is owned or eligible. The
    caller must verify the authenticated creation receipt, reserve the one
    allowed message, and never blindly retry an ambiguous POST result.
    """

    def __init__(self, token: str, owner: str, repo: str, *,
                 poster: Poster | None = None) -> None:
        if not token or not all(
            value not in {".", ".."}
            and re.fullmatch(r"[A-Za-z0-9_.-]+", value or "")
            for value in (owner, repo)
        ):
            raise ValueError("GitHub comment transport requires fixed repository credentials")
        register_secret_values([token])
        self._token = token
        self._owner = owner
        self._repo = repo
        self._poster = poster

    async def post_standdown(self, number: int, *, marker: str,
                             reason_code: str,
                             failed_checks: tuple[str, ...] = ()) -> int:
        if type(number) is not int or number <= 0 or reason_code not in _STANDDOWN_REASONS:
            raise ValueError("invalid stand-down target or reason")
        try:
            if str(uuid.UUID(marker)) != marker:
                raise ValueError("non-canonical marker")
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid stand-down marker") from exc
        if not isinstance(failed_checks, tuple):
            failed_checks = ()
        names = tuple(name for name in failed_checks[:3] if (
            isinstance(name, str) and redact_known_secrets(name) == name
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._/-]{0,79}", name) is not None
        ))
        failing = ", ".join(names) if names else "not safely identified"
        body = (
            f"Hive is standing down on this PR: {_STANDDOWN_REASONS[reason_code]}.\n"
            f"Failing CI checks: {failing}.\n"
            "No further automated changes will be attempted for this signal. "
            "A human review is required.\n\n"
            f"<!-- hive-standdown:{marker} -->"
        )
        if self._poster is not None:
            comment_id = await self._poster(number, body)
        else:
            import httpx

            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    f"https://api.github.com/repos/{self._owner}/{self._repo}/"
                    f"issues/{number}/comments",
                    headers={"Authorization": f"Bearer {self._token}",
                             "Accept": "application/vnd.github+json"},
                    json={"body": body},
                )
            if response.status_code != 201:
                raise RuntimeError("GitHub stand-down comment was not confirmed")
            try:
                comment_id = response.json().get("id")
            except (ValueError, TypeError, AttributeError) as exc:
                raise RuntimeError("GitHub stand-down comment response was invalid") from exc
        if type(comment_id) is not int or comment_id <= 0:
            raise RuntimeError("GitHub stand-down comment response lacked an ID")
        return comment_id


async def stand_down_once(
    ledger: Any, commenter: GitHubPRCommenter, *, run_id: str,
    pr_url: str, snapshot: dict[str, Any], observer: Any,
) -> dict[str, Any]:
    """Recheck live PR identity and reserve one public comment POST."""
    if (
        not isinstance(snapshot, dict) or snapshot.get("url") != pr_url
        or snapshot.get("ownership_verified") is not True
    ):
        return {"status": "wait"}
    try:
        live_result = await observer.observe(snapshot["number"])
        live = live_result.as_dict() if hasattr(live_result, "as_dict") else live_result
        if not isinstance(live, dict):
            return {"status": "wait"}
        live = dict(live)
        if (
            live.get("head_sha") != snapshot.get("head_sha")
            or live.get("url") != pr_url
        ):
            return {"status": "wait"}
        authenticated = ledger.validate_pr_identity(run_id, pr_url, live)
        live["ownership_verified"] = authenticated
        rounds = ledger.get_pr_feedback_rounds(pr_url)
    except Exception:  # noqa: BLE001 - storage failure cannot authorize a write
        return {"status": "wait"}
    if not isinstance(rounds, list) or not 1 <= len(rounds) <= _MAX_FEEDBACK_ROUNDS or any(
        not isinstance(row, dict)
        or type(row.get("round")) is not int or row["round"] != index + 1
        or (row.get("state") != "pushed" if index < len(rounds) - 1
            else row.get("state") not in {"pushed", "failed", "uncertain"})
        for index, row in enumerate(rounds)
    ):
        return {"status": "wait"}
    if (
        not authenticated or live.get("ownership_verified") is not True
        or live.get("state") != "open"
        or not _failed_ci_status(live)
    ):
        return {"status": "wait"}
    last_state = rounds[-1]["state"]
    if last_state == "pushed":
        if feedback_action(
            live, authenticated_creation=True, rounds_used=len(rounds),
        ) != "stand_down":
            return {"status": "wait"}
        reason = "round_cap"
    else:
        reason = "repair_failed" if last_state == "failed" else "feedback_ambiguous"
    try:
        marker = ledger.reserve_pr_standdown(
            run_id, pr_url, live, reason_code=reason,
        )
    except Exception:  # noqa: BLE001 - reservation failure cannot authorize a POST
        return {"status": "wait"}
    if marker is None:
        return {"status": "already_reserved"}
    try:
        refreshed_result = await observer.observe(live["number"])
        refreshed = (
            refreshed_result.as_dict() if hasattr(refreshed_result, "as_dict")
            else refreshed_result
        )
        if isinstance(refreshed, dict):
            refreshed = dict(refreshed)
            refreshed["ownership_verified"] = ledger.validate_pr_identity(
                run_id, pr_url, refreshed,
            )
        if (
            not isinstance(refreshed, dict)
            or refreshed != live
            or refreshed.get("ownership_verified") is not True
        ):
            ledger.mark_pr_standdown(pr_url, marker, state="uncertain")
            return {"status": "uncertain"}
        comment_id = await commenter.post_standdown(
            live["number"], marker=marker, reason_code=reason,
            failed_checks=_safe_failed_checks(live),
        )
    except asyncio.CancelledError:
        try:
            ledger.mark_pr_standdown(pr_url, marker, state="uncertain")
        except Exception:  # noqa: BLE001 - the reservation still prevents a retry
            pass
        raise
    except Exception:  # noqa: BLE001 - ambiguous POST must never be retried
        try:
            ledger.mark_pr_standdown(pr_url, marker, state="uncertain")
        except Exception:  # noqa: BLE001 - do not disclose storage error text
            pass
        return {"status": "uncertain"}
    try:
        confirmed = ledger.mark_pr_standdown(pr_url, marker, state="posted")
    except Exception:  # noqa: BLE001 - reservation still blocks another POST
        confirmed = False
    return {"status": "posted", "comment_id": comment_id} if confirmed else {
        "status": "uncertain",
    }
