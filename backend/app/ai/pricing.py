"""Token usage and cost arithmetic (exact decimals, never floats)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.config import Settings

MILLION = Decimal(1_000_000)
_SIX_PLACES = Decimal("0.000001")
# Rough upper bound for estimating input tokens before a call (English ~4 chars/token).
CHARS_PER_TOKEN_ESTIMATE = 3


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0


def cost_usd(usage: Usage, settings: Settings) -> Decimal:
    """Billed cost of one call.

    Cached reads and cache writes are subsets of input tokens: cached reads use
    the cached price, cache writes cost the input price times the write
    multiplier, and the rest uses the normal input price. Reasoning tokens are
    billed as output tokens and are already included in output_tokens.
    """
    cached = min(usage.cached_input_tokens, usage.input_tokens)
    writes = min(usage.cache_write_tokens, usage.input_tokens - cached)
    plain = usage.input_tokens - cached - writes
    total = (
        plain * settings.price_input_per_mtok
        + cached * settings.price_cached_input_per_mtok
        + writes * settings.price_input_per_mtok * settings.price_cache_write_multiplier
        + usage.output_tokens * settings.price_output_per_mtok
    ) / MILLION
    return total.quantize(_SIX_PLACES, rounding=ROUND_HALF_UP)


def worst_case_cost_usd(input_chars: int, max_output_tokens: int, settings: Settings) -> Decimal:
    """Upper bound reserved before a call: generous input estimate plus the full output cap."""
    estimated_input = Usage(
        input_tokens=input_chars // CHARS_PER_TOKEN_ESTIMATE + 2000,
        output_tokens=max_output_tokens,
    )
    return cost_usd(estimated_input, settings)
