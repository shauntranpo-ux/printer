"""
Exhaustion fade signal for final-minutes window.

When a large-cap crypto has made a >2 sigma move in the last minute AND
there are fewer than 120 seconds remaining, the directional flow tends to
reverse as market makers absorb late momentum. Small mean-reversion effect.

Only active if:
  - seconds_left < activation_seconds (default 120)
  - |1-min return| > sigma_threshold * realized_vol_1min (default 2 sigma)

Direction is opposite to the extreme move.
"""

from __future__ import annotations
import time
from typing import Optional


def exhaustion_fade_adjustment(
    prices_1m: list,
    realized_vol_1min: Optional[float],
    seconds_left: float,
    adj_magnitude: float = 0.03,
    activation_seconds: float = 120.0,
    sigma_threshold: float = 2.0,
) -> tuple[float, dict]:
    """
    Returns (adjustment_to_p_yes, diagnostic_signals).

    Positive adjustment = nudge p_yes up (price dumped, expect rebound).
    Negative adjustment = nudge p_yes down (price ripped, expect fade).
    """
    signals: dict = {
        "exhaustion_active": False,
        "exhaustion_reason": "inactive",
        "recent_1m_return": None,
        "sigma_ratio": None,
    }

    if seconds_left >= activation_seconds:
        signals["exhaustion_reason"] = (
            f"outside_window ({seconds_left:.0f}s >= {activation_seconds:.0f}s)"
        )
        return 0.0, signals

    if realized_vol_1min is None or realized_vol_1min <= 0:
        signals["exhaustion_reason"] = "no_vol"
        return 0.0, signals

    if len(prices_1m) < 2:
        signals["exhaustion_reason"] = "insufficient_data"
        return 0.0, signals

    prices = list(prices_1m)
    now = time.time()
    cutoff = now - 60.0
    oldest = None
    for ts, p in prices:
        if ts >= cutoff:
            oldest = p
            break
    if oldest is None or oldest <= 0:
        signals["exhaustion_reason"] = "no_prior_price"
        return 0.0, signals

    current = prices[-1][1]
    recent_return = (current - oldest) / oldest
    signals["recent_1m_return"] = recent_return

    sigma_ratio = abs(recent_return) / realized_vol_1min
    signals["sigma_ratio"] = sigma_ratio

    if sigma_ratio < sigma_threshold:
        signals["exhaustion_reason"] = (
            f"sigma_ratio_{sigma_ratio:.2f} < {sigma_threshold}"
        )
        return 0.0, signals

    signals["exhaustion_active"] = True
    signals["exhaustion_reason"] = f"active: sigma_ratio={sigma_ratio:.2f}"
    if recent_return > 0:
        return -adj_magnitude, signals  # ripped up -> fade -> p_yes down
    else:
        return +adj_magnitude, signals  # dumped -> rebound -> p_yes up
