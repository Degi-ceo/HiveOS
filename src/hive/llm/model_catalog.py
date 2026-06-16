"""
model_catalog.py — per-model capability / compat config.

Pattern from OpenClaw model-catalog-core (docs/references/OPENCLAW_REFERENCE.md
§"Model catalog compat config"): the adapter must not hardcode per-model quirks
(thinking format, tool support, context window). The catalog holds them so model
strings stay env-driven and a NEW MiniMax id (M2 -> M3 churn) resolves to a safe
conservative default instead of crashing.
"""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class ModelEntry:
    model_id: str
    provider: str = "minimax"
    context_length: int = 192_000
    max_output: int = 8_192
    supports_thinking: bool = True
    supports_tools: bool = True
    thinking_budget: int = 2_048


# Known models. Unknown ids fall back to a conservative default (see get()).
_DEFAULTS: dict[str, ModelEntry] = {
    "MiniMax-M3": ModelEntry("MiniMax-M3", context_length=192_000, max_output=8_192),
    "MiniMax-M2.7": ModelEntry("MiniMax-M2.7", context_length=192_000, max_output=8_192),
    "MiniMax-M2": ModelEntry("MiniMax-M2", context_length=192_000, max_output=8_192),
}


class ModelCatalog:
    def __init__(self, entries: dict[str, ModelEntry] | None = None) -> None:
        self._entries: dict[str, ModelEntry] = dict(_DEFAULTS)
        if entries:
            self._entries.update(entries)

    def register(self, entry: ModelEntry) -> None:
        self._entries[entry.model_id] = entry

    def get(self, model_id: str) -> ModelEntry:
        """Known entry, else a conservative default tagged with the requested id."""
        entry = self._entries.get(model_id)
        if entry is not None:
            return entry
        # Conservative default for unknown MiniMax models: full context, thinking enabled.
        # Using hardcoded values rather than dict lookup so get() never raises KeyError.
        return ModelEntry(
            model_id=model_id,
            context_length=192_000,
            max_output=8_192,
            supports_thinking=True,
            supports_tools=True,
            thinking_budget=2_048,
        )

    def __contains__(self, model_id: str) -> bool:
        return model_id in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(self._entries.values())
