"""
Idiosyncratic mode detector for DOGE.

When DOGE is moving in a way that diverges meaningfully from BTC-implied
behavior, it's in idiosyncratic mode — news, meme flow, or retail sentiment.
The bot has no edge in predicting direction during these windows. Skip.

Detector:
  - Compute 15-min return divergence: DOGE_ret - (beta * BTC_ret)
  - Build distribution of historical divergences from non-overlapping
    15-min windows (excluding the most recent 15 data points)
  - Compare the current 15-min window's divergence to the distribution
  - If > sigma_threshold std deviations, idiosyncratic mode

Unlike XRP's news-mode (which switches to momentum), DOGE's idiosyncratic
detector skips entirely because we lack directional edge in these windows.
"""

from __future__ import annotations
import math
from typing import Optional

from strategies.signals.rolling_beta import log_returns_from_prices


def detect_idiosyncratic_mode(
    doge_prices_60m: list,
    btc_prices_60m: list,
    beta: float,
    sigma_threshold: float = 2.5,
) -> tuple[bool, dict]:
    """
    Returns (is_idiosyncratic, diagnostics).

    is_idiosyncratic = True when the most-recent 15-min DOGE return diverges
    from beta * (BTC 15-min return) by more than sigma_threshold standard
    deviations of historical 15-min divergences.
    """
    signals: dict = {
        "idiosyncratic": False,
        "reason": "",
        "divergence": None,
        "divergence_sigma": None,
    }

    if len(doge_prices_60m) < 20 or len(btc_prices_60m) < 20:
        signals["reason"] = "insufficient_data"
        return False, signals

    d_by_min = {int(ts // 60): p for ts, p in doge_prices_60m}
    b_by_min = {int(ts // 60): p for ts, p in btc_prices_60m}
    common = sorted(set(d_by_min) & set(b_by_min))
    if len(common) < 20:
        signals["reason"] = "insufficient_aligned_data"
        return False, signals

    # Current window: last 15 common buckets
    if len(common) < 16:
        signals["reason"] = "insufficient_data_for_current_window"
        return False, signals

    curr_start = common[-16]
    curr_end = common[-1]
    if not (d_by_min.get(curr_start) and d_by_min.get(curr_end)
            and b_by_min.get(curr_start) and b_by_min.get(curr_end)
            and d_by_min[curr_start] > 0 and b_by_min[curr_start] > 0):
        signals["reason"] = "invalid_current_window"
        return False, signals

    d_ret_curr = math.log(d_by_min[curr_end] / d_by_min[curr_start])
    b_ret_curr = math.log(b_by_min[curr_end] / b_by_min[curr_start])
    current_divergence = d_ret_curr - (beta * b_ret_curr)

    # Historical windows: non-overlapping 15-min blocks from data BEFORE the last 15 points
    historical_common = common[:-15]
    divergences = []
    i = 0
    while i + 15 < len(historical_common):
        start = historical_common[i]
        end = historical_common[i + 15]
        if (start in d_by_min and end in d_by_min
                and start in b_by_min and end in b_by_min
                and d_by_min[start] > 0 and b_by_min[start] > 0):
            d_ret = math.log(d_by_min[end] / d_by_min[start])
            b_ret = math.log(b_by_min[end] / b_by_min[start])
            divergences.append(d_ret - (beta * b_ret))
        i += 15

    if len(divergences) < 2:
        signals["reason"] = "insufficient_divergence_samples"
        return False, signals

    mean = sum(divergences) / len(divergences)
    var = sum((d - mean) ** 2 for d in divergences) / (len(divergences) - 1)
    std = math.sqrt(var) if var > 0 else 0

    signals["divergence"] = current_divergence

    if std == 0:
        # Historical tracking was perfectly uniform — any large absolute
        # deviation from the historical mean is idiosyncratic.
        abs_deviation = abs(current_divergence - mean)
        if abs_deviation > 0.02:
            signals["divergence_sigma"] = float("inf")
            signals["idiosyncratic"] = True
            signals["reason"] = f"zero_variance_large_deviation: {abs_deviation:.4f}"
            return True, signals
        signals["reason"] = "zero_variance_small_deviation"
        return False, signals

    sigma_ratio = abs(current_divergence - mean) / std
    signals["divergence_sigma"] = sigma_ratio

    if sigma_ratio > sigma_threshold:
        signals["idiosyncratic"] = True
        signals["reason"] = f"sigma={sigma_ratio:.2f} > {sigma_threshold}"
        return True, signals

    signals["reason"] = f"normal: sigma={sigma_ratio:.2f}"
    return False, signals
