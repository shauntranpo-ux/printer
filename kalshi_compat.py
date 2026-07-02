"""kalshi_compat.py - Fixed-point <-> int conversion helpers for Kalshi API responses.

Mar 12 2026: Kalshi removed integer count/price fields from all responses.
New fields: count_fp, filled_count_fp, yes_price_dollars, no_price_dollars, fee_cost (dollars string).

All functions are pure, dependency-free (stdlib only), and fall back to legacy
integer fields during the transition window.
"""
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Optional

__all__ = [
    "fp_to_int",
    "dollars_to_cents",
    "extract_order_counts",
    "extract_fill_price_cents",
    "extract_fee_cents",
]


def fp_to_int(fp_str_or_none) -> Optional[int]:
    """Convert fixed-point contract count string to whole-contract integer.

    "10.00" -> 10, "10.50" -> 10 (truncates - bot only trades whole contracts).
    None/"" -> None. Parse error -> None. Negative value -> ValueError.
    """
    if fp_str_or_none is None or fp_str_or_none == "":
        return None
    try:
        val = float(fp_str_or_none)
    except (TypeError, ValueError):
        return None
    if val < 0:
        raise ValueError(f"fp_to_int: negative value {fp_str_or_none!r}")
    return int(val)


def dollars_to_cents(dollars_str_or_none) -> Optional[int]:
    """Convert fixed-point dollars string to integer cents using banker's rounding.

    "0.5600" -> 56, "0.5650" -> 56 (half-even), "0.5750" -> 58, "1.0000" -> 100.
    Uses Decimal for precision (avoids float rounding artifacts). None/"" -> None.
    """
    if dollars_str_or_none is None or dollars_str_or_none == "":
        return None
    try:
        return int(
            Decimal(str(dollars_str_or_none))
            .scaleb(2)
            .quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
        )
    except Exception:
        return None


def extract_order_counts(order_dict: dict) -> dict:
    """Extract contract count fields from an order or fill response dict.

    Returns {"total": int|None, "filled": int|None, "remaining": int|None}.
    Prefers *_fp string fields; falls back to legacy int fields for transition safety.
    Missing field -> None. Caller decides what to do with None - no defaults here.
    """
    def _get(fp_key: str, legacy_key: str) -> Optional[int]:
        fp_val = order_dict.get(fp_key)
        if fp_val is not None:
            return fp_to_int(fp_val)
        leg = order_dict.get(legacy_key)
        if leg is not None:
            try:
                return int(leg)
            except (TypeError, ValueError):
                return None
        return None

    total = _get("contracts_count_fp", "contracts_count")
    if total is None:
        total = _get("count_fp", "count")
    return {
        "total":     total,
        "filled":    _get("filled_count_fp",    "filled_count"),
        "remaining": _get("remaining_count_fp", "remaining_count"),
    }


def extract_fill_price_cents(fill_or_order: dict, side: str) -> Optional[int]:
    """Extract fill/order price in cents for the given side.

    Prefers yes_price_dollars / no_price_dollars (new); falls back to legacy
    yes_price / no_price integer cents. Returns None if field is missing.
    side must be "yes" or "no".
    """
    if side == "yes":
        dollars_val = fill_or_order.get("yes_price_dollars")
        if dollars_val is not None:
            return dollars_to_cents(dollars_val)
        legacy = fill_or_order.get("yes_price")
        if legacy is not None:
            try:
                return int(legacy)
            except (TypeError, ValueError):
                return None
    else:
        dollars_val = fill_or_order.get("no_price_dollars")
        if dollars_val is not None:
            return dollars_to_cents(dollars_val)
        legacy = fill_or_order.get("no_price")
        if legacy is not None:
            try:
                return int(legacy)
            except (TypeError, ValueError):
                return None
    return None


def extract_fee_cents(fill: dict) -> Optional[int]:
    """Extract fill fee in cents.

    Jan 29 2026: fee_cost changed from int cents to fixed-point dollars string.
    Falls back to legacy int cents if field is numeric.
    """
    fee_val = fill.get("fee_cost")
    if fee_val is None:
        return None
    if isinstance(fee_val, str):
        return dollars_to_cents(fee_val)
    try:
        return int(fee_val)
    except (TypeError, ValueError):
        return None
