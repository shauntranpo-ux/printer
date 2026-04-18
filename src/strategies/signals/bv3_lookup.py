"""
BV3 empirical lookup as a standalone signal.

Wraps bot._win_prob_for_asset() so it can be used as a SECONDARY signal
in the new BTC strategy without depending on the full legacy brain pipeline.

The raw BV3 output is the empirical probability that the asset stays on
its current side of strike, conditional on (distance_from_strike,
minutes_remaining). We convert this to a p_yes by considering which side
of strike the asset is currently on.

Historical context:
- BV3 was the centerpiece of the legacy strategy
- It has documented data leakage (see BV3_DATA_LEAKAGE_WARNING.md)
- Section 8 will regenerate with proper train/test split
- Until then, we use it at low weight (20%) as a sanity check
"""

from __future__ import annotations
from typing import Optional


def bv3_p_yes(
    asset: str,
    current_price: float,
    strike: float,
    seconds_left: float,
) -> Optional[float]:
    """
    Return BV3-based probability that YES wins at close.

    Returns None if BV3 lookup fails (e.g. bot module not importable,
    table not loaded, or distance outside table range).
    """
    try:
        import bot
    except ImportError:
        return None

    if strike <= 0:
        return None

    abs_pct = abs(current_price - strike) / strike
    mins_left = seconds_left / 60.0
    above = current_price > strike

    try:
        p_same_side = bot._win_prob_for_asset(asset, abs_pct, mins_left)
    except Exception:
        return None

    if p_same_side is None:
        return None

    return p_same_side if above else (1.0 - p_same_side)
