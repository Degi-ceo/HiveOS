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
STATUS_PROPOSED = "proposed"
STATUS_APPROVED = "approved"
STATUS_REGISTERED = "registered"
STATUS_REJECTED = "rejected"
STATUS_ARCHIVED = "archived"

ALL_STATUSES = (STATUS_PROPOSED, STATUS_APPROVED, STATUS_REGISTERED, STATUS_REJECTED,
                STATUS_ARCHIVED)


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
) -> SkillTemplate:
    """Build a ``SkillTemplate`` from a detected tool sequence.

    The generated body is a small python function that calls each tool in order
    via the runtime-provided ``call_tool(name, args)`` helper. Inputs to the
    skill are auto-inferred from the union of tool argument keys; extra params
    can be appended via ``extra_params``.
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
    return SkillTemplate(
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
  notes       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tool_sequences(
  seq_key   TEXT PRIMARY KEY,
  pattern   TEXT NOT NULL,
  count     INTEGER NOT NULL,
  last_seen REAL NOT NULL
);
"""


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
        self._db.commit()

    # ---- templates -----------------------------------------------------------

    def save(self, template: SkillTemplate) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO learned_skills"
            "(id, name, description, pattern, params, code, status, created_ts,"
            " approved_ts, use_count, last_used_ts, category, dangerous, notes)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (template.id, template.name, template.description,
             json.dumps(list(template.pattern)),
             json.dumps(template.params), template.code, template.status,
             template.created_ts, template.approved_ts, template.use_count,
             template.last_used_ts, template.category, int(template.dangerous),
             template.notes),
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
    return SkillTemplate(
        id=row["id"], name=row["name"], description=row["description"],
        pattern=pattern, params=params, code=row["code"],
        status=row["status"], created_ts=row["created_ts"],
        approved_ts=row["approved_ts"], use_count=row["use_count"],
        last_used_ts=row["last_used_ts"], category=row["category"],
        dangerous=bool(row["dangerous"]), notes=row["notes"],
    )


# ---------- dynamic tool execution --------------------------------------------

class _ExecError(Exception):
    """Raised when an auto-learned body fails to execute or compile."""


class LearnedSkill(BaseTool):
    """Runtime instance of an approved ``SkillTemplate``.

    The generated body runs with one helper in its globals: ``call_tool``,
    which is bound at execution time to the live ``ToolRegistry.snapshot()``.
    The helper is async (matching ``BaseTool.execute``); bodies ``await`` it.
    """

    def __init__(self, template: SkillTemplate, *, registry: Any) -> None:
        self._template = template
        self._registry = registry
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
        call_tool = _make_call_tool(self._registry)
        scope: dict[str, Any] = {"__name__": f"learned:{self._template.id}"}
        # Compile once per template, cached on the spec.
        code = _get_or_compile(self._template.id, self._template.code)
        try:
            exec(code, scope)
        except Exception as exc:
            raise _ExecError(f"compile/run failed: {exc}") from exc
        run_fn = scope.get("run")
        if not callable(run_fn):
            raise _ExecError("generated body did not define run()")
        result = await run_fn(call_tool, params)
        # The body returns a list of ToolResults; surface the last one for clarity.
        if isinstance(result, list) and result:
            tail = result[-1]
            if isinstance(tail, ToolResult):
                return ToolResult(
                    tool_name=self.spec.name,
                    success=tail.success,
                    content=tail.content,
                )
        return ToolResult(tool_name=self.spec.name,
                          content=str(result)[:8000])


_CODE_CACHE: dict[str, Any] = {}


def _get_or_compile(template_id: str, code: str) -> Any:
    cached = _CODE_CACHE.get(template_id)
    if cached is not None:
        return cached
    compiled = compile(code, f"<learned:{template_id}>", "exec")
    _CODE_CACHE[template_id] = compiled
    return compiled


def _make_call_tool(registry: Any) -> Callable[[str, dict], Any]:
    """Return an async ``call_tool`` bound to the live registry snapshot."""
    async def call_tool(name: str, args: dict) -> ToolResult:
        tool = registry.snapshot().get(name) if hasattr(registry, "snapshot") else None
        if tool is None:
            return ToolResult(tool_name=name, success=False,
                              content=f"[learned_skill: unknown tool {name!r}]")
        return await tool.execute(**(args or {}))
    return call_tool


# ---------- registration bridge -----------------------------------------------

def add_learned_skill(
    template: SkillTemplate,
    *,
    registry: Any,
    skill_usage: Any | None = None,
    store: LearnedSkillStore | None = None,
    auto_approve: bool = False,
) -> SkillTemplate:
    """Persist ``template`` and (when approved) register it as a live tool.

    Returns the persisted template (with status updated). When ``store`` is
    provided the template is saved there; when ``skill_usage`` is provided the
    skill is tracked via the existing ``SkillUsageStore`` as
    ``agent_created=True`` so the Curator treats it like any other learned
    capability. If the template is already REGISTERED (live in the registry),
    this is a no-op and the existing template is returned unchanged.

    ``skill_usage`` is duck-typed (``register(name, agent_created=True)``) — the
    module deliberately doesn't import the concrete class so the ``tools`` DAG
    stays free of ``hive.memory`` (architecture test invariant).
    """
    # Idempotent: if the template is already live in the registry, don't
    # downgrade its status or overwrite the stored row.
    existing = registry.snapshot().get(template.name) if hasattr(registry, "snapshot") else None
    if existing is not None and getattr(existing, "__class__", None) is LearnedSkill:
        # Already registered — return the persisted copy without mutation.
        if store is not None:
            persisted = store.get(template.id)
            if persisted is not None:
                return persisted
        template.status = STATUS_REGISTERED
        return template
    template.status = STATUS_APPROVED if auto_approve else template.status
    if auto_approve and not template.approved_ts:
        template.approved_ts = time.time()
    if store is not None:
        store.save(template)
    if skill_usage is not None and hasattr(skill_usage, "register"):
        # Track usage even when not yet registered — so the Curator doesn't
        # pre-emptively archive the just-proposed skill.
        skill_usage.register(template.name, agent_created=True)
    if template.status == STATUS_APPROVED and hasattr(registry, "add"):
        registry.add(LearnedSkill(template, registry=registry))
        template.status = STATUS_REGISTERED
        if store is not None:
            store.save(template)
    return template
