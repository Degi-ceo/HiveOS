"""
learned_skills.py — Hive's auto-learned skill registry (PILLAR 3, sprint7).

PILLAR 3 closes the loop that builtins/curator left open: tools a Hive agent
creates by detecting a repeated sequence in its own audit log. The flow is
deliberately constrained so an auto-learned tool can NEVER widen Hive's
authority on its own:

  1. ``detect_patterns(audit_entries)`` — scans the recent audit log for tool-call
     sequences that repeat at least ``min_repeats`` times within a sliding
     window. Returns the most-repeated concrete sequences (tool-name tuples,
     not args) so we generalise across inputs.
  2. ``propose_skill(pattern)`` — turns a detected sequence into a ``SkillTemplate``
     (name, description, parameter schema, generated python body, status
     ``proposed``). The body runs each tool in order via the existing
     ``ToolRegistry``; nothing it generates can call a tool that isn't already
     registered, and dangerous tools require an explicit approval flag.
  3. ``add_learned_skill(template, *, approve=False)`` — persists the template
     and, when approved, instantiates a ``LearnedSkill`` BaseTool subclass,
     registers it with ``ToolRegistry``, and tracks usage via the existing
     ``SkillUsageStore`` (``agent_created=True``). Without approval the template
     sits in ``proposed`` state for human review.

Persistence is SQLite (single file, two tables — ``learned_skills`` and
``tool_sequences``) so the learned state survives restarts. DAG: this module
imports from core + memory + tools only, no LLM, no gateway.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from hive.core.types import ToolResult
from hive.tools.base import BaseTool, ToolSpec

log = logging.getLogger("hive.tools.learned_skills")

# Lifecycle: proposed -> approved -> registered -> archived (no deletes, ever).
# `smoke_failed` is a sibling of `proposed` — the body failed the pre-flight
# smoke test; an operator can override (force=True) to flip it back to proposed.
STATUS_PROPOSED = "proposed"
STATUS_APPROVED = "approved"
STATUS_REGISTERED = "registered"
STATUS_REJECTED = "rejected"
STATUS_ARCHIVED = "archived"
STATUS_SMOKE_FAILED = "smoke_failed"

ALL_STATUSES = (STATUS_PROPOSED, STATUS_APPROVED, STATUS_REGISTERED, STATUS_REJECTED,
                STATUS_ARCHIVED, STATUS_SMOKE_FAILED)

# Pre-flight smoke-test outcomes (Batch B). The enum keeps the four valid values
# in one place — the SkillTemplate dataclass, run_smoke_test, and the gateway
# all reference these constants instead of hard-coded strings.
SMOKE_NONE = "none"           # not yet run
SMOKE_PASS = "pass"           # body executed without raising and returned truthy
SMOKE_FAIL = "fail"           # body ran but returned falsy OR failed a guard
SMOKE_ERROR = "error"         # compile or runtime exception

ALL_SMOKE_RESULTS = (SMOKE_NONE, SMOKE_PASS, SMOKE_FAIL, SMOKE_ERROR)

# Cap the smoke log so a noisy traceback can't blow up the SQLite row.
_SMOKE_LOG_MAX = 500


@dataclass(slots=True)
class SkillTemplate:
    """A Hive-generated skill template. The body is python source that runs inside
    the ``LearnedSkill._run`` method (which receives the live tool registry)."""
    id: str
    name: str
    description: str
    pattern: tuple[str, ...]            # the tool sequence that triggered this
    params: dict[str, Any]             # JSON Schema for the skill's inputs
    code: str                          # python source (body of _run)
    status: str = STATUS_PROPOSED
    created_ts: float = 0.0
    approved_ts: float | None = None
    use_count: int = 0
    last_used_ts: float | None = None
    category: str = "learned"
    dangerous: bool = False
    notes: str = ""
    smoke_result: str = SMOKE_NONE     # Batch B: pre-flight verdict
    smoke_log: str = ""                # Batch B: truncated traceback / reason

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "pattern": list(self.pattern),
            "params": self.params,
            "code": self.code,
            "status": self.status,
            "created_ts": self.created_ts,
            "approved_ts": self.approved_ts,
            "use_count": self.use_count,
            "last_used_ts": self.last_used_ts,
            "category": self.category,
            "dangerous": self.dangerous,
            "notes": self.notes,
            "smoke_result": self.smoke_result,
            "smoke_log": self.smoke_log,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SkillTemplate":
        return cls(
            id=d["id"],
            name=d["name"],
            description=d["description"],
            pattern=tuple(d.get("pattern", ())),
            params=d.get("params", {}) or {},
            code=d.get("code", ""),
            status=d.get("status", STATUS_PROPOSED),
            created_ts=float(d.get("created_ts", 0.0)),
            approved_ts=d.get("approved_ts"),
            use_count=int(d.get("use_count", 0)),
            last_used_ts=d.get("last_used_ts"),
            category=d.get("category", "learned"),
            dangerous=bool(d.get("dangerous", False)),
            notes=d.get("notes", ""),
            smoke_result=d.get("smoke_result", SMOKE_NONE),
            smoke_log=d.get("smoke_log", ""),
        )


# ---------- pattern detection -------------------------------------------------

def _sliding_sequences(tools: Sequence[str], seq_len: int) -> Iterable[tuple[str, ...]]:
    """Yield every contiguous ``seq_len``-tuple from a tool-call sequence."""
    if len(tools) < seq_len:
        return
    for i in range(len(tools) - seq_len + 1):
        yield tuple(tools[i:i + seq_len])


def detect_patterns(
    audit_entries: Iterable[dict],
    *,
    min_repeats: int = 2,
    min_seq_len: int = 3,
    max_seq_len: int = 5,
    limit: int = 20,
) -> list[tuple[tuple[str, ...], int]]:
    """Find tool-name sequences that repeat at least ``min_repeats`` times.

    Args:
        audit_entries: iterable of audit-log dicts (each must have a ``tool`` key).
        min_repeats: minimum occurrences to count as a pattern.
        min_seq_len: smallest sequence length to consider.
        max_seq_len: largest sequence length to consider.
        limit: cap on the number of patterns returned.

    Returns:
        A list of ``(sequence, count)`` tuples sorted by count (descending).
        Sequences are tuples of tool names; their order matters.
    """
    # Only "ok" audit rows count — a pattern of failures shouldn't be promoted.
    tools = [str(e["tool"]) for e in audit_entries
             if e.get("status") == "ok" and e.get("tool")]
    counts: dict[tuple[str, ...], int] = {}
    for sl in range(min_seq_len, max_seq_len + 1):
        for seq in _sliding_sequences(tools, sl):
            counts[seq] = counts.get(seq, 0) + 1
    # Drop under-represented sequences; sort by count then by length desc.
    out = [(seq, c) for seq, c in counts.items() if c >= min_repeats]
    out.sort(key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    return out[:limit]


# ---------- template generation -----------------------------------------------

# Reserved words in candidate skill names — refuse to shadow builtins or core APIs.
_NAME_RESERVED = {
    "shell", "web_get", "read_file", "write_file", "delete_file",
    "spend_money", "deploy", "external_message", "delegate_to_specialist",
    "discover", "hive_status", "create_task", "query_memory",
    "obsidian_read", "obsidian_search", "obsidian_list",
    "github_list_prs", "github_get_pr", "github_list_commits", "github_create_issue",
}

# Monotonic counter so back-to-back proposals within the same millisecond still
# get unique names. Combined with the wall-clock suffix below it stays unique
# across processes (the timestamp) and within a process (the counter).
_PROPOSAL_COUNTER = 0


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
    return s or "learned_skill"


def propose_skill(
    pattern: Sequence[str],
    *,
    description: str = "",
    extra_params: dict[str, Any] | None = None,
    seq_id: str | None = None,
    registry: Any | None = None,
    force: bool = False,
) -> SkillTemplate:
    """Build a ``SkillTemplate`` from a detected tool sequence.

    The generated body is a small python function that calls each tool in order
    via the runtime-provided ``call_tool(name, args)`` helper. Inputs to the
    skill are auto-inferred from the union of tool argument keys; extra params
    can be appended via ``extra_params``.

    Batch B: when ``registry`` is provided, the body is run through a smoke
    test BEFORE the status is flipped. Templates that pass the smoke check
    keep ``status=proposed`` (the normal happy path). Templates that fail the
    smoke check are stored with ``status=smoke_failed`` so a human can review
    the failure reason; pass ``force=True`` to override and propose anyway.
    When ``registry`` is ``None`` (e.g. tests that don't care), the smoke
    test is skipped and the status is ``proposed`` — backward compatible.
    """
    pat = tuple(pattern)
    if not pat:
        raise ValueError("pattern must contain at least one tool name")
    base = "_".join(pat)
    slug = _slugify(base)
    global _PROPOSAL_COUNTER
    _PROPOSAL_COUNTER += 1
    suffix = (seq_id or f"{int(time.time() * 1000):x}{_PROPOSAL_COUNTER:x}")[-10:]
    name = f"learned_{slug}_{suffix}"
    while name in _NAME_RESERVED:
        _PROPOSAL_COUNTER += 1
        suffix = (f"{int(time.time() * 1000):x}{_PROPOSAL_COUNTER:x}")[-10:]
        name = f"learned_{slug}_{suffix}"
    if not description:
        description = (
            f"Auto-learned composite skill that calls: {', '.join(pat)} "
            f"in order (pattern observed in audit log)."
        )
    # Minimal JSON Schema: aggregate distinct argument keys from the pattern.
    param_props: dict[str, Any] = {}
    for tool_name in pat:
        param_props[tool_name] = {
            "type": "object",
            "description": f"Arguments forwarded to {tool_name}.",
        }
    if extra_params:
        param_props.update(extra_params)
    params = {
        "type": "object",
        "properties": param_props,
        "required": list(param_props.keys()),
    }
    code = _generate_body(pat, name)
    template = SkillTemplate(
        id=name,                       # id == name keeps the lookup cheap
        name=name,
        description=description,
        pattern=pat,
        params=params,
        code=code,
        status=STATUS_PROPOSED,
        created_ts=time.time(),
        category="learned",
        notes=f"derived from pattern {list(pat)}",
    )

    # Batch B: pre-flight smoke. If a registry is supplied we run the body
    # against a fixture before flipping the status to ``proposed``. A body
    # that fails the smoke gets ``smoke_failed`` so a human can inspect.
    # ``force=True`` bypasses the gate (operator override) and keeps the
    # normal ``proposed`` status even on a smoke failure.
    if registry is not None:
        template.dangerous = _pattern_is_dangerous(pat, registry)
        run_smoke_test(template, registry)
        if template.smoke_result != SMOKE_PASS and not force:
            template.status = STATUS_SMOKE_FAILED

    return template


def _generate_body(pattern: Sequence[str], skill_name: str) -> str:
    """Emit a python module that defines ``async def run(call_tool, args)``.

    ``exec(code, scope)`` produces a module; we then look up ``run`` and call
    it with the bound ``call_tool`` helper and the user-provided args dict.
    Each tool gets its own sub-dict from args (keyed by tool name).
    """
    lines: list[str] = [
        "# Auto-generated for learned skill: " + skill_name,
        "# Sequence: " + " -> ".join(pattern),
        "async def run(call_tool, args):",
        "    results = []",
    ]
    for tool_name in pattern:
        lines.append(
            f"    results.append(await call_tool({tool_name!r}, "
            f"args.get({tool_name!r}, {{}})))"
        )
    lines.append("    return results")
    return "\n".join(lines) + "\n"


# ---------- pre-flight smoke test (Batch B) ---------------------------------

def _extract_call_tool_names(code: str) -> list[str]:
    """Best-effort scan for ``call_tool("name", ...)`` invocations in body.

    Used by the DAG safety check — bodies that reference tools absent from the
    registry fail smoke rather than being proposed for approval. We look at the
    raw string rather than AST to avoid depending on import structure (the body
    is auto-generated python; AST would also work but adds parsing overhead and
    brittle handling of edge cases like f-strings or comments).
    """
    out: list[str] = []
    for m in re.finditer(r"call_tool\(\s*['\"]([A-Za-z0-9_]+)['\"]\s*,", code):
        out.append(m.group(1))
    return out


def _build_isolated_namespace(registry: Any) -> dict[str, Any]:
    """Build a fresh ``__main__``-style namespace for smoke execution.

    Bodies get a real ``__builtins__`` (so ``len``, ``dict``, etc. work), an
    isolated empty ``args`` dict, and a ``call_tool`` fake. The fake is an
    async function that validates the name is in the registry and returns a
    ToolResult carrying ``"ok"``. We deliberately do NOT include any of the
    caller's globals — mutations to this namespace cannot leak.
    """
    # Snapshot the registry's known tool names once. The registry may be a
    # ToolRegistry class (uses classmethods) or a duck-typed object; both
    # expose ``snapshot() -> dict``.
    if hasattr(registry, "snapshot"):
        tool_names = set((registry.snapshot() or {}).keys())
    elif isinstance(registry, dict):
        tool_names = set(registry.keys())
    else:
        tool_names = set()

    async def call_tool(name: str, args: dict) -> ToolResult:
        if name not in tool_names:
            raise NameError(f"unknown tool: {name!r}")
        return ToolResult(tool_name=name, success=True, content="ok")

    import builtins as _bi
    return {
        "__builtins__": _bi.__dict__,
        "args": {},
        "call_tool": call_tool,
    }


def run_smoke_test(template: SkillTemplate, registry: Any) -> SkillTemplate:
    """Validate a candidate's declarative tool sequence without running code.

    ``SkillTemplate.code`` is retained only for migration/display compatibility.
    Learned skills execute their persisted ``pattern`` below, never arbitrary
    Python.  A smoke test must therefore prove that each declared tool exists,
    not compile or invoke a generated body with ambient Python authority.
    """
    if hasattr(registry, "snapshot"):
        known = set((registry.snapshot() or {}).keys())
    elif isinstance(registry, dict):
        known = set(registry.keys())
    else:
        known = set()
    unknown = [n for n in template.pattern if n not in known]
    if unknown:
        template.smoke_result = SMOKE_FAIL
        template.smoke_log = (
            f"DAG violation: tool(s) not in registry: {unknown}"
        )[:_SMOKE_LOG_MAX]
        return template

    if not template.pattern:
        template.smoke_result = SMOKE_FAIL
        template.smoke_log = "skill pattern is empty"
        return template
    template.smoke_result = SMOKE_PASS
    template.smoke_log = ""
    return template


# ---------- persistence --------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS learned_skills(
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  description TEXT NOT NULL,
  pattern     TEXT NOT NULL,
  params      TEXT NOT NULL,
  code        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'proposed',
  created_ts  REAL NOT NULL,
  approved_ts REAL,
  use_count   INTEGER NOT NULL DEFAULT 0,
  last_used_ts REAL,
  category    TEXT NOT NULL DEFAULT 'learned',
  dangerous   INTEGER NOT NULL DEFAULT 0,
  notes       TEXT NOT NULL DEFAULT '',
  smoke_result TEXT NOT NULL DEFAULT 'none',
  smoke_log   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tool_sequences(
  seq_key   TEXT PRIMARY KEY,
  pattern   TEXT NOT NULL,
  count     INTEGER NOT NULL,
  last_seen REAL NOT NULL
);
"""

# Schema migrations for pre-Batch-B databases. Each entry is ``(table,
# column, default_clause)`` — applied via ``ALTER TABLE ADD COLUMN`` when the
# column is absent. Adding a column is a one-line change; dropping one would
# require a separate migration and is not currently needed.
#
# Idempotency: ``PRAGMA table_info`` is consulted before every ALTER. The ALTER
# itself is also idempotent at the SQL level (re-adding a column would fail,
# but we never reach that branch when the column exists). Migrations are
# single-process (Hive's SQLite is local-only); no row-level locking is
# required because we hold the connection for the lifetime of the store.
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("learned_skills", "smoke_result", "TEXT NOT NULL DEFAULT 'none'"),
    ("learned_skills", "smoke_log",   "TEXT NOT NULL DEFAULT ''"),
)


