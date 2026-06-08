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

    def write(self, kind: str, topic: str, content: str, source: str = "") -> Path:
        folder = self._root / kind
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
