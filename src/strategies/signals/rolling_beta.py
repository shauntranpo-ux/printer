"""
Rolling beta of asset returns vs BTC returns.

Beta is the OLS regression slope of asset returns regressed on BTC returns
over a lookback window. For crypto majors, this is typically 0.8-2.0 and
is the primary driver of co-movement.

We compute beta on 1-min log returns over a rolling 30-day window, refitted
daily. At feature-build time, the strategy reads the cached beta value
rather than recomputing.
"""

from __future__ import annotations
import math
from typing import Optional


def compute_beta_from_returns(asset_returns: list, btc_returns: list) -> Optional[float]:
    """
    OLS beta: slope of asset_returns regressed on btc_returns.

    Args:
        asset_returns: list of log returns for the asset (same length as btc_returns)
        btc_returns: list of log returns for BTC

    Returns:
        beta (float) or None if inputs are insufficient or degenerate
    """
    if len(asset_returns) != len(btc_returns):
        return None
    n = len(asset_returns)
    if n < 100:  # require at least 100 1-min return pairs
        return None

    mean_a = sum(asset_returns) / n
    mean_b = sum(btc_returns) / n

    cov = 0.0
    var_b = 0.0
    for a, b in zip(asset_returns, btc_returns):
        da = a - mean_a
        db = b - mean_b
        cov += da * db
        var_b += db * db

    if var_b <= 0:
        return None
    return cov / var_b


def log_returns_from_prices(prices: list) -> list:
    """Given a list of (ts, price) tuples, return list of log returns."""
    returns = []
    prev = None
    for ts, p in prices:
        if prev is not None and prev > 0 and p > 0:
            returns.append(math.log(p / prev))
        prev = p
    return returns