class LearnedSkillStore:
    """SQLite persistence for ``SkillTemplate`` rows + observed sequences."""

    def __init__(self, db_path: str | Path, *,
                 clock: Callable[[], float] = time.time) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._clock = clock
        self._db.executescript(_SCHEMA)
        self._migrate()
        self._db.commit()

    def _migrate(self) -> None:
        """Apply pending schema migrations idempotently.

        Reads ``PRAGMA table_info`` for each known table and adds any column
        declared in ``_MIGRATIONS`` that's missing. SQLite's ``ALTER TABLE
        ADD COLUMN`` accepts a ``DEFAULT`` clause so existing rows get a
        sensible value, and the NOT NULL constraint is satisfied because the
        default backfills. Running this twice is a no-op: the second pass
        sees every column already present and skips the ALTER.
        """
        for table, column, default_clause in _MIGRATIONS:
            try:
                rows = self._db.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            except sqlite3.OperationalError:
                # Table doesn't exist yet — _SCHEMA's CREATE TABLE IF NOT
                # EXISTS will create it on the next open, so this migration
                # pass is a no-op for this table. Skip silently.
                continue
            existing = {row["name"] for row in rows}
            if column in existing:
                continue
            self._db.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {default_clause}"
            )

    # ---- templates -----------------------------------------------------------

    def save(self, template: SkillTemplate) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO learned_skills"
            "(id, name, description, pattern, params, code, status, created_ts,"
            " approved_ts, use_count, last_used_ts, category, dangerous, notes,"
            " smoke_result, smoke_log)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (template.id, template.name, template.description,
             json.dumps(list(template.pattern)),
             json.dumps(template.params), template.code, template.status,
             template.created_ts, template.approved_ts, template.use_count,
             template.last_used_ts, template.category, int(template.dangerous),
             template.notes, template.smoke_result, template.smoke_log),
        )
        self._db.commit()

    def get(self, template_id: str) -> SkillTemplate | None:
        row = self._db.execute(
            "SELECT * FROM learned_skills WHERE id=?", (template_id,)
        ).fetchone()
        return _row_to_template(row) if row else None

    def list_by_status(self, status: str | None = None) -> list[SkillTemplate]:
        if status is not None:
            rows = self._db.execute(
                "SELECT * FROM learned_skills WHERE status=? ORDER BY created_ts DESC",
                (status,),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM learned_skills ORDER BY created_ts DESC"
            ).fetchall()
        return [_row_to_template(r) for r in rows]

    def all(self) -> list[SkillTemplate]:
        return self.list_by_status()

    def update_status(self, template_id: str, status: str,
                     *, approved_ts: float | None = None) -> bool:
        if approved_ts is None and status == STATUS_APPROVED:
            approved_ts = self._clock()
        cur = self._db.execute(
            "UPDATE learned_skills SET status=?, approved_ts=COALESCE(?, approved_ts)"
            " WHERE id=?",
            (status, approved_ts, template_id),
        )
        self._db.commit()
        return cur.rowcount > 0

    def record_use(self, template_id: str) -> None:
        self._db.execute(
            "UPDATE learned_skills SET use_count=use_count+1, last_used_ts=? WHERE id=?",
            (self._clock(), template_id),
        )
        self._db.commit()

    def stats(self) -> dict:
        total = self._db.execute(
            "SELECT COUNT(*) AS n FROM learned_skills"
        ).fetchone()["n"]
        by_status: dict[str, int] = {}
        for r in self._db.execute(
            "SELECT status, COUNT(*) AS n FROM learned_skills GROUP BY status"
        ).fetchall():
            by_status[r["status"]] = r["n"]
        return {"total": total, "by_status": by_status}

    # ---- sequence observation ----------------------------------------------

    def observe_sequence(self, pattern: Sequence[str]) -> int:
        """Record a concrete observed sequence; returns its updated count."""
        seq_key = "->".join(pattern)
        now = self._clock()
        row = self._db.execute(
            "SELECT count FROM tool_sequences WHERE seq_key=?",
            (seq_key,),
        ).fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO tool_sequences(seq_key, pattern, count, last_seen)"
                " VALUES(?,?,?,?)",
                (seq_key, json.dumps(list(pattern)), 1, now),
            )
        else:
            self._db.execute(
                "UPDATE tool_sequences SET count=count+1, last_seen=? WHERE seq_key=?",
                (now, seq_key),
            )
        self._db.commit()
        cur = self._db.execute(
            "SELECT count FROM tool_sequences WHERE seq_key=?", (seq_key,)
        ).fetchone()
        return int(cur["count"]) if cur else 0

    def observed_sequences(self, min_count: int = 2) -> list[tuple[tuple[str, ...], int]]:
        rows = self._db.execute(
            "SELECT pattern, count FROM tool_sequences WHERE count>=? "
            "ORDER BY count DESC",
            (min_count,),
        ).fetchall()
        out: list[tuple[tuple[str, ...], int]] = []
        for r in rows:
            try:
                pat = tuple(json.loads(r["pattern"]))
            except (json.JSONDecodeError, TypeError):
                continue
            out.append((pat, int(r["count"])))
        return out

    def close(self) -> None:
        self._db.close()


