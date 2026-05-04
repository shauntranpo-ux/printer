"""Shared BTC context helpers for non-BTC strategies."""

from __future__ import annotations
from typing import Optional


def three_min_return(btc_prices_60m) -> Optional[float]:
    """
    BTC 3-minute return derived from the injected price deque.
    Uses deque timestamps as reference so this works correctly in backtesting.
    Returns None when the deque is empty or has no price 3 minutes back.
    """
    prices = list(btc_prices_60m)
    if len(prices) < 2:
        return None
    current = prices[-1][1]
    cutoff = prices[-1][0] - 180
    oldest = None
    for ts, p in prices:
        if ts >= cutoff:
            oldest = p
            break
    if oldest is None or oldest <= 0:
        return None
    return (current - oldest) / oldest

