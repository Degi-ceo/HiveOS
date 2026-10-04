"""
self_mod.py — safe self-modification engine (KEEP+ADAPT from Core/self_mod.py).

How Hive changes its OWN code without destroying itself. The flow from SOUL.md is
non-negotiable:
  1. snapshot last-known-good HEAD (instant rollback)
  2. isolated git worktree on a new branch (never live main)
  3. apply changes only inside the worktree
  4. run tests in the candidate
  5. fail -> discard worktree, stay on last-known-good, record
  6. pass -> commit + push branch + open PR (NEVER merge); a human merges
Changes touching SOUL.md / approval_gate.py are refused outright.

`dry_run=True` runs steps 1–4 and skips push/PR (the P8 verify). The shell runner
is injectable so the flow is unit-testable without real git. Depends on core only.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import posixpath
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Protocol
from urllib.parse import unquote, unquote_plus, urlparse

from hive.core.approval import PROTECTED_PATHS
from hive.core.child_env import without_privileged_credentials
from hive.core.events import EventBus, EventType
from hive.core.redact import (
    known_secret_values,
    redact_known_secrets,
    redact_value,
    register_secret_values,
)
from hive.core.run_context import current_run_id
from hive.core.secret_scan import SecretFinding, scan_added_diff, scan_candidate_paths

log = logging.getLogger("hive.selfmod")

# (cmd, cwd) -> (returncode, stdout on success; stdout + stderr on failure)
# cmd may be a list (exec, safe) or a plain string (shell, for trusted git sub-commands).
Runner = Callable[[str | list[str], str | None], Awaitable[tuple[int, str]]]
# (worktree_path) -> list of changed repo-relative paths
ApplyFn = Callable[[str], Awaitable[list[str]]]
CandidateGate = Callable[[str, str, list[str], str, str], Awaitable[dict[str, Any]]]
SecretScanner = Callable[[str], list[SecretFinding]]


@dataclass(frozen=True, slots=True)
class CandidateFailure:
    """Redacted, bounded context supplied to one candidate-repair attempt."""

    attempt: int
    test_log: str
    fingerprint: str
    staged_diff: str = ""
    run_id: str = ""
    changed_paths: tuple[str, ...] = ()


RepairFn = Callable[[CandidateFailure], Awaitable[ApplyFn | None]]
FreshPRCheck = Callable[[str, str], Awaitable[dict[str, Any]]]
_MAX_REPAIR_ATTEMPTS = 2  # Extra repairs; the initial test is attempt one.
_REPAIR_RAW_CONTEXT_MAX_BYTES = 65_536
_REPAIR_TEST_LOG_MAX_BYTES = 2_000
_REPAIR_STAGED_DIFF_MAX_BYTES = 4_096
_REPAIR_DECODE_MAX_LAYERS = 16
_REPAIR_SECRET_EVIDENCE_REDACTION = "[redacted credential-bearing candidate evidence]"
_REPAIR_OVERSIZED_EVIDENCE_REDACTION = "[omitted oversized candidate evidence]"


def repair_evidence_withheld(failure: CandidateFailure) -> bool:
    """Do not ask a model to repair when its evidence was withheld for safety."""
    return any(
        marker in evidence
        for evidence in (failure.test_log, failure.staged_diff)
        for marker in (
            _REPAIR_SECRET_EVIDENCE_REDACTION,
            _REPAIR_OVERSIZED_EVIDENCE_REDACTION,
        )
    )


@dataclass(frozen=True, slots=True)
class _CandidateState:
    branch: str
    worktree: str
    last_good: str
    reported_changed: tuple[str, ...]
    tested_digest: str


@dataclass(frozen=True, slots=True)
class _ExistingPR:
    branch: str
    expected_head: str
    verify_fresh_pr: FreshPRCheck
    source_paths: tuple[str, ...] = ()


_EXISTING_PR_BRANCH = re.compile(r"hive/auto-(?:[a-z0-9]{1,8}-)?[0-9a-f]{32}\Z")
_GIT_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _existing_pr_source_path(path: str) -> bool:
    normalized = _normalize_changed_path(path).casefold()
    # GitHub feedback is untrusted. In AUTO, only non-executable documentation
    # text may be changed; any code, test, workflow, or configuration requires
    # explicit REVIEW approval even if the generic path policy is more lenient.
    return not (
        normalized.startswith("docs/")
        and normalized.endswith((".md", ".rst", ".txt"))
    )


def _safe_repair_excerpt(
    raw: str, *, max_bytes: int, tail: bool = False,
    secret_values: Iterable[str] = (),
) -> str:
    """Redact before byte truncation; omit unbounded input rather than split a secret."""
    if len(raw) > _REPAIR_RAW_CONTEXT_MAX_BYTES:
        return _REPAIR_OVERSIZED_EVIDENCE_REDACTION
    encoded = raw.encode("utf-8", errors="replace")
    if len(encoded) > _REPAIR_RAW_CONTEXT_MAX_BYTES:
        return _REPAIR_OVERSIZED_EVIDENCE_REDACTION
    fragments = tuple(
        part.strip()
        for value in (*known_secret_values(), *secret_values)
        for part in value.replace("\r", "\n").replace(",", "\n").split("\n")
        if part.strip()
    )
    if fragments:
        for decoder in (unquote, unquote_plus):
            layer = raw
            # Bound decoding time even for adversarial nesting. Deeply nested
            # input is omitted instead of being passed through unchecked.
            for _ in range(_REPAIR_DECODE_MAX_LAYERS):
                if any(fragment in layer for fragment in fragments):
                    return _REPAIR_SECRET_EVIDENCE_REDACTION
                decoded = decoder(layer)
                if decoded == layer or len(decoded) > len(layer):
                    break
                if len(decoded) == len(layer):
                    if any(fragment in decoded for fragment in fragments):
                        return _REPAIR_SECRET_EVIDENCE_REDACTION
                    break
                layer = decoded
            else:
                return _REPAIR_SECRET_EVIDENCE_REDACTION
    safe = redact_known_secrets(raw).encode("utf-8")
    if len(safe) <= max_bytes:
        return safe.decode("utf-8")
    excerpt = safe[-max_bytes:] if tail else safe[:max_bytes]
    return excerpt.decode("utf-8", errors="ignore")


class HistoryStore(Protocol):
    """Injected durable self-mod history surface (implemented by observability)."""

    def record_selfmod(self, record: dict[str, Any]) -> int: ...
    def selfmod_history(self, *, limit: int = 20) -> list[dict[str, Any]]: ...
    def clear_selfmod_history(self) -> int: ...


async def _default_run(cmd: str | list[str], cwd: str | None = None) -> tuple[int, str]:
    child_env = without_privileged_credentials()
    if isinstance(cmd, list):
        # Use exec (no shell interpretation) for commands with LLM-sourced arguments.
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=child_env)
    else:
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=child_env)
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode is None:
        raise RuntimeError("subprocess finished without a return code")
    stdout = out.decode(errors="replace")
    stderr = err.decode(errors="replace")
    return int(proc.returncode), stdout if proc.returncode == 0 else stdout + stderr


@dataclass(frozen=True, slots=True)
class PRCreationReceipt:
    """Whitelisted identity from the authenticated create-PR response, not a later GET."""

    url: str
    number: int
    pr_id: int
    author_id: int
    head_repo_id: int
    base_repo_id: int
    head_ref: str
    head_sha: str
    base_ref: str

    def as_dict(self) -> dict[str, str | int]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def matches_candidate(self, branch: str, head_sha: str) -> bool:
        if not all(isinstance(value, str) for value in (
            self.url, self.head_ref, self.head_sha, self.base_ref,
        )):
            return False
        parsed = urlparse(self.url)
        match = re.fullmatch(r"/[^/]+/[^/]+/pull/([1-9][0-9]*)", parsed.path)
        return bool(
            parsed.scheme == "https" and parsed.netloc.casefold() == "github.com"
            and not parsed.query and not parsed.fragment and match is not None
            and int(match.group(1)) == self.number
            and all(type(value) is int and value > 0 for value in (
                self.number, self.pr_id, self.author_id,
                self.head_repo_id, self.base_repo_id,
            ))
            and self.head_repo_id == self.base_repo_id
            and self.head_ref == branch and self.base_ref == "main"
            and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", self.head_sha) is not None
            and self.head_sha == head_sha
        )


# Legacy injected openers may return a URL for observation; only a complete
# structured receipt can provide creation provenance for later writer authority.
PROpener = Callable[[str, str, str], Awaitable[PRCreationReceipt | str | None]]


def github_pr_opener(token: str, owner: str, repo: str, *, base: str = "main",
                     draft: bool = True) -> PROpener:
    """Real PR opener over the GitHub REST API using Hive's own token (#si-3).

    Hive opens a DRAFT PR from its pushed branch and NEVER merges — a human merges
    (SOUL.md hard rule). httpx is imported lazily so importing self_mod never
    requires it."""
    register_secret_values([token])

    async def open_pr(branch: str, title: str, body: str) -> PRCreationReceipt | str | None:
        if not (token and owner and repo):
            return None
        import httpx
        url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
        payload = redact_value({"title": title, "head": branch, "base": base,
                                "body": body, "draft": draft})
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(url, json=payload, headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                })
            if r.status_code in (200, 201):
                response = r.json()
                if not isinstance(response, dict):
                    return None
                pr_url = response.get("html_url")
                if not isinstance(pr_url, str):
                    return None
                head = response.get("head")
                base_data = response.get("base")
                user = response.get("user")
                if r.status_code == 201 and all(
                    isinstance(value, dict) for value in (head, base_data, user)
                ):
                    head_repo = head.get("repo")
                    base_repo = base_data.get("repo")
                    if isinstance(head_repo, dict) and isinstance(base_repo, dict):
                        receipt = PRCreationReceipt(
                            url=pr_url, number=response.get("number"),
                            pr_id=response.get("id"), author_id=user.get("id"),
                            head_repo_id=head_repo.get("id"),
                            base_repo_id=base_repo.get("id"),
                            head_ref=head.get("ref"), head_sha=head.get("sha"),
                            base_ref=base_data.get("ref"),
                        )
                        expected_path = f"/{owner}/{repo}/pull/{receipt.number}"
                        if (
                            receipt.matches_candidate(branch, receipt.head_sha)
                            and urlparse(receipt.url).path.casefold()
                            == expected_path.casefold()
                        ):
                            return receipt
                return pr_url
            log.warning(
                "PR open failed (%s): %s", r.status_code,
                redact_known_secrets(r.text[:300]),
            )
        except Exception as exc:  # noqa: BLE001 - PR opening is best-effort
            log.warning("PR open error: %s", redact_known_secrets(str(exc)))
        return None

    return open_pr


_PROTECTED_NAMES = {p.rsplit("/", 1)[-1].lower() for p in PROTECTED_PATHS}
_PROTECTED_PATHS_LOWER = {p.lower().replace("\\", "/") for p in PROTECTED_PATHS}


def _touches_protected(changed: list[str]) -> bool:
    """True if any changed path is a PROTECTED file."""
    for cp in changed:
        norm = cp.replace("\\", "/").lower()
        if any(norm == pp or norm.endswith("/" + pp) for pp in _PROTECTED_PATHS_LOWER):
            return True
        basename = norm.rsplit("/", 1)[-1]
        if basename in _PROTECTED_NAMES:
            return True
    return False


def _normalize_changed_path(path: str) -> str:
    """Return one normalized, repository-relative comparison form for a path."""
    normalized = posixpath.normpath(path.replace("\\", "/"))
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


async def candidate_artifact_digest(
    run: Runner, worktree: str, changed: list[str],
) -> str:
    """Hash the exact staged tree that a successful proposal will commit.

    The candidate must already be fully staged. ``git write-tree`` binds the
    digest to Git's canonical blob bytes, modes, deletions, and paths, including
    any clean/EOL filters. Working-tree symlinks remain unsupported because
    they can change meaning between evaluation and checkout.
    """
    root = Path(worktree).resolve()
    for raw_path in sorted(set(changed)):
        normalized = _normalize_changed_path(raw_path)
        first = normalized.split("/", 1)[0]
        if (
            not normalized
            or normalized == ".."
            or normalized.startswith(("../", "/"))
            or ":" in first
        ):
            raise ValueError(f"unsafe candidate path: {raw_path!r}")
        path = root.joinpath(*normalized.split("/"))
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"candidate file escapes worktree: {raw_path!r}") from exc
        if path.is_symlink():
            raise ValueError(f"candidate symlinks are not supported: {raw_path!r}")
    rc, tree_out = await run(["git", "write-tree"], worktree)
    tree_oid = tree_out.strip()
    if rc != 0 or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", tree_oid) is None:
        raise OSError("unable to identify staged candidate tree")
    return hashlib.sha256(f"git-tree\0{tree_oid.lower()}".encode()).hexdigest()


async def _actual_changed_files(run: Runner, worktree: str) -> tuple[int, list[str], str]:
    """Read every tracked and untracked candidate change from Git.

    The callback's result is only an assertion. Git is the source of truth before
    tests, staging, committing, and pushing are allowed to proceed.
    """
    rc, diff_out = await run(
        ["git", "diff", "--name-only", "-z", "--no-renames", "HEAD", "--"],
        worktree,
    )
    if rc != 0:
        return rc, [], diff_out
    rc, untracked_out = await run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], worktree,
    )
    if rc != 0:
        return rc, [], untracked_out
    separator = "\0" if "\0" in diff_out or "\0" in untracked_out else "\n"
    changed = {
        _normalize_changed_path(line)
        for line in (diff_out + separator + untracked_out).split(separator)
        if line.strip()
    }
    return 0, sorted(path for path in changed if path), ""


async def _candidate_present_files(
    run: Runner, worktree: str,
) -> tuple[int, list[str], str]:
    """Read changed paths that remain present in the staged candidate tree."""
    rc, output = await run(
        [
            "git", "diff", "--cached", "--name-only",
            "--diff-filter=ACMRTUXB", "-z", "--", ".",
        ],
        worktree,
    )
    if rc != 0:
        return rc, [], output
    separator = "\0" if "\0" in output else "\n"
    paths = {
        _normalize_changed_path(item)
        for item in output.split(separator)
        if item.strip()
    }
    return 0, sorted(path for path in paths if path), ""


def _review_required_paths(paths: list[str]) -> list[str]:
    """Return actual paths that cannot remain on the autonomous AUTO path."""
    # This import must stay lazy: spec_search imports SelfModifier.
    from hive.core.spec_search import path_requires_review

    return [path for path in paths if path_requires_review(path)]


async def _verify_candidate_changes(
    run: Runner, worktree: str, reported_changed: list[str], *, approved_review: bool,
) -> dict:
    """Fail closed unless Git matches the callback and policy permits the paths."""
    if not isinstance(reported_changed, list) or any(
        not isinstance(path, str) for path in reported_changed
    ):
        return {
            "ok": False,
            "stage": "changed_files",
            "msg": "apply_fn must report a list of repository-relative paths",
        }
    rc, actual_changed, error = await _actual_changed_files(run, worktree)
    if rc != 0:
        return {
            "ok": False,
            "stage": "changed_files",
            "msg": "unable to verify candidate worktree changes",
            "log": error[-1000:],
        }
    ignored_rc, ignored_out = await run(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard"],
        worktree,
    )
    if ignored_rc != 0:
        return {
            "ok": False,
            "stage": "changed_files",
            "msg": "unable to verify ignored candidate files",
            "log": ignored_out[-1000:],
        }
    ignored = sorted(line.strip() for line in ignored_out.splitlines() if line.strip())
    if ignored:
        return {
            "ok": False,
            "stage": "changed_files",
            "msg": "candidate created ignored files that are outside the staged artifact",
            "ignored": ignored,
        }
    reported_set = {_normalize_changed_path(path) for path in reported_changed}
    actual_set = set(actual_changed)
    if _touches_protected(actual_changed):
        return {
            "ok": False,
            "stage": "protected",
            "msg": "actual change touches SOUL.md or approval gate — human-only",
        }
    if reported_set != actual_set:
        log.warning(
            "self_mod BLOCKED: callback paths differ from Git paths; reported=%s actual=%s",
            redact_value(sorted(reported_set)), redact_value(actual_changed),
        )
        return {
            "ok": False,
            "stage": "changed_files",
            "msg": "apply_fn file list does not match actual Git changes",
            "reported": sorted(reported_set),
            "actual": actual_changed,
        }
    review_paths = _review_required_paths(actual_changed)
    if review_paths and not approved_review:
        return {
            "ok": False,
            "stage": "review_required",
            "msg": "actual change requires REVIEW tier before commit",
            "review_paths": review_paths,
            "changed": actual_changed,
        }
    return {"changed": actual_changed}


_MAX_HISTORY = 50   # keep at most this many proposal records in memory


def _parse_worktree_list(porcelain: str) -> list[tuple[str, str]]:
    """Parse `git worktree list --porcelain` output into (path, branch) pairs.

    Detached worktrees (no ``branch`` line) are skipped — SelfModifier always
    creates a named branch (``hive/auto-<ts>``), so a detached entry can never
    be one of ours.
    """
    out: list[tuple[str, str]] = []
    path: str | None = None
    branch: str | None = None
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
            branch = None
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        elif line == "":
            if path and branch:
                out.append((path, branch))
            path = None
            branch = None
    if path and branch:
        out.append((path, branch))
    return out


class SelfModifier:
    def __init__(self, *, repo_root: str = ".", run: Runner | None = None,
                 test_cmd: str = "python -m pytest -q",
                 open_pr: PROpener | None = None,
                 bus: EventBus | None = None, history_store: HistoryStore | None = None,
                 audit: Callable[[dict[str, Any]], None] | None = None,
                 secret_scanner: SecretScanner = scan_added_diff,
                 secret_values: Iterable[str] = ()) -> None:
        self._root = repo_root
        self._run = run or _default_run
        self._test_cmd = test_cmd
        self._open_pr = open_pr
        self._bus = bus
        self._history_store = history_store
        self._audit = audit
        self._secret_scanner = secret_scanner
        self._repair_secret_values = frozenset(
            value for value in secret_values if isinstance(value, str) and value
        )
        self._history: list[dict] = []   # recent proposal outcomes (capped at _MAX_HISTORY)

    def _emit(self, event_type: EventType, data: dict) -> None:
        if self._bus is not None:
            try:
                self._bus.publish(event_type, data)
            except Exception:  # noqa: BLE001 - observability must not break self-mod
                pass

    def history(self, limit: int = 20) -> list[dict]:
        """Return the most recent proposal outcomes (newest first), capped to `limit`."""
        if self._history_store is not None:
            persisted = self._history_store.selfmod_history(limit=_MAX_HISTORY)
            # Persisted rows are canonical. Keep only in-memory records whose
            # ledger write failed, so independent but identical outcomes remain
            # distinct and immediately visible results remain backwards-compatible.
            pending = [record for record in self._history if "_ledger_id" not in record]
            indexed = list(enumerate([*persisted, *reversed(pending)]))
            indexed.sort(key=lambda item: (float(item[1].get("ts", 0.0)), -item[0]),
                         reverse=True)
            return [record for _, record in indexed[:min(max(1, limit), _MAX_HISTORY)]]
        return list(reversed(self._history[-_MAX_HISTORY:]))[:limit]

    @property
    def last_result(self) -> dict | None:
        """The outcome dict from the most recent propose() call, or None."""
        history = self.history(limit=1)
        return history[0] if history else None

    def recent_branches(self, n: int = 5) -> list[str]:
        """Return up to n branch names from the most recent successful proposals (newest first)."""
        branches = []
        for record in self.history(limit=_MAX_HISTORY):
            if record.get("ok") and record.get("branch"):
                branches.append(record["branch"])
                if len(branches) >= n:
                    break
        return branches

    def clear_history(self) -> int:
        """Discard all recorded proposal history. Returns the count cleared."""
        if self._history_store is not None:
            self._history = []
            return self._history_store.clear_selfmod_history()
        count = len(self._history)
        self._history = []
        return count

    def proposal_count(self) -> int:
        """Return the total number of proposals recorded in history (capped at _MAX_HISTORY)."""
        return len(self.history(limit=_MAX_HISTORY))

    def success_rate(self) -> float:
        """Fraction of proposals that succeeded (ok=True). Returns 0.0 if no history."""
        history = self.history(limit=_MAX_HISTORY)
        if not history:
            return 0.0
        ok = sum(1 for r in history if r.get("ok"))
        return round(ok / len(history), 4)

    def failed_proposals(self, limit: int = 10) -> list[dict]:
        """Return the most recent failed proposals (ok=False), newest first."""
        failed = [r for r in self.history(limit=_MAX_HISTORY) if not r.get("ok")]
        return failed[:max(1, limit)]

    def proposals_by_stage(self) -> dict[str, int]:
        """Return a count of proposals grouped by their terminal stage.

        Useful for spotting patterns: if 'test' dominates, the tests are too brittle;
        if 'protected' dominates, the diagnoser keeps targeting locked files."""
        counts: dict[str, int] = {}
        for r in self.history(limit=_MAX_HISTORY):
            stage = str(r.get("stage") or "unknown")
            counts[stage] = counts.get(stage, 0) + 1
        return counts

    async def sweep_orphaned_worktrees(self) -> dict:
        """Startup crash-recovery: reclaim any self-mod worktree/branch left behind
        by a process that was killed mid-``propose()`` — between ``git worktree
        add`` and the ``finally`` cleanup in ``_propose_inner`` (which only runs
        on a normal return/exception, never on SIGKILL/OOM/container restart).

        Mirrors ``TaskBoard.requeue_running()`` for the self-mod side of
        autonomy: it does NOT try to resume the half-finished edit (the
        `apply_fn` closure that produced it is gone with the dead process
        anyway) — it only reclaims disk/git state. Per ADR 005's fail-forward
        philosophy, the heartbeat re-detects the original symptom and
        re-proposes a fresh edit on its own; this just stops orphaned
        ``.worktrees/hive-auto-*`` directories and branches from accumulating
        forever.

        Safe to call on a clean start: with nothing orphaned, ``git worktree
        list`` has no ``hive/auto-*`` entries and this is a no-op.
        """
        removed: list[str] = []
        errors: list[str] = []
        rc, out = await self._run(["git", "worktree", "list", "--porcelain"], self._root)
        if rc != 0:
            return {"removed": removed, "errors": [out[:300]]}
        for path, branch in _parse_worktree_list(out):
            # Defense in depth: require BOTH the branch name and the worktree
            # path to match what _propose_inner actually creates (path derives
            # from branch via `.replace("/", "-")`, see the `wt =` line above).
            # A branch-name-only check would delete a human's worktree if they
            # ever happened to name a branch `hive/auto-<anything>` by hand;
            # this way that would need the exact matching directory too.
            if not branch.startswith("hive/auto-"):
                continue
            expected_dir = Path(self._root) / ".worktrees" / branch.replace("/", "-")
            if Path(path).resolve() != expected_dir.resolve():
                continue
            rc2, out2 = await self._run(["git", "worktree", "remove", "--force", path],
                                        self._root)
            if rc2 != 0:
                errors.append(f"{path}: {out2[:200]}")
                continue
            removed.append(path)
            rc3, out3 = await self._run(["git", "branch", "-D", branch], self._root)
            if rc3 != 0:
                log.warning(
                    "self_mod: orphaned branch cleanup failed for %s: %s",
                    redact_known_secrets(branch), redact_known_secrets(out3[:200]),
                )
        # Clear stale metadata for any worktree whose directory is already gone
        # (e.g. the container's ephemeral disk was wiped but .git/worktrees
        # bookkeeping survived on a persistent volume).
        await self._run(["git", "worktree", "prune"], self._root)
        if removed:
            log.info("self_mod: swept %d orphaned worktree(s) from a prior crashed run: %s",
                     len(removed), removed)
        return {"removed": removed, "errors": errors}

    async def _remote_head(self, branch: str) -> str | None:
        ref = f"refs/heads/{branch}"
        rc, output = await self._run(
            ["git", "ls-remote", "--exit-code", "origin", ref], self._root,
        )
        lines = output.splitlines()
        if rc != 0 or len(lines) != 1:
            return None
        parts = lines[0].split("\t")
        if len(parts) != 2 or parts[1] != ref or _GIT_OID.fullmatch(parts[0]) is None:
            return None
        return parts[0]

    async def _fresh_existing_pr(self, context: _ExistingPR) -> bool:
        try:
            evidence = await context.verify_fresh_pr(
                context.branch, context.expected_head,
            )
        except Exception:  # noqa: BLE001 - identity checks fail closed
            return False
        return bool(
            isinstance(evidence, dict) and evidence.get("ok") is True
            and evidence.get("branch") == context.branch
            and evidence.get("head_sha") == context.expected_head
        )

    async def repair_existing_pr(
        self, branch: str, expected_head: str, verify_fresh_pr: FreshPRCheck,
        repair_fn: RepairFn, *, title: str, run_id: str = "",
        description: str = "",
        candidate_gate: CandidateGate,
        max_repair_attempts: int = 2,
    ) -> dict:
        """Reproduce a failed exact PR head, then repair on that same branch.

        ``verify_fresh_pr`` is an authenticated caller-owned GET/ledger check.
        It must return ``{"ok": True, "branch": branch, "head_sha": sha}``
        only when immutable creation identity, live PR identity and head match.
        No URL-only provenance grants write authority. This autonomous seam can
        change documentation text only; REVIEW approval must use a separate,
        explicitly authenticated path rather than a caller-provided boolean.
        """
        if (
            not isinstance(branch, str) or _EXISTING_PR_BRANCH.fullmatch(branch) is None
            or not isinstance(expected_head, str)
            or _GIT_OID.fullmatch(expected_head) is None
            or not callable(verify_fresh_pr) or not callable(repair_fn)
            or not callable(candidate_gate)
            or type(max_repair_attempts) is not int
        ):
            return {"ok": False, "stage": "pr_identity", "msg": "invalid repair authority"}
        safe_title = _safe_repair_excerpt(
            str(title), max_bytes=120, secret_values=self._repair_secret_values,
        )
        safe_description = _safe_repair_excerpt(
            str(description), max_bytes=2_000, secret_values=self._repair_secret_values,
        )
        safe_run_id = _safe_repair_excerpt(
            str(run_id), max_bytes=128, secret_values=self._repair_secret_values,
        )
        context = _ExistingPR(branch, expected_head, verify_fresh_pr)
        if not await self._fresh_existing_pr(context):
            return {"ok": False, "stage": "pr_identity", "msg": "PR identity not verified"}
        if await self._remote_head(branch) != expected_head:
            return {"ok": False, "stage": "stale_head", "msg": "remote PR head changed"}
        ref = f"refs/heads/{branch}"
        fetch_rc, _ = await self._run(
            ["git", "fetch", "--no-tags", "origin", ref], self._root,
        )
        if fetch_rc != 0:
            return {"ok": False, "stage": "fetch", "msg": "unable to fetch exact PR head"}
        fetch_rc, fetched = await self._run(["git", "rev-parse", "FETCH_HEAD"], self._root)
        if fetch_rc != 0 or fetched.strip() != expected_head:
            return {"ok": False, "stage": "stale_head", "msg": "fetched PR head changed"}
        # Keep feedback checkouts distinct from ordinary startup sweeping:
        # another live process may own an in-flight repair for this remote PR.
        wt = str(Path(self._root) / ".worktrees" / f"hive-feedback-{uuid.uuid4().hex}")
        add_rc, _ = await self._run(
            ["git", "worktree", "add", "-b", branch, wt, expected_head], self._root,
        )
        if add_rc != 0:
            return {"ok": False, "stage": "worktree", "msg": "unable to create PR repair worktree"}
        state = _CandidateState(branch, wt, expected_head, (), "")
        handed_off = False
        try:
            head_rc, head = await self._run(["git", "rev-parse", "HEAD"], wt)
            ref_rc, local_ref = await self._run(
                ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], wt,
            )
            if head_rc != 0 or head.strip() != expected_head or ref_rc != 0 or local_ref.strip() != branch:
                return {"ok": False, "stage": "stale_head", "msg": "repair checkout differs from PR head"}
            test_wt = str(Path(self._root) / ".worktrees" / f"hive-test-{uuid.uuid4().hex}")
            checkout_rc, _ = await self._run(
                ["git", "worktree", "add", "--detach", test_wt, expected_head], self._root,
            )
            if checkout_rc != 0:
                return {"ok": False, "stage": "test", "msg": "unable to reproduce PR head"}
            try:
                test_rc, test_out = await self._run(self._test_cmd, test_wt)
            finally:
                cleanup_rc, _ = await self._run(
                    ["git", "worktree", "remove", "--force", test_wt], self._root,
                )
            if cleanup_rc != 0:
                return {"ok": False, "stage": "test", "msg": "test checkout cleanup failed"}
            if test_rc == 0:
                return {"ok": False, "stage": "ci_unreproducible", "msg": "PR head tests pass locally"}
            diff_rc, prior_diff = await self._run(
                ["git", "diff", "--no-ext-diff", "--no-textconv", "--no-color",
                 "--text", "--unified=0", f"{expected_head}^", expected_head, "--", "."],
                wt,
            )
            if diff_rc != 0:
                return {"ok": False, "stage": "test", "msg": "unable to collect failing head diff"}
            paths_rc, path_output = await self._run(
                ["git", "diff", "--name-only", "--no-renames",
                 f"{expected_head}^", expected_head, "--", "."], wt,
            )
            # File names are still untrusted PR data. A consumer may use only
            # an exact, separately validated path; malformed/large output
            # cannot select a repair target.
            changed_paths = (
                tuple(path_output.splitlines())
                if paths_rc == 0 and len(path_output) <= 4096 else ()
            )
            context = _ExistingPR(
                branch, expected_head, verify_fresh_pr, source_paths=changed_paths,
            )
            safe_log = _safe_repair_excerpt(
                test_out, max_bytes=_REPAIR_TEST_LOG_MAX_BYTES, tail=True,
                secret_values=self._repair_secret_values,
            )
            safe_diff = _safe_repair_excerpt(
                prior_diff, max_bytes=_REPAIR_STAGED_DIFF_MAX_BYTES,
                secret_values=self._repair_secret_values,
            )
            failure = CandidateFailure(
                attempt=0, test_log=safe_log,
                fingerprint=hashlib.sha256(
                    f"{expected_head}\0{safe_log}".encode("utf-8")
                ).hexdigest(),
                staged_diff=safe_diff, run_id=safe_run_id,
                changed_paths=changed_paths,
            )
            try:
                apply_fn = await repair_fn(failure)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - untrusted repair boundary
                return {"ok": False, "stage": "repair_error", "msg": f"repair failed: {type(exc).__name__}"}
            if not callable(apply_fn):
                return {"ok": False, "stage": "repair_declined", "msg": "repair did not provide an edit"}
            handed_off = True
            return await self.propose(
                safe_title, safe_description, apply_fn, approved_review=False,
                run_id=safe_run_id, repair_fn=repair_fn,
                max_repair_attempts=max_repair_attempts, candidate_gate=candidate_gate,
                _initial_candidate_state=state, _existing_pr=context,
            )
        finally:
            if not handed_off:
                await self._cleanup_candidate(state, dry_run=False, ok=False)

    async def propose(self, title: str, description: str, apply_fn: ApplyFn,
                      *, dry_run: bool = False, approved_review: bool = False,
                      run_id: str | None = None, repair_fn: RepairFn | None = None,
                      max_repair_attempts: int = 0,
                      candidate_gate: CandidateGate | None = None,
                      _initial_candidate_state: _CandidateState | None = None,
                      _existing_pr: _ExistingPR | None = None) -> dict:
        title = redact_known_secrets(str(title))
        description = redact_known_secrets(str(description))
        effective_run_id = redact_known_secrets(
            current_run_id() if run_id is None else str(run_id)
        )
        self._emit(EventType.SELFMOD_START, {
            "title": title, "dry_run": dry_run, "run_id": effective_run_id,
        })
        repair_limit = max(0, min(int(max_repair_attempts), _MAX_REPAIR_ATTEMPTS))
        active_apply = apply_fn
        attempts = 0
        candidate_state: _CandidateState | None = _initial_candidate_state
        while True:
            previous_digest = candidate_state.tested_digest if candidate_state else ""
            try:
                result = await self._propose_inner(
                    title, description, active_apply, dry_run=dry_run,
                    approved_review=approved_review, run_id=effective_run_id,
                    candidate_gate=candidate_gate,
                    candidate_state=candidate_state,
                    retain_on_test_failure=(
                        repair_fn is not None and attempts < repair_limit
                    ),
                    attempt=attempts + 1,
                    existing_pr=_existing_pr,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - no candidate error may disclose data
                result = {
                    "ok": False,
                    "stage": "candidate_error",
                    "msg": f"candidate operation failed: {type(exc).__name__}",
                }
            failure = result.pop("_repair_failure", None)
            next_state = result.pop("_candidate_state", None)
            tested_digest = result.pop("_tested_digest", "")
            result["repair_attempts"] = attempts
            if self._audit is not None:
                try:
                    self._audit({
                        "tool": "self_mod.attempt",
                        "status": "ok" if result.get("ok") else "error",
                        "approved": approved_review,
                        "run_id": effective_run_id,
                        "args": {
                            "attempt": attempts + 1,
                            "stage": result.get("stage"),
                            "candidate_digest": tested_digest,
                        },
                    })
                except Exception as exc:  # noqa: BLE001 - evidence must not change outcome
                    log.warning("self_mod: attempt audit failed: %s", type(exc).__name__)
            if failure is None or repair_fn is None:
                break
            if previous_digest and tested_digest == previous_digest:
                result.update({
                    "stage": "repair_no_progress", "failure_stage": "test",
                    "msg": "candidate repair did not change the staged tree",
                })
                if next_state is not None:
                    await self._cleanup_candidate(next_state, dry_run=dry_run, ok=False)
                break
            if attempts >= repair_limit:
                result.update({
                    "stage": "repair_exhausted", "failure_stage": "test",
                    "msg": "candidate repair attempt limit reached",
                })
                break
            if next_state is None:
                result.update({
                    "stage": "candidate_error", "failure_stage": "test",
                    "msg": "candidate state unavailable for repair",
                })
                break
            try:
                replacement = await repair_fn(failure)
            except asyncio.CancelledError:
                await self._cleanup_candidate(next_state, dry_run=dry_run, ok=False)
                raise
            except Exception as exc:  # noqa: BLE001 - repair must never escape candidate flow
                result.update({
                    "stage": "repair_error", "failure_stage": "test",
                    "msg": f"candidate repair failed: {type(exc).__name__}",
                })
                if next_state is not None:
                    await self._cleanup_candidate(next_state, dry_run=dry_run, ok=False)
                break
            if replacement is None:
                result.update({
                    "stage": "repair_declined", "failure_stage": "test",
                    "msg": "candidate repair declined to produce a replacement",
                })
                if next_state is not None:
                    await self._cleanup_candidate(next_state, dry_run=dry_run, ok=False)
                break
            attempts += 1
            active_apply = replacement
            candidate_state = next_state
        result = redact_value(result)
        result["run_id"] = effective_run_id
        self._emit(EventType.SELFMOD_END, {
            "title": title, "ok": result.get("ok"), "stage": result.get("stage"),
            "branch": result.get("branch"), "dry_run": dry_run,
            "repair_attempts": result.get("repair_attempts", 0),
            "run_id": effective_run_id,
        })
        # Record in history (trim to _MAX_HISTORY).
        record = {"title": title, "dry_run": dry_run, "ts": time.time(),
                  "ok": result.get("ok"), "stage": result.get("stage"),
                  "outcome": result.get("stage"), "branch": result.get("branch"),
                  "pr_url": result.get("pr_url"),
                  "pr_creation": result.get("pr_creation"),
                  "head_sha": result.get("head_sha"),
                  "run_id": effective_run_id,
                  "repair_attempts": result.get("repair_attempts", 0),
                  "tier": "review" if approved_review else "auto"}
        self._history.append(record)
        if len(self._history) > _MAX_HISTORY:
            self._history = self._history[-_MAX_HISTORY:]
        if self._history_store is not None:
            try:
                record["_ledger_id"] = self._history_store.record_selfmod(record)
            except Exception as exc:  # noqa: BLE001 - history must not alter self-mod outcome
                log.warning(
                    "self_mod: durable history write failed: %s",
                    redact_known_secrets(str(exc)),
                )
        if self._audit is not None:
            try:
                self._audit({
                    "tool": "self_mod",
                    "status": "ok" if result.get("ok") else "error",
                    "approved": approved_review,
                    "run_id": effective_run_id,
                    "error": "" if result.get("ok") else str(
                        result.get("msg") or result.get("log") or result.get("stage") or ""
                    ),
                    "args": {
                        "title": title,
                        "stage": result.get("stage"),
                        "branch": result.get("branch"),
                        "pr_url": result.get("pr_url"),
                    },
                })
            except Exception as exc:  # noqa: BLE001 - audit must not alter outcome
                log.warning(
                    "self_mod: audit write failed: %s", redact_known_secrets(str(exc)),
                )
        return result

    async def propose_approved(self, title: str, description: str, apply_fn: ApplyFn,
                               *, dry_run: bool = False,
                               run_id: str | None = None,
                               candidate_gate: CandidateGate | None = None) -> dict:
        """Run a human-approved REVIEW edit through the isolated modifier flow."""
        return await self.propose(
            title, description, apply_fn, dry_run=dry_run, approved_review=True,
            run_id=run_id, candidate_gate=candidate_gate,
        )

    async def _cleanup_candidate(
        self, state: _CandidateState, *, dry_run: bool, ok: bool,
    ) -> None:
        """Preserve the branch if worktree removal fails, so recovery remains possible."""
        try:
            rc, out = await self._run(
                ["git", "worktree", "remove", "--force", state.worktree], self._root,
            )
        except Exception as exc:  # noqa: BLE001 - cleanup must not hide the result
            log.warning("self_mod: worktree cleanup failed (%s); branch preserved", type(exc).__name__)
            return
        if rc != 0:
            log.warning(
                "self_mod: worktree cleanup failed for %s: %s; branch preserved",
                redact_known_secrets(state.worktree), redact_known_secrets(out[:200]),
            )
            return
        if dry_run and ok:
            return
        try:
            rc, out = await self._run(["git", "branch", "-D", state.branch], self._root)
        except Exception as exc:  # noqa: BLE001 - local branch remains recoverable
            log.warning("self_mod: branch cleanup failed (%s)", type(exc).__name__)
            return
        if rc != 0:
            log.warning(
                "self_mod: branch cleanup failed for %s: %s",
                redact_known_secrets(state.branch), redact_known_secrets(out[:200]),
            )

    async def _propose_inner(self, title: str, description: str, apply_fn: ApplyFn,
                             *, dry_run: bool = False, approved_review: bool = False,
                             run_id: str = "",
                             candidate_gate: CandidateGate | None = None,
                             candidate_state: _CandidateState | None = None,
                             retain_on_test_failure: bool = False,
                             attempt: int = 1,
                             existing_pr: _ExistingPR | None = None) -> dict:
        if candidate_state is None:
            run_segment = "".join(c for c in run_id.lower() if c.isalnum())[:8]
            branch_prefix = f"hive/auto-{run_segment}-" if run_segment else "hive/auto-"
            branch = f"{branch_prefix}{uuid.uuid4().hex}"
            wt = str(Path(self._root) / ".worktrees" / branch.replace("/", "-"))
            _, head = await self._run("git rev-parse HEAD", self._root)
            last_good = head.strip()
            rc, out = await self._run(
                ["git", "worktree", "add", "-b", branch, wt], self._root,
            )
            if rc != 0:
                return {"ok": False, "stage": "worktree", "log": out}
        else:
            branch = candidate_state.branch
            wt = candidate_state.worktree
            last_good = candidate_state.last_good
        retain_candidate = False
        success = False
        try:
            if existing_pr is not None:
                head_rc, head = await self._run(["git", "rev-parse", "HEAD"], wt)
                ref_rc, local_ref = await self._run(
                    ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], wt,
                )
                if (
                    branch != existing_pr.branch or last_good != existing_pr.expected_head
                    or head_rc != 0 or head.strip() != last_good
                    or ref_rc != 0 or local_ref.strip() != branch
                ):
                    return {"ok": False, "stage": "stale_head", "msg": "repair branch moved"}
            reported_changed = await apply_fn(wt)
            if candidate_state is not None and isinstance(reported_changed, list):
                reported_changed = list(dict.fromkeys([
                    *candidate_state.reported_changed, *reported_changed,
                ]))
            if isinstance(reported_changed, list) and _touches_protected(reported_changed):
                log.warning(
                    "self_mod BLOCKED: proposed edit touches protected files: %s",
                    redact_value([p for p in reported_changed if _touches_protected([p])]),
                )
                return {"ok": False, "stage": "protected",
                        "msg": "change touches SOUL.md or approval gate — human-only"}

            verified = await _verify_candidate_changes(
                self._run, wt, reported_changed, approved_review=approved_review,
            )
            if verified.get("ok") is False:
                return verified
            changed = verified["changed"]
            if existing_pr is not None and any(
                _existing_pr_source_path(path) for path in changed
            ):
                return {
                    "ok": False, "stage": "review_required",
                    "msg": "existing PR edit requires REVIEW approval",
                }
            if not changed:
                return {
                    "ok": False,
                    "stage": "no_changes",
                    "msg": "apply_fn produced no file changes",
                }
            # Stage before testing. From here onward the index is the candidate
            # artifact and every gate is bound to its exact Git tree.
            stage_rc, stage_out = await self._run("git add -A", wt)
            if stage_rc != 0:
                return {
                    "ok": False, "stage": "stage", "last_good": last_good,
                    "log": stage_out[-1000:],
                }
            tree_rc, tree_out = await self._run(["git", "write-tree"], wt)
            staged_tree = tree_out.strip()
            if (
                tree_rc != 0
                or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", staged_tree) is None
            ):
                return {
                    "ok": False, "stage": "stage",
                    "msg": "unable to identify staged candidate tree",
                }
            try:
                scan_rc, staged_diff = await self._run(
                    [
                        "git", "diff", "--cached", "--no-ext-diff",
                        "--no-textconv", "--no-color", "--text", "--unified=0",
                        "--", ".",
                    ],
                    wt,
                )
                if scan_rc != 0:
                    raise RuntimeError("staged diff unavailable")
                path_rc, candidate_paths, _ = await _candidate_present_files(
                    self._run, wt,
                )
                if path_rc != 0:
                    raise RuntimeError("staged candidate paths unavailable")
                scanner_findings = self._secret_scanner(staged_diff)
                if not isinstance(scanner_findings, list) or any(
                    not isinstance(item, SecretFinding) for item in scanner_findings
                ):
                    raise TypeError("invalid scanner result")
                findings = [
                    *scan_candidate_paths(candidate_paths), *scanner_findings,
                ]
            except Exception:  # noqa: BLE001 - scanner boundary fails closed
                return {
                    "ok": False,
                    "stage": "secret_scan_error",
                    "last_good": last_good,
                    "msg": "unable to safely scan staged candidate changes",
                }
            if findings:
                safe_findings = [
                    {"rule": item.rule, "path": item.path, "line": item.line}
                    for item in findings
                ]
                log.warning(
                    "self_mod BLOCKED: candidate secret scan found %d potential secret(s)",
                    len(safe_findings),
                )
                return {
                    "ok": False,
                    "stage": "secret_scan",
                    "last_good": last_good,
                    "findings": safe_findings,
                    "msg": "candidate secret scan found potential credentials",
                }
            try:
                tested_digest = await candidate_artifact_digest(self._run, wt, changed)
            except (OSError, ValueError) as exc:
                return {"ok": False, "stage": "changed_files", "msg": str(exc)}

            commit_tree_rc, commit_tree_out = await self._run(
                [
                    "git", "-c", "user.name=Hive Evaluator",
                    "-c", "user.email=hive-evaluator@localhost",
                    "commit-tree", staged_tree, "-p", last_good,
                    "-m", "materialize candidate for evaluation",
                ],
                wt,
            )
            evaluation_commit = commit_tree_out.strip()
            if (
                commit_tree_rc != 0
                or re.fullmatch(
                    r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", evaluation_commit,
                ) is None
            ):
                return {
                    "ok": False,
                    "stage": "stage",
                    "msg": "unable to materialize staged candidate tree",
                }

            test_wt = str(
                Path(self._root) / ".worktrees" / f"hive-test-{uuid.uuid4().hex}"
            )
            checkout_rc, checkout_out = await self._run(
                ["git", "worktree", "add", "--detach", test_wt, evaluation_commit],
                self._root,
            )
            if checkout_rc != 0:
                return {
                    "ok": False,
                    "stage": "test",
                    "msg": "unable to create immutable candidate test checkout",
                    "log": checkout_out[-1000:],
                }
            try:
                rc, test_out = await self._run(self._test_cmd, test_wt)
            finally:
                cleanup_rc, cleanup_out = await self._run(
                    ["git", "worktree", "remove", "--force", test_wt], self._root,
                )
            if cleanup_rc != 0:
                return {
                    "ok": False,
                    "stage": "test",
                    "msg": "unable to remove immutable candidate test checkout",
                    "log": cleanup_out[-1000:],
                }
            if rc != 0:
                safe_log = _safe_repair_excerpt(
                    test_out, max_bytes=_REPAIR_TEST_LOG_MAX_BYTES, tail=True,
                    secret_values=self._repair_secret_values,
                )
                safe_diff = _safe_repair_excerpt(
                    staged_diff, max_bytes=_REPAIR_STAGED_DIFF_MAX_BYTES,
                    secret_values=self._repair_secret_values,
                )
                failure = CandidateFailure(
                    attempt=attempt,
                    test_log=safe_log,
                    fingerprint=hashlib.sha256(
                        f"{tested_digest}\0{safe_log}".encode("utf-8")
                    ).hexdigest(),
                    staged_diff=safe_diff,
                    run_id=run_id[:128],
                    changed_paths=existing_pr.source_paths if existing_pr else (),
                )
                retain_candidate = retain_on_test_failure
                next_state = _CandidateState(
                    branch=branch, worktree=wt, last_good=last_good,
                    reported_changed=tuple(reported_changed),
                    tested_digest=tested_digest,
                ) if retain_candidate else None
                return {
                    "ok": False, "stage": "test", "last_good": last_good,
                    "log": safe_log, "recorded": True,
                    "_repair_failure": failure,
                    "_candidate_state": next_state,
                    "_tested_digest": tested_digest,
                }

            # Tests/callbacks must not add or alter paths after the initial check
            # and before `git add -A` below.
            verified = await _verify_candidate_changes(
                self._run, wt, reported_changed, approved_review=approved_review,
            )
            if verified.get("ok") is False:
                return verified
            changed = verified["changed"]
            unstaged_rc, _ = await self._run(
                ["git", "diff", "--quiet", "--no-ext-diff", "--", "."], wt,
            )
            if unstaged_rc != 0:
                return {
                    "ok": False,
                    "stage": "changed_files",
                    "msg": "candidate working tree changed after staging",
                }
            try:
                post_test_digest = await candidate_artifact_digest(self._run, wt, changed)
            except (OSError, ValueError) as exc:
                return {"ok": False, "stage": "changed_files", "msg": str(exc)}
            if post_test_digest != tested_digest:
                return {
                    "ok": False,
                    "stage": "changed_files",
                    "msg": "candidate content changed during tests",
                }

            evaluation: dict[str, Any] = {}
            if candidate_gate is not None:
                gate_wt = str(
                    Path(self._root) / ".worktrees" / f"hive-eval-{uuid.uuid4().hex}"
                )
                checkout_rc, checkout_out = await self._run(
                    ["git", "worktree", "add", "--detach", gate_wt, evaluation_commit],
                    self._root,
                )
                if checkout_rc != 0:
                    return {
                        "ok": False,
                        "stage": "evaluation",
                        "msg": "unable to create immutable candidate evaluation checkout",
                        "log": checkout_out[-1000:],
                    }
                try:
                    try:
                        evaluation = await candidate_gate(
                            gate_wt, last_good, changed, run_id, post_test_digest,
                        )
                    finally:
                        cleanup_rc, cleanup_out = await self._run(
                            ["git", "worktree", "remove", "--force", gate_wt], self._root,
                        )
                except Exception as exc:  # noqa: BLE001 - quality gate must fail closed
                    return {
                        "ok": False,
                        "stage": "evaluation",
                        "last_good": last_good,
                        "msg": f"candidate evaluation raised: {type(exc).__name__}: {exc}",
                    }
                if cleanup_rc != 0:
                    return {
                        "ok": False,
                        "stage": "evaluation",
                        "msg": "unable to remove immutable candidate evaluation checkout",
                        "log": cleanup_out[-1000:],
                    }
                if not evaluation.get("ok"):
                    return {
                        "ok": False,
                        "stage": "evaluation",
                        "last_good": last_good,
                        "msg": str(evaluation.get("reason") or "candidate evaluation rejected"),
                        "evaluation": evaluation,
                    }

                # Candidate execution inside the quality gate must not alter,
                # remove, or add anything after the preceding policy check.
                verified = await _verify_candidate_changes(
                    self._run, wt, reported_changed,
                    approved_review=approved_review,
                )
                if verified.get("ok") is False:
                    return verified
                changed = verified["changed"]
                unstaged_rc, _ = await self._run(
                    ["git", "diff", "--quiet", "--no-ext-diff", "--", "."], wt,
                )
                if unstaged_rc != 0:
                    return {
                        "ok": False,
                        "stage": "evaluation",
                        "msg": "candidate working tree changed during evaluation",
                    }
                try:
                    post_gate_digest = await candidate_artifact_digest(self._run, wt, changed)
                except (OSError, ValueError) as exc:
                    return {"ok": False, "stage": "changed_files", "msg": str(exc)}
                if (
                    post_gate_digest != post_test_digest
                    or evaluation.get("candidate_digest") != post_test_digest
                ):
                    return {
                        "ok": False,
                        "stage": "evaluation",
                        "msg": "candidate artifact changed during evaluation",
                    }

            if dry_run:
                success = True
                return {"ok": True, "stage": "dry_run", "branch": branch,
                        "last_good": last_good, "changed": changed,
                        "evaluation": evaluation}

            try:
                staged_digest = await candidate_artifact_digest(self._run, wt, changed)
            except (OSError, ValueError) as exc:
                return {"ok": False, "stage": "stage", "msg": str(exc)}
            if staged_digest != post_test_digest:
                return {
                    "ok": False, "stage": "stage",
                    "msg": "staged candidate differs from evaluated artifact",
                }
            # Abort early if apply_fn made no actual changes (avoids empty-commit error).
            _, status_out = await self._run("git status --porcelain", wt)
            if not status_out.strip():
                return {"ok": False, "stage": "no_changes",
                        "msg": "apply_fn produced no file changes"}
            # Use list form (exec, not shell) so LLM-sourced title cannot inject shell.
            title = title.replace("\n", " ").replace("\r", " ")[:120]
            commit_rc, commit_out = await self._run(["git", "commit", "-m", title], wt)
            if commit_rc != 0:
                return {
                    "ok": False, "stage": "commit", "last_good": last_good,
                    "log": commit_out[-1000:],
                }
            tree_rc, tree_out = await self._run(["git", "rev-parse", "HEAD^{tree}"], wt)
            if tree_rc != 0 or tree_out.strip() != staged_tree:
                return {
                    "ok": False, "stage": "commit",
                    "msg": "committed tree differs from evaluated staged tree",
                }
            head_rc, head_out = await self._run(["git", "rev-parse", "HEAD"], wt)
            head_sha = head_out.strip().lower()
            if (
                head_rc != 0
                or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head_sha) is None
            ):
                if existing_pr is not None:
                    return {"ok": False, "stage": "commit", "msg": "unable to identify repair commit"}
                # PR feedback automation cannot claim this candidate without
                # a precise locally pushed commit. Preserve the existing PR
                # creation path, but leave future writes fail-closed.
                head_sha = ""
            if existing_pr is not None:
                parent_rc, parent = await self._run(["git", "rev-parse", "HEAD^"], wt)
                ref_rc, local_ref = await self._run(
                    ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], wt,
                )
                if (
                    parent_rc != 0 or parent.strip() != existing_pr.expected_head
                    or ref_rc != 0 or local_ref.strip() != branch
                ):
                    return {"ok": False, "stage": "stale_head", "msg": "repair commit parent or branch changed"}
                if not await self._fresh_existing_pr(existing_pr):
                    return {"ok": False, "stage": "pr_identity", "msg": "PR identity changed before push"}
                if await self._remote_head(branch) != existing_pr.expected_head:
                    return {"ok": False, "stage": "stale_head", "msg": "remote PR head changed before push"}
                push_rc, _ = await self._run(
                    ["git", "push", "--porcelain", "origin", f"HEAD:refs/heads/{branch}"], wt,
                )
                if push_rc != 0:
                    return {"ok": False, "stage": "push_uncertain", "msg": "repair push not confirmed"}
                if await self._remote_head(branch) != head_sha:
                    return {"ok": False, "stage": "push_uncertain", "msg": "pushed repair head not confirmed"}
                success = True
                return {
                    "ok": True, "stage": "pushed", "branch": branch,
                    "last_good": last_good, "head_sha": head_sha,
                    "evaluation": evaluation,
                    "note": "existing PR branch updated; human review remains required",
                }
            rc, push_out = await self._run(f"git push -u origin {branch}", wt)
            if rc != 0:
                # Push failed (auth/network) — surface it instead of falsely reporting ok.
                return {"ok": False, "stage": "push", "branch": branch,
                        "last_good": last_good, "log": push_out[-500:]}

            result = {"ok": True, "stage": "pushed", "branch": branch,
                      "last_good": last_good, "head_sha": head_sha,
                      "push": push_out[-500:],
                      "evaluation": evaluation}
            # #si-3: open a DRAFT PR via the GitHub REST API; never merge (human merges).
            if self._open_pr is not None:
                safe_evaluation = redact_value(evaluation)
                pr_body = (
                    f"## Summary\n\n{description or title}\n\n"
                    f"## Changed files\n\n"
                    + "".join(f"- `{f}`\n" for f in changed)
                    + "\n## Safety\n\n"
                    "- Proposed by Hive's self-improvement loop\n"
                    "- Tests passed in isolated git worktree before this PR was opened\n"
                    "- **Hive never merges — a human reviews and merges**\n"
                    f"\nRun ID: `{run_id or 'unattributed'}`"
                    f"\nBranch: `{branch}` | Base commit: `{last_good[:8]}`"
                    f"\nEvaluation: `{safe_evaluation or 'not configured'}`"
                )
                try:
                    opened = await self._open_pr(
                        branch, title, redact_known_secrets(pr_body),
                    )
                except Exception as exc:  # noqa: BLE001 - PR transport is best-effort
                    log.warning(
                        "self_mod: PR opener failed: %s",
                        redact_known_secrets(type(exc).__name__),
                    )
                    opened = None
                receipt = opened if isinstance(opened, PRCreationReceipt) else None
                pr_url = receipt.url if receipt is not None else opened
                if receipt is not None and receipt.matches_candidate(branch, head_sha):
                    result["pr_creation"] = receipt.as_dict()
                result["pr_url"] = pr_url
                result["note"] = ("draft PR opened by Hive; a human merges"
                                  if pr_url else "branch pushed; PR open failed (see logs)")
            else:
                result["note"] = "branch pushed; open a PR to review (Hive never merges)"
            success = True
            return result
        finally:
            if not retain_candidate:
                await self._cleanup_candidate(
                    _CandidateState(branch, wt, last_good, (), ""),
                    dry_run=dry_run, ok=success,
                )
