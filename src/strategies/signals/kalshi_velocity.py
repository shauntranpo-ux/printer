"""
Kalshi contract velocity.

Measures how fast the YES contract price has been moving over the last
N samples. Fast movement suggests informed flow: professional market
makers or traders with better models are aggressively repricing.
"""

from __future__ import annotations
from typing import Literal, Optional


def contract_velocity(
    kalshi_price_history: list,
    lookback_samples: int = 30,
    threshold_pct: float = 0.02,
) -> Literal["rising", "falling", "flat"]:
    """
    Args:
        kalshi_price_history: list of (ts, yes_price_cents) tuples
        lookback_samples: how many recent samples to consider
        threshold_pct: minimum fractional change to call rising/falling

    Returns:
        'rising', 'falling', or 'flat'
    """
    if len(kalshi_price_history) < max(10, lookback_samples // 3):
        return "flat"

    recent = list(kalshi_price_history)[-lookback_samples:]
    first = recent[0][1]
    last = recent[-1][1]

    if first <= 0:
        return "flat"

    delta = (last - first) / first
    if delta > threshold_pct:
        return "rising"
    if delta < -threshold_pct:
        return "falling"
    return "flat"


def velocity_adjustment_for_side(
    velocity: str,
    side: Literal["yes", "no"],
    magnitude: float = 0.02,
) -> float:
    """
    Translate velocity into a probability adjustment for the given side.

    Rising contract + YES = favorable = +magnitude
    Rising contract + NO  = unfavorable = -magnitude
    Falling contract + NO = favorable = +magnitude
    Falling contract + YES = unfavorable = -magnitude
    Flat = 0
    """
    if velocity == "rising":
        return +magnitude if side == "yes" else -magnitude
    if velocity == "falling":
        return +magnitude if side == "no" else -magnitude
    return 0.0
