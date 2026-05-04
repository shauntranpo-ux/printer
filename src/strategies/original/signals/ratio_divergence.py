"""
ETH/BTC ratio divergence.

When ETH/BTC ratio deviates from its recent (e.g. 4-hour) rolling mean by
more than 1 sigma without fresh news, the ratio tends to revert.
"""

from __future__ import annotations
import math
from typing import Optional


def ratio_z_score(
    asset_prices: list,
    btc_prices: list,
    lookback_minutes: int = 240,
) -> Optional[float]:
    """
    Z-score of current asset/btc ratio vs its rolling mean.

    Args:
        asset_prices: list of (ts, price) tuples for the asset
        btc_prices: list of (ts, price) tuples for BTC
        lookback_minutes: window for mean/std computation

    Returns:
        z-score (float) or None if data insufficient.
        Positive z: asset overpriced vs BTC (p_yes down)
        Negative z: asset underpriced vs BTC (p_yes up)
    """
    if not asset_prices or not btc_prices:
        return None

    asset_by_min = {}
    for ts, p in asset_prices:
        bucket = int(ts // 60)
        asset_by_min[bucket] = p

    btc_by_min = {}
    for ts, p in btc_prices:
        bucket = int(ts // 60)
        btc_by_min[bucket] = p

    common = set(asset_by_min) & set(btc_by_min)
    if len(common) < 30:
        return None

    sorted_buckets = sorted(common)
    window = sorted_buckets[-lookback_minutes:]
    if len(window) < 30:
        return None

    ratios = []
    for b in window:
        a = asset_by_min[b]
        bt = btc_by_min[b]
        if a > 0 and bt > 0:
            ratios.append(a / bt)
    if len(ratios) < 30:
        return None

    mean = sum(ratios) / len(ratios)
    var = sum((r - mean) ** 2 for r in ratios) / (len(ratios) - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    if std == 0:
        return None

    current = ratios[-1]
    return (current - mean) / std