def _row_to_template(row: sqlite3.Row) -> SkillTemplate:
    try:
        pattern = tuple(json.loads(row["pattern"]))
    except (json.JSONDecodeError, TypeError):
        pattern = ()
    try:
        params = json.loads(row["params"])
    except (json.JSONDecodeError, TypeError):
        params = {}
    # Older DBs (pre-Batch-B) won't have smoke_result / smoke_log columns;
    # sqlite3.Row raises IndexError when the key is missing.
    try:
        smoke_result = row["smoke_result"]
    except (IndexError, KeyError):
        smoke_result = SMOKE_NONE
    try:
        smoke_log = row["smoke_log"]
    except (IndexError, KeyError):
        smoke_log = ""
    return SkillTemplate(
        id=row["id"], name=row["name"], description=row["description"],
        pattern=pattern, params=params, code=row["code"],
        status=row["status"], created_ts=row["created_ts"],
        approved_ts=row["approved_ts"], use_count=row["use_count"],
        last_used_ts=row["last_used_ts"], category=row["category"],
        dangerous=bool(row["dangerous"]), notes=row["notes"],
        smoke_result=smoke_result, smoke_log=smoke_log,
    )


class LearnedSkill(BaseTool):
    """Runtime instance of an approved ``SkillTemplate``.

    The persisted pattern is executed sequentially through ``ToolExecutor``.
    Template ``code`` is never evaluated, so an edited SQLite row cannot gain
    arbitrary Python execution or bypass the approval boundary.
    """

    def __init__(self, template: SkillTemplate, *, registry: Any,
                 executor: Any | None = None) -> None:
        self._template = template
        self._registry = registry
        # Optional ToolExecutor: when supplied, constituent calls are routed
        # through it (see _make_call_tool) so arg/context-dependent gating —
        # gate.is_dangerous() inspecting e.g. a shell command string, or
        # check_path() on a file write — applies per constituent call, not
        # just to the composite's own static `dangerous` flag.
        self._executor = executor
        self.spec = ToolSpec(
            name=template.name,
            description=template.description,
            parameters=template.params,
            dangerous=template.dangerous,
            category=template.category,
        )

    async def execute(self, **params: Any) -> ToolResult:
        try:
            return await self._run(params)
        except Exception as exc:  # noqa: BLE001 - body is auto-generated
            log.warning("learned skill %s failed: %s", self.spec.name, exc)
            return ToolResult(
                tool_name=self.spec.name, success=False,
                content=f"[learned_skill error: {type(exc).__name__}: {exc}]",
            )

    async def _run(self, params: dict) -> ToolResult:
        call_tool = _make_call_tool(self._registry, executor=self._executor)
        results: list[ToolResult] = []
        for name in self._template.pattern:
            args = params.get(name, {})
            if not isinstance(args, dict):
                raise ValueError(f"arguments for {name!r} must be an object")
            result = await call_tool(name, args)
            results.append(result)
            if not result.success:
                break
        if results:
            tail = results[-1]
            return ToolResult(tool_name=self.spec.name, success=tail.success,
                              content=tail.content)
        return ToolResult(tool_name=self.spec.name, success=False,
                          content="[learned_skill: empty pattern]")


