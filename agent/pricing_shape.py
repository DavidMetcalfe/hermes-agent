"""Pricing-entry shape tolerance for endpoint ``/models`` metadata (PR 3).

OpenAI-compatible ``/models`` endpoints do not agree on a pricing convention:

- OpenRouter-style: ``pricing.prompt = "0.00000075"`` — **dollars per token**,
  keys ``prompt``/``completion``, flat.
- Per-million style (observed on a hosted LLM gateway): ``pricing.global.prompt
  = 0.75`` — **dollars per million**, keys ``prompt``/``completions``, nested
  under ``global`` with a ``regional_increase_percent`` sibling.

The parser assumed the OpenRouter shape unconditionally. On a per-million
endpoint that produced a 1,000,000× inflated input rate ($750,000/M for a
$0.75/M model), dropped the output rate entirely (``completions`` alias not
recognized), and displayed a session cost of hundreds of thousands of dollars.

The unit is inferred per value: OpenRouter per-token prices for real models are
always < $0.01/token (that would be $10,000+/M); any value ≥ 0.01 can only be a
per-million figure. Nested ``global`` containers are unwrapped, and the
``completions`` alias joins the completion-key chain.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# A genuine per-token price >= $0.01/token means $10,000+/M — no real model.
# Values at/above this are per-million figures served without a unit suffix.
_PER_TOKEN_CEILING = Decimal("0.01")

_NEST_KEYS = ("global", "default", "usd")

_PROMPT_KEYS = ("prompt", "input", "input_cost")
_COMPLETION_KEYS = ("completion", "completions", "output", "output_cost")
_CACHE_READ_KEYS = ("cache_read", "cached_prompt", "input_cache_read")
_CACHE_WRITE_KEYS = ("cache_write", "cache_creation", "input_cache_write")


def _to_decimal(raw: Any) -> Optional[Decimal]:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return Decimal(str(raw))
    except Exception:  # noqa: BLE001 — malformed metadata must not break pricing
        return None


def unwrap_pricing_container(pricing: Any) -> Dict[str, Any]:
    """Flatten ``{global: {...}}`` / ``{default: {...}}`` / ``{usd: {...}}``
    nestings some endpoints use into a flat key->value dict."""
    if not isinstance(pricing, dict):
        return {}
    flat: Dict[str, Any] = {}
    for key, value in pricing.items():
        if isinstance(value, dict):
            flat.update(value)
        elif key not in _NEST_KEYS:
            flat[key] = value
    return flat


def _unit_normalized(value: Optional[Decimal]) -> Optional[Decimal]:
    """Normalize a raw pricing value to dollars-per-token scale.

    Values >= _PER_TOKEN_CEILING are per-million figures: divide. Anything below
    is already per-token. None passes through.
    """
    if value is None:
        return None
    if value >= _PER_TOKEN_CEILING:
        return value / Decimal(1_000_000)
    return value


def extract_pricing_fields(pricing: Any) -> Dict[str, Optional[Decimal]]:
    """Shape-tolerant (key -> $/token) extraction from a pricing container."""
    flat = unwrap_pricing_container(pricing)

    def first_value(*keys: str) -> Optional[Decimal]:
        for key in keys:
            value = _to_decimal(flat.get(key))
            if value is not None:
                return value
        return None

    def per_token(*keys: str) -> Optional[Decimal]:
        value = first_value(*keys)
        return _unit_normalized(value)

    return {
        "prompt": per_token(*_PROMPT_KEYS),
        "completion": per_token(*_COMPLETION_KEYS),
        "cache_read": per_token(*_CACHE_READ_KEYS),
        "cache_write": per_token(*_CACHE_WRITE_KEYS),
        "request": first_value("request"),
    }
