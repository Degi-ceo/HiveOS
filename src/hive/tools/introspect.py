"""introspect.py — AST-based self-introspection of Hive's tool surface.

Walks tools/builtins/ + tools/mcp/, AST-parses each module, and extracts every
BaseTool subclass with its name, docstring, and declared spec attributes.
Search uses deterministic token overlap so the same query always returns the
same ranking — no LLM, no embeddings, no third-party AST libs."""
from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

_TOOLS_DIR = Path(__file__).parent
_TOOL_ROOTS: tuple[Path, ...] = (_TOOLS_DIR / "builtins", _TOOLS_DIR / "mcp")

_BASETOOL_NAMES = {"BaseTool", "_Gated", "_GitHubBase"}

# Tokenize CamelCase + snake_case + kebab-case. Alternatives (first match wins):
#   [A-Z]+(?=[A-Z][a-z])  acronym run ending where the next CamelCase word begins
#                          (e.g. 'XML' in 'XMLParser')
#   [A-Z][a-z]+            CamelCase word starting with uppercase
#   [A-Z]+[a-z]            acronym with a trailing lowercase (e.g. 'PRs' -> 'PR', 's')
#   [A-Z]+                 pure acronym (no following CamelCase word)
#   [a-z]+                 lowercase word
#   \d+                    digit run
_TOKEN_RE = re.compile(
    r"[A-Z]+(?=[A-Z][a-z])|[A-Z][a-z]+|[A-Z]+[a-z]|[A-Z]+|[a-z]+|\d+")
_SUBSTRING_MIN = 2


def _is_basetool_subclass(node: ast.ClassDef) -> bool:
    """True when the class inherits from any known BaseTool-like ancestor."""
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id in _BASETOOL_NAMES:
            return True
    return False


def _class_assign_strs(node: ast.ClassDef) -> dict[str, str]:
    """Lift top-level string attribute assignments on a class body.

    Handles plain `name = "x"` assignments and `spec = ToolSpec(name="x",
    description="y", ...)` keyword-argument strings (used by most concrete
    BaseTool subclasses). Other ToolSpec parameters (parameters dict,
    category, dangerous) are kept as raw repr-strings for keyword matches."""
    out: dict[str, str] = {}
    for stmt in node.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
            continue
        target = stmt.targets[0].id
        if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
            out[target] = stmt.value.value
            continue
        if isinstance(stmt.value, ast.Call):
            call = stmt.value
            if isinstance(call.func, ast.Name) and call.func.id == "ToolSpec":
                for kw in call.keywords:
                    if (kw.arg in {"name", "description", "category"}
                            and isinstance(kw.value, ast.Constant)
                            and isinstance(kw.value.value, str)):
                        out[kw.arg] = kw.value.value
    return out


def _extract_tool_from_class(node: ast.ClassDef, module: str) -> dict[str, Any] | None:
    """Build an index entry for a single BaseTool subclass, or None to skip."""
    if not _is_basetool_subclass(node):
        return None
    doc = ast.get_docstring(node) or ""
    attrs = _class_assign_strs(node)
    name = attrs.get("name") or attrs.get("_name") or node.name
    description = (attrs.get("description") or attrs.get("_desc")
                   or (doc.splitlines()[0] if doc else ""))
    category = attrs.get("category", "")
    return {"name": name, "module": module, "class": node.name,
            "doc": doc, "description": description, "category": category}


def _index_module(path: Path) -> list[dict[str, Any]]:
    """Parse one .py file and return BaseTool subclass entries. Malformed files
    are logged at WARNING and return [] — never raised."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        log.warning("introspect: skipping malformed module %s: %s", path, exc)
        return []
    except OSError as exc:
        log.warning("introspect: cannot read %s: %s", path, exc)
        return []
    module = path.stem
    entries: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            entry = _extract_tool_from_class(node, module)
            if entry is not None:
                entries.append(entry)
    return entries


def _walk_sources(roots: Iterable[Path]) -> Iterable[Path]:
    """Yield every .py file under each root, sorted for determinism."""
    seen: set[Path] = set()
    paths: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*.py")):
            if p in seen:
                continue
            seen.add(p)
            paths.append(p)
    return paths


def index(roots: tuple[Path, ...] = _TOOL_ROOTS) -> list[dict[str, Any]]:
    """Walk tool modules, AST-parse, extract BaseTool subclasses.

    Returns a sorted list of {"name", "module", "class", "doc", "description",
    "category"}. Malformed modules are skipped with a logged warning."""
    entries: list[dict[str, Any]] = []
    for path in _walk_sources(roots):
        entries.extend(_index_module(path))
    entries.sort(key=lambda e: (e["module"], e["class"]))
    return entries


def _tokenize(text: str) -> set[str]:
    """Split CamelCase / snake_case / kebab-case into lowercase tokens."""
    return {t.lower() for t in _TOKEN_RE.findall(text)}


def _score(entry: dict[str, Any], query_tokens: set[str]) -> float:
    """Deterministic score: token overlap + prefix + raw-substring matches.

    A query token counts as a hit when:
      * it equals a hay token, OR
      * the shorter of (qt, ht) is >= _SUBSTRING_MIN and is a prefix of the longer, OR
      * it appears as a substring in the lowercased name field.
    Final score is hits / len(query_tokens), in [0,1]."""
    if not query_tokens:
        return 0.0
    hay = " ".join([entry.get("name", ""), entry.get("description", ""),
                    entry.get("doc", ""), entry.get("class", "")]).lower()
    hay_tokens = _tokenize(hay)
    if not hay_tokens:
        return 0.0
    name_field = str(entry.get("name", "")).lower()
    hits = 0
    for qt in query_tokens:
        if qt in hay_tokens:
            hits += 1
            continue
        for ht in hay_tokens:
            short, long = (qt, ht) if len(qt) <= len(ht) else (ht, qt)
            if len(short) >= _SUBSTRING_MIN and long.startswith(short):
                hits += 1
                break
        else:
            if len(qt) >= _SUBSTRING_MIN and qt in name_field:
                hits += 1
    return hits / len(query_tokens)


def search(query: str, k: int = 5, *, idx: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Score-based search over the AST index. Returns top-k hits with score and
    'source': 'ast' attribution. Pure & deterministic: same input → same output."""
    entries = idx if idx is not None else index()
    qt = _tokenize(query)
    scored: list[tuple[float, dict[str, Any]]] = []
    for entry in entries:
        s = _score(entry, qt)
        if s > 0:
            hit = dict(entry)
            hit["score"] = s
            hit["source"] = "ast"
            scored.append((s, hit))
    scored.sort(key=lambda x: (-x[0], x[1]["module"], x[1]["class"]))
    return [hit for _, hit in scored[:k]]


def format_for_discover(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shape AST search hits to match the existing discover() candidate schema,
    adding 'source': 'ast' attribution."""
    out: list[dict[str, Any]] = []
    for r in results:
        out.append({"source": r.get("source", "ast"),
                    "name": r.get("name", ""),
                    "class": r.get("class", ""),
                    "module": r.get("module", ""),
                    "description": r.get("description", "") or r.get("doc", ""),
                    "score": r.get("score", 0.0)})
    return out
