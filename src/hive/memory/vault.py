"""
vault.py — Obsidian long-term store (human-readable markdown export).

Ported from Memory/brain._promote_to_vault. The vault is the durable, linkable
"old memories" layer: one markdown note per learning, grouped by kind, with YAML
frontmatter. This is a named product artifact (an export), which is exactly the
file-storage the SQLite-first rule allows — runtime state stays in SQLite.
"""
from __future__ import annotations

import time
from pathlib import Path


def _safe_name(topic: str) -> str:
    return "".join(c if c.isalnum() or c in " -_" else "_" for c in topic)[:80] or "untitled"


class ObsidianVault:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def _kind_dir(self, kind: str) -> Path:
        """Return one direct child of the vault root, never a caller-selected path."""
        if not kind or kind in {".", ".."}:
            raise ValueError("vault kind must name one non-empty direct folder")
        root = self._root.resolve()
        folder = (root / kind).resolve()
        if folder.parent != root:
            raise ValueError("vault kind must not escape the vault root")
        return folder

    def write(self, kind: str, topic: str, content: str, source: str = "") -> Path:
        folder = self._kind_dir(kind)
        folder.mkdir(parents=True, exist_ok=True)
        note = folder / f"{_safe_name(topic)}.md"
        body = (
            f"---\nkind: {kind}\ntopic: {topic}\nsource: {source}\n"
            f"created: {time.strftime('%Y-%m-%d %H:%M')}\ntags: [{kind}]\n---\n\n"
            f"# {topic}\n\n{content}\n"
        )
        note.write_text(body, encoding="utf-8")
        return note

    def stats(self) -> dict[str, int]:
        notes = list(self._root.rglob("*.md")) if self._root.exists() else []
        return {"notes": len(notes)}

    def read(self, kind: str, topic: str) -> str | None:
        """Return the body of a note (without YAML frontmatter), or None if not found."""
        note = self._kind_dir(kind) / f"{_safe_name(topic)}.md"
        if not note.exists():
            return None
        raw = note.read_text(encoding="utf-8")
        # Strip YAML frontmatter (between first two "---" lines)
        if raw.startswith("---"):
            end = raw.find("---", 3)
            if end != -1:
                return raw[end + 3:].lstrip("\n")
        return raw

    def list_notes(self, kind: str | None = None) -> list[dict]:
        """Return metadata for all notes, optionally filtered by kind."""
        if kind:
            self._kind_dir(kind)
        if not self._root.exists():
            return []
        pattern = "*.md" if kind else "**/*.md"
        notes = []
        search_root = self._kind_dir(kind) if kind else self._root
        for path in search_root.glob(pattern):
            rel = path.relative_to(self._root)
            parts = rel.parts
            note_kind = parts[0] if len(parts) > 1 else ""
            note_topic = path.stem
            notes.append({
                "kind": note_kind,
                "topic": note_topic,
                "path": str(path),
                "modified": path.stat().st_mtime,
            })
        notes.sort(key=lambda n: n["modified"], reverse=True)
        return notes

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Full-text search across vault notes; ranked by term frequency."""
        if not self._root.exists() or not query:
            return []
        terms = query.lower().split()
        results: list[dict] = []
        for path in self._root.rglob("*.md"):
            raw = path.read_text(encoding="utf-8", errors="replace").lower()
            score = sum(raw.count(t) for t in terms)
            if score == 0:
                continue
            # Build a short snippet around the first matching term
            snippet = ""
            for term in terms:
                idx = raw.find(term)
                if idx != -1:
                    start = max(0, idx - 60)
                    snippet = "..." + raw[start:idx + 80].replace("\n", " ").strip() + "..."
                    break
            rel = path.relative_to(self._root)
            parts = rel.parts
            results.append({
                "kind": parts[0] if len(parts) > 1 else "",
                "topic": path.stem,
                "snippet": snippet[:200],
                "score": score,
            })
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:limit]
