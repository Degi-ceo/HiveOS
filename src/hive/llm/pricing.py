"""
pricing.py — per-token cost estimation for the budgeter.

A trimmed adaptation of Hermes agent/usage_pricing.py (HERMES_REFERENCE REUSE-READY
#9). Hermes fetches live pricing from provider metadata APIs; HiveOS keeps a small
static table (env-overridable) so the budgeter can estimate cost without a network
call on every turn. MiniMax Token-Plan billing is credit-window based (the budgeter
also polls the remains endpoint), so these rates are an *estimate* for cost telemetry,
not the source of truth for the hard budget gate.

Override a rate from the environment:
    HIVE_PRICE_<MODEL>_IN  / HIVE_PRICE_<MODEL>_OUT   (USD per million tokens)
where <MODEL> is the model id upper-cased with non-alnum -> '_'.
"""
from __future__ import annotations

import os

# Conservative defaults (USD per 1M tokens). MiniMax credit plans are effectively
# flat-rate, so these are nominal estimates for telemetry; tune via env as needed.
_DEFAULT_RATES: dict[str, tuple[float, float]] = {
    "MiniMax-M3": (0.30, 1.20),
    "MiniMax-M2.7": (0.20, 0.80),
    "MiniMax-M2": (0.20, 0.80),
}

# Fallback when a model id is unknown (keeps cost non-zero so it stays visible).
_FALLBACK_RATE = (0.30, 1.20)


def _env_key(model: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in model).upper()


def rate_for(model: str) -> tuple[float, float]:
    """(input_per_million, output_per_million) for a model, env-override aware."""
    base_in, base_out = _DEFAULT_RATES.get(model, _FALLBACK_RATE)
    key = _env_key(model)
    in_env = os.getenv(f"HIVE_PRICE_{key}_IN")
    out_env = os.getenv(f"HIVE_PRICE_{key}_OUT")
    try:
        if in_env is not None:
            base_in = float(in_env)
        if out_env is not None:
            base_out = float(out_env)
    except ValueError:
        pass  # malformed override -> keep defaults
    return base_in, base_out


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimated USD cost for one call."""
    rin, rout = rate_for(model)
    return (input_tokens / 1_000_000) * rin + (output_tokens / 1_000_000) * rout


def get_pricing(model: str) -> "PricingEntry":
    """Return a PricingEntry with input_per_mtok and output_per_mtok for the model."""
    rin, rout = rate_for(model)
    return PricingEntry(input_per_mtok=rin, output_per_mtok=rout)


class PricingEntry:
    """Simple container for per-million-token rates."""
    __slots__ = ("input_per_mtok", "output_per_mtok")

    def __init__(self, input_per_mtok: float, output_per_mtok: float) -> None:
        self.input_per_mtok = input_per_mtok
        self.output_per_mtok = output_per_mtok


def estimate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD for a given model and token counts."""
    pricing = get_pricing(model_id)
    return (input_tokens * pricing.input_per_mtok + output_tokens * pricing.output_per_mtok) / 1_000_000
