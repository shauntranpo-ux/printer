"""
Volume-spike event detector.

XRP has a bimodal behavior: calm drift most of the time, occasional news
spikes (SEC, Ripple, exchanges). When volume spikes AND return spikes
simultaneously, the asset is in news-driven mode and behavior diverges
from normal statistical patterns.

Detector:
  - 1-minute volume > 3x trailing 60-min median volume
  - AND 1-minute absolute return > 95th percentile of trailing 60-min
    absolute returns
  - THEN classify as 'news_mode', return direction of the spike

Used to SWITCH strategy modes, not just adjust probability.
In news mode, lean momentum (news rarely fully reverses in 15 min).
"""

from __future__ import annotations
from typing import Optional


def detect_volume_spike(
    price_volume_history: list,
    lookback_minutes: int = 60,
    volume_multiple: float = 3.0,
    return_percentile: float = 0.95,
) -> tuple[bool, str, Optional[float]]:
    """
    Args:
        price_volume_history: list of (ts, price, volume) tuples.
        lookback_minutes: window for median volume + return distribution
        volume_multiple: spike threshold
        return_percentile: percentile of recent abs returns

    Returns:
        (is_spike, direction, recent_return_pct)
        direction is "up", "down", or "none"
    """
    if not price_volume_history or len(price_volume_history) < lookback_minutes:
        return False, "none", None

    by_min: dict = {}
    for ts, price, volume in price_volume_history:
        bucket = int(ts // 60)
        if bucket not in by_min:
            by_min[bucket] = {"last_price": price, "total_volume": 0.0, "first_price": price}
        by_min[bucket]["last_price"] = price
        by_min[bucket]["total_volume"] += volume

    sorted_buckets = sorted(by_min.keys())
    if len(sorted_buckets) < lookback_minutes:
        return False, "none", None

    returns = []
    for i in range(1, len(sorted_buckets)):
        prev_p = by_min[sorted_buckets[i - 1]]["last_price"]
        curr_p = by_min[sorted_buckets[i]]["last_price"]
        if prev_p > 0 and curr_p > 0:
            returns.append((curr_p - prev_p) / prev_p)

    if len(returns) < 30:
        return False, "none", None

    current_bucket = sorted_buckets[-1]
    current_volume = by_min[current_bucket]["total_volume"]
    recent_return = returns[-1] if returns else 0.0

    volumes = [by_min[b]["total_volume"] for b in sorted_buckets[-lookback_minutes:-1]]
    if not volumes:
        return False, "none", None
    median_vol = sorted(volumes)[len(volumes) // 2]

    if median_vol <= 0:
        return False, "none", None

    volume_spike = current_volume > (volume_multiple * median_vol)
    if not volume_spike:
        return False, "none", recent_return

    abs_returns = [abs(r) for r in returns[-lookback_minutes:-1]]
    if not abs_returns:
        return False, "none", recent_return
    sorted_abs = sorted(abs_returns)
    threshold_idx = int(return_percentile * len(sorted_abs))
    threshold = sorted_abs[min(threshold_idx, len(sorted_abs) - 1)]

    return_spike = abs(recent_return) >= threshold
    if not return_spike:
        return False, "none", recent_return

    direction = "up" if recent_return > 0 else "down"
    return True, direction, recent_return
