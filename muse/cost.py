"""What one call cost, for the daily paid cap.

Why this order (checked against LiteLLM 1.108.6, see muse/hook.py):
1. tier free | trial | local costs 0 by definition. LiteLLM prices a model by
   its name, so a free-tier Gemini call would otherwise be billed at the paid
   list price.
2. A catalogue `price:` (USD per million input/output tokens, rendered as
   model_info.muse_price_in / muse_price_out) is what the user approved the cap
   against, so it wins over LiteLLM's built-in map. It ignores cache discounts,
   which can only over-count, the safe direction for a cap.
3. Otherwise LiteLLM's `standard_logging_object["response_cost"]`, but only a
   positive finite number: for a model missing from its map LiteLLM reports 0.0
   (with `response_cost_failure_debug_info` set), which would under-count.
4. Otherwise unknown (None). The ledger keeps NULL and the paid-spend query
   counts such calls separately.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from muse.ration import is_paid, price_of, tier_of


def from_price(price: tuple[float, float], tokens_in: int | None, tokens_out: int | None) -> float | None:
    """USD for the given token counts; None if a count is missing."""
    if tokens_in is None and tokens_out is None:
        return None
    tin = max(0, int(tokens_in or 0))
    tout = max(0, int(tokens_out or 0))
    return (tin * price[0] + tout * price[1]) / 1_000_000


def litellm_cost(slo: Mapping[str, Any] | None, kwargs: Mapping[str, Any] | None = None) -> float | None:
    """LiteLLM's computed cost when it is trustworthy (positive, finite, no failure note)."""
    for source in (slo or {}, kwargs or {}):
        if not isinstance(source, Mapping):
            continue
        if source.get("response_cost_failure_debug_info"):
            return None
        value = source.get("response_cost")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        value = float(value)
        if math.isfinite(value) and value > 0:
            return value
    return None


def call_cost(
    info: Mapping[str, Any],
    tokens_in: int | None,
    tokens_out: int | None,
    slo: Mapping[str, Any] | None = None,
    kwargs: Mapping[str, Any] | None = None,
) -> tuple[float | None, bool]:
    """(cost_usd or None if unknown, paid) for one successful call."""
    paid = is_paid(info)
    if tier_of(info) in ("free", "trial", "local"):
        return 0.0, False
    price = price_of(info)
    if price is not None:
        cost = from_price(price, tokens_in, tokens_out)
        if cost is not None:
            return cost, paid
    return litellm_cost(slo, kwargs), paid