def _registry_snapshot(registry: Any) -> dict:
    """Name -> tool mapping for either a ToolRegistry-like object (exposing
    ``.snapshot()``) or a plain ``dict`` registry (e.g. the gateway's
    ``hive.tools``). Both shapes are used across the codebase."""
    if hasattr(registry, "snapshot"):
        return registry.snapshot()
    if isinstance(registry, dict):
        return registry
    return {}


def _pattern_is_dangerous(pattern: Sequence[str], registry: Any) -> bool:
    """True if any tool named in ``pattern`` is flagged dangerous in ``registry``.

    A learned skill is a composite that runs its constituent tools without
    per-step approval (see ``_make_call_tool``); the ONLY gate a dangerous
    constituent gets is the gate on the composite skill itself when the
    ToolExecutor dispatches it. So the composite must inherit danger from any
    tool it wraps — otherwise wrapping ``shell`` (or any other dangerous
    tool) in a 1-step "pattern" silently launders it into an unguarded call.
    Unknown tool names (not yet in the registry, or no registry supplied) are
    treated as dangerous too: fail closed rather than assume safety.
    """
    snapshot = _registry_snapshot(registry)
    for tool_name in pattern:
        tool = snapshot.get(tool_name)
        if tool is None or getattr(getattr(tool, "spec", None), "dangerous", True):
            return True
    return False


