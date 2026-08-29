"""
entity_resolver.py — Surface-form normalization for memory entities (SPRINT_7 Batch D).

Mnemosyne stores facts by the literal text the caller supplies. "PR #95", "pr_95",
"PR-95" and "PR95" therefore become four distinct facts even though they all
refer to the same real-world entity. Retrieval then misses related work because
the exact phrase is required.

This module introduces a thin, side-effect-free helper that maps any surface form
to a deterministic canonical key and groups facts by it. It is pure-Python (no
Hive dependencies) so it can be reused by Mnemosyne consolidation, the memory
keeper, the curator, or anything else that wants to coalesce duplicates.

Layer discipline: imports Python stdlib only. Callers wire it in.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

# Pre-compiled at import-time so hot paths don't pay the regex cost.
# Keep ascii alphanumerics + Unicode "letter" + "number" categories from
# NFKD-lowercased text so e.g. "Привет" survives as "привет", not "".
_NON_KEEPABLE = re.compile(r"[^\w]+", re.UNICODE)
_WHITESPACE_RUNS = re.compile(r"\s+")


@dataclass(slots=True, frozen=True)
class ResolvedEntity:
    """Result of resolving a surface form to a canonical entity.

    Attributes:
        canonical_key: The normalized key (e.g. "pr95"). Lowercased, alphanumeric
            only, whitespace collapsed — identical for every surface form that
            refers to the same real-world entity.
        aliases: Every surface form that has produced this canonical_key via
            this resolver instance, in insertion order with duplicates removed.
            For an alias-map override the aliases list is empty (the override
            came from configuration, not observed usage).
        confidence: 1.0 for a straight normalization, 0.9 for an alias-map hit.
            Downstream code may use this to score retrieval candidates.
    """
    canonical_key: str
    aliases: list[str] = field(default_factory=list)
    confidence: float = 1.0

    @property
    def is_alias_match(self) -> bool:
        """True iff this resolution came via the alias-map override."""
        return self.confidence < 1.0

    def __post_init__(self) -> None:
        # slots + frozen=True turns aliases into a tuple; give callers a real list.
        if not isinstance(self.aliases, list):
            object.__setattr__(self, "aliases", list(self.aliases))


class EntityResolver:
    """Normalize surface forms to canonical keys; merge fact records by them.

    Two inputs collapse to the same canonical_key when they normalize identically:
        "PR #95"  -> "pr95"
        "pr_95"   -> "pr95"
        "PR-95"   -> "pr95"
        "PR95"    -> "pr95"

    An optional alias_map lets the operator declare that two surface forms map
    to the same key even when their normalized forms differ (e.g. brand-name
    drift, project code names).

    The resolver is stateless apart from the alias map and a small observed-
    alias index, so it is safe to share across threads without locking.
    """

    def __init__(self, alias_map: dict[str, str] | None = None) -> None:
        # Normalize alias-map keys the same way we normalize surfaces so the
        # caller can pass either form (e.g. {"PR-95": "pr95"} is equivalent to
        # {"pr95": "pr95"} once key is normalized).
        self._alias: dict[str, str] = {}
        for raw_src, raw_dst in (alias_map or {}).items():
            src_norm = self._normalize(raw_src)
            dst_norm = self._normalize(raw_dst) if raw_dst else src_norm
            if src_norm:
                self._alias[src_norm] = dst_norm or src_norm
        # Lazy: surface -> canonical_key for aliases that we've already seen.
        self._observed: dict[str, str] = {}

    # ------------------------------------------------------------------
    # canonical key
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(surface: str) -> str:
        """Lowercase, strip diacritics, drop non-word characters, collapse whitespace.

        UNICODE NFKD safe: "Žižek" -> "zizek" (NFKD + combining-mark strip).
        Unicode word characters survive (Cyrillic "Привет" -> "привет"), so the
        key is stable for names and entities from any script.
        Empty / whitespace-only inputs return "" so callers can detect edge cases.
        """
        if not surface:
            return ""
        # NFD splits characters into base + combining marks; drop the marks.
        decomposed = unicodedata.normalize("NFKD", surface)
        stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
        lowered = stripped.lower()
        # Strip underscores too (they're in \w but we want PR_95 -> pr95).
        no_us = lowered.replace("_", "")
        cleaned = _NON_KEEPABLE.sub("", no_us)
        return cleaned

    def canonical_key(self, surface: str) -> str:
        """Return the canonical key for *surface*.

        Alias-map overrides take precedence; otherwise the normalized surface.
        Empty surface returns "" so callers can short-circuit.
        """
        if not surface:
            return ""
        norm = self._normalize(surface)
        if not norm:
            return ""
        if norm in self._alias:
            return self._alias[norm]
        return norm

    # ------------------------------------------------------------------
    # resolve single surface
    # ------------------------------------------------------------------

    def resolve(self, surface: str) -> ResolvedEntity:
        """Return the ResolvedEntity for a single surface form.

        Resolution rules:
          - empty surface -> empty key, no aliases, 1.0 confidence
          - normalized form already in alias map -> 0.9 confidence,
            no recorded aliases (override came from configuration)
          - fresh normalized form -> 1.0 confidence, surface added to aliases
            so future merges can report the observed variants
        """
        canonical = self.canonical_key(surface)
        if not canonical:
            return ResolvedEntity(canonical_key="", aliases=[], confidence=1.0)

        # Alias-map hit: lower confidence, no observed aliases for this slot.
        if surface and self._normalize(surface) in self._alias:
            return ResolvedEntity(
                canonical_key=canonical,
                aliases=[],
                confidence=0.9,
            )

        # Fresh observation: track the original surface form so merge() reports it.
        if surface and self._observed.get(self._normalize(surface)) != canonical:
            # No entry yet OR entry points at a different canonical — record under canonical.
            self._observed.setdefault(canonical, []).append(surface)
        observed = self._observed.setdefault(canonical, [])
        # de-dupe while preserving insertion order
        seen: set[str] = set()
        deduped: list[str] = []
        for s in observed:
            if s not in seen:
                seen.add(s)
                deduped.append(s)
        self._observed[canonical] = deduped
        return ResolvedEntity(
            canonical_key=canonical,
            aliases=deduped,
            confidence=1.0,
        )

    # ------------------------------------------------------------------
    # batch merge
    # ------------------------------------------------------------------

    def merge(self, facts: Iterable[dict] | None) -> dict:
        """Group *facts* by canonical_key and merge each group into one record.

        Each fact dict is expected to carry at least:
          - ``subject`` (str): the surface form whose canonical key we group by
          - ``id``       (Any): a stable identifier for the source fact
          - ``data``    (dict, optional): fields to deep-merge into the result

        Returns a dict shaped like::

            {
              "groups": [
                {
                  "canonical_key": "pr95",
                  "aliases": ["PR #95", "pr_95", ...],
                  "fact_ids": ["mem-1", "mem-7", ...],
                  "data": {...merged...},
                  "count": 4,
                },
                ...
              ],
              "group_count": N,
              "fact_count": M,
            }

        An empty/None input yields the same shape with all-zero counts so callers
        can rely on the keys without special-casing.
        """
        facts_list = list(facts) if facts else []
        if not facts_list:
            return {"groups": [], "group_count": 0, "fact_count": 0}

        groups: dict[str, dict] = {}
        for fact in facts_list:
            if not isinstance(fact, dict):
                continue
            subject = str(fact.get("subject", ""))
            key = self.canonical_key(subject)
            if not key:
                # surface forms that normalize to "" can't be grouped
                key = f"_blank:{id(fact)}"
            bucket = groups.setdefault(key, {
                "canonical_key": key,
                "aliases": [],
                "fact_ids": [],
                "data": {},
                "count": 0,
            })
            surface = subject
            if surface and surface not in bucket["aliases"]:
                bucket["aliases"].append(surface)
            fid = fact.get("id")
            if fid is not None and fid not in bucket["fact_ids"]:
                bucket["fact_ids"].append(fid)
            data = fact.get("data") or {}
            if isinstance(data, dict):
                _deep_merge(bucket["data"], data)
            bucket["count"] += 1

        # Stable order: by canonical_key so tests are deterministic.
        ordered = [groups[k] for k in sorted(groups)]
        return {
            "groups": ordered,
            "group_count": len(ordered),
            "fact_count": len(facts_list),
        }


def _deep_merge(dst: dict, src: dict) -> dict:
    """In-place deep-merge *src* into *dst*. Lists are concatenated (set union by value)."""
    for k, v in src.items():
        if k in dst and isinstance(dst[k], dict) and isinstance(v, dict):
            _deep_merge(dst[k], v)
        elif k in dst and isinstance(dst[k], list) and isinstance(v, list):
            existing = list(dst[k])
            for item in v:
                if item not in existing:
                    existing.append(item)
            dst[k] = existing
        else:
            dst[k] = v
    return dst
