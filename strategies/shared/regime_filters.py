from __future__ import annotations
import pandas as pd

_REGIME_HOURS: list[tuple[str, int, int]] = [
    ("asia_deep_night",  0,  4),
    ("asia_active",      4,  8),
    ("eu_open",          8, 13),
    ("eu_us_overlap",   13, 16),
    ("us_afternoon",    16, 20),
    ("us_late",         20, 24),
]


def get_current_regime(timestamp: pd.Timestamp, config: dict) -> str:
    """Return the regime string for a UTC timestamp. config is unused (reserved for future extension)."""
    hour = timestamp.hour
    for name, start, end in _REGIME_HOURS:
        if start <= hour < end:
            return name
    return "us_late"


def get_regime_threshold(regime: str, timestamp: pd.Timestamp, config: dict) -> float:
    """
    Look up the edge-above-fee threshold for the given regime.
    Returns weekend threshold if timestamp falls on Saturday (5) or Sunday (6).
    Returns 0.02 as a safe default when config entry is null/missing.
    """
    thresholds = config.get("thresholds", {}).get("edge_above_fee", {})
    if timestamp.dayofweek >= 5:
        val = thresholds.get("weekend")
        if val is not None:
            return float(val)
    val = thresholds.get(regime)
    return float(val) if val is not None else 0.02


def get_fee_adjusted_threshold(
    regime: str,
    timestamp: pd.Timestamp,
    config: dict,
    fees_config: dict,
) -> float:
    """
    Minimum edge required to trade after fees.

    min_edge = taker_fee + safety_margin + regime_extra

    Note: fees.yaml stores a conservative flat-rate approximation (0.03).
    The actual Kalshi taker fee is 0.07·C·p·(1-p), which peaks at ~3.5% at p=0.5.
    The execution layer uses the exact formula; this threshold uses the flat approx.
    """
    taker_fee = float(fees_config["kalshi"]["taker_fee_rate"])
    safety_margin = float(fees_config["safety_margin"])
    regime_extra = get_regime_threshold(regime, timestamp, config)
    return taker_fee + safety_margin + regime_extra
