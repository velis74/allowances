"""Turning a fractional cost into a whole unit, per the consuming currency's ``round_fn``."""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

ROUND_MODES = {
    "floor": ROUND_FLOOR,
    "ceil": ROUND_CEILING,
    "round": ROUND_HALF_UP,
}


def apply_round(value: Decimal, round_fn: str | None) -> Decimal:
    """
    Round ``value`` to a whole unit. ``round_fn=None`` means the currency is happy with fractions
    and the value is returned untouched.
    """
    if round_fn is None:
        return value
    try:
        mode = ROUND_MODES[round_fn]
    except KeyError:
        raise ValueError(f"Unknown round_fn: {round_fn!r} (expected one of {sorted(ROUND_MODES)} or None)") from None
    return value.to_integral_value(rounding=mode)