def _make_call_tool(registry: Any, *, executor: Any | None = None) -> Callable[[str, dict], Any]:
    """Return an async ``call_tool`` bound to the live registry snapshot.

    When ``executor`` (a ``ToolExecutor``) is supplied, each constituent call
    is routed through ``executor.execute()`` instead of calling
    ``tool.execute()`` directly. That's the SAME choke point every other tool
    call in Hive goes through, so a constituent tool whose danger is
    argument/context-dependent — ``shell`` (gate.is_dangerous inspects the
    command string) or a file-writing tool (check_path on the target path) —
    gets gated on ITS OWN, not just on the composite's static ``dangerous``
    flag. A gated constituent doesn't silently proceed: the composite call
    fails with the approval id surfaced, exactly like any other pending
    dangerous call would.

    Without an executor, fail closed.  A bare registry has no per-call gate,
    so directly invoking a child tool would recreate the approval bypass.
    """
    async def call_tool(name: str, args: dict) -> ToolResult:
        args = args or {}
        if executor is not None:
            from hive.tools.executor import DispatchStatus
            dispatch = await executor.execute(name, args, reason=f"learned_skill:{name}")
            if dispatch.status == DispatchStatus.OK:
                return dispatch.result
            if dispatch.status == DispatchStatus.PENDING:
                return ToolResult(
                    tool_name=name, success=False,
                    content=(f"[learned_skill: {name!r} requires approval "
                             f"(approval_id={dispatch.approval_id})]"),
                )
            return ToolResult(tool_name=name, success=False,
                              content=f"[learned_skill: {name!r} failed: {dispatch.error}]")
        return ToolResult(tool_name=name, success=False,
                          content="[learned_skill: ToolExecutor is required]")
    return call_tool


