"""
Rolling correlation between two time series of prices.

Used by XRPStrategy to detect when XRP has decoupled from BTC. When
rolling 60-min correlation drops below a threshold (e.g. 0.3), XRP is
in idiosyncratic mode and the BTC signal should be downweighted or zeroed.

Standard Pearson correlation on log returns.
"""

from __future__ import annotations
import math
from typing import Optional

from strategies.original.signals.rolling_beta import log_returns_from_prices


def rolling_correlation(
    asset_prices: list,
    btc_prices: list,
    lookback_minutes: int = 60,
) -> Optional[float]:
    """
    Compute Pearson correlation of asset 1-min log returns vs BTC 1-min
    log returns over the most recent lookback_minutes of common timestamps.

    Returns:
        correlation in [-1, 1] or None if insufficient data.
    """
    if not asset_prices or not btc_prices:
        return None

    a_by_min = {}
    for ts, p in asset_prices:
        bucket = int(ts // 60)
        a_by_min[bucket] = p
    b_by_min = {}
    for ts, p in btc_prices:
        bucket = int(ts // 60)
        b_by_min[bucket] = p

    common = sorted(set(a_by_min) & set(b_by_min))
    if len(common) < 30:
        return None

    window_buckets = common[-lookback_minutes:]
    if len(window_buckets) < 30:
        return None

    aligned_a = [(b, a_by_min[b]) for b in window_buckets]
    aligned_b = [(b, b_by_min[b]) for b in window_buckets]

    a_returns = log_returns_from_prices(aligned_a)
    b_returns = log_returns_from_prices(aligned_b)

    n = min(len(a_returns), len(b_returns))
    if n < 25:
        return None
    a_returns = a_returns[:n]
    b_returns = b_returns[:n]

    mean_a = sum(a_returns) / n
    mean_b = sum(b_returns) / n

    cov = 0.0
    var_a = 0.0
    var_b = 0.0
    for ar, br in zip(a_returns, b_returns):
        da = ar - mean_a
        db = br - mean_b
        cov += da * db
        var_a += da * da
        var_b += db * db

    if var_a <= 0 or var_b <= 0:
        return None

    return cov / math.sqrt(var_a * var_b)


def btc_signal_weight_from_correlation(
    correlation: Optional[float],
    decoupling_threshold: float = 0.3,
    max_weight: float = 0.30,
) -> float:
    """
    Translate correlation into a BTC-signal weight for XRP strategy.

    Returns weight in [0, max_weight].
    - correlation None: max_weight * 0.5 (conservative middle ground)
    - correlation <= decoupling_threshold: 0 (XRP is decoupled)
    - correlation >= 0.7: max_weight
    - in-between: linear ramp
    """
    if correlation is None:
        return max_weight * 0.5

    if correlation <= decoupling_threshold:
        return 0.0

    if correlation >= 0.7:
        return max_weight

    ramp = (correlation - decoupling_threshold) / (0.7 - decoupling_threshold)
    return max_weight * ramp