# ---------- registration bridge -----------------------------------------------

def add_learned_skill(
    template: SkillTemplate,
    *,
    registry: Any,
    skill_usage: Any | None = None,
    store: LearnedSkillStore | None = None,
    auto_approve: bool = False,
    executor: Any | None = None,
) -> SkillTemplate:
    """Persist ``template`` and (when approved) register it as a live tool.

    Returns the persisted template (with status updated). When ``store`` is
    provided the template is saved there; when ``skill_usage`` is provided the
    skill is tracked via the existing ``SkillUsageStore`` as
    ``agent_created=True`` so the Curator treats it like any other learned
    capability. If the template is already REGISTERED (live in the registry),
    this is a no-op and the existing template is returned unchanged.

    ``executor`` (a ``ToolExecutor``), when supplied, is threaded into the
    registered ``LearnedSkill`` so its constituent calls are individually
    gated (see ``_make_call_tool``), and the skill is added to the executor
    itself (mirroring how ``runtime.load_mcp_servers`` keeps ``hive.tools``
    and ``hive.tool_executor`` in sync) so top-level dispatch through the
    executor can find it too.

    ``skill_usage`` is duck-typed (``register(name, agent_created=True)``) — the
    module deliberately doesn't import the concrete class so the ``tools`` DAG
    stays free of ``hive.memory`` (architecture test invariant).
    """
    # Idempotent: if the template is already live in the registry, don't
    # downgrade its status or overwrite the stored row.
    existing = _registry_snapshot(registry).get(template.name)
    if existing is not None and getattr(existing, "__class__", None) is LearnedSkill:
        # Already registered — return the persisted copy without mutation.
        if store is not None:
            persisted = store.get(template.id)
            if persisted is not None:
                return persisted
        template.status = STATUS_REGISTERED
        return template
    # Defense in depth: re-derive ``dangerous`` from the LIVE registry right
    # before persisting/registering, regardless of what propose_skill saw
    # (it may have run with no registry, an older snapshot, or been forged
    # by a caller). A composite that wraps a dangerous tool must never be
    # registered as non-dangerous — that would let the approval gate skip it
    # on every future call via ``_make_call_tool``.
    if _pattern_is_dangerous(template.pattern, registry):
        template.dangerous = True
    template.status = STATUS_APPROVED if auto_approve else template.status
    if auto_approve and not template.approved_ts:
        template.approved_ts = time.time()
    if store is not None:
        store.save(template)
    if skill_usage is not None and hasattr(skill_usage, "register"):
        # Track usage even when not yet registered — so the Curator doesn't
        # pre-emptively archive the just-proposed skill.
        skill_usage.register(template.name, agent_created=True)
    if template.status == STATUS_APPROVED:
        skill = LearnedSkill(template, registry=registry, executor=executor)
        if hasattr(registry, "add"):
            registry.add(skill)
        elif isinstance(registry, dict):
            registry[template.name] = skill
        else:
            return template
        if executor is not None and hasattr(executor, "add_tool"):
            executor.add_tool(skill)
        template.status = STATUS_REGISTERED
        if store is not None:
            store.save(template)
    return template
