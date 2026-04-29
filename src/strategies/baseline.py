"""
Brownian-bridge fair-value baseline for binary up/down contracts.

For current price S, strike K, fraction of window remaining tau,
and 1-minute volatility sigma:

    p_baseline = P(asset closes above strike | currently at S, tau remains)
               = Phi( ln(S/K) / (sigma * sqrt(tau * T)) )

where Phi is the standard normal CDF and T is total window length in minutes.

This gives the "physics-based" probability absent any edge signal.
Strategies produce adjustments to this baseline, not absolute probabilities.
"""

from __future__ import annotations

import math

from scipy.stats import norm


WINDOW_TOTAL_MINUTES = 15.0


def brownian_bridge_prob_above(
    current_price: float,
    strike: float,
    seconds_left: float,
    vol_1min: float,
) -> float:
    """
    Returns probability asset closes ABOVE strike at window expiry.

    Args:
        current_price: current asset price (same units as strike)
        strike: contract strike price
        seconds_left: seconds until window close (>=0)
        vol_1min: 1-minute log-return volatility (e.g., 0.002 = 0.2%)

    Returns:
        P(close > strike), clamped to [0.001, 0.999]

    Edge cases:
        - seconds_left <= 0: returns 1.0 if currently above strike, else 0.0
        - vol_1min <= 0: returns 1.0 if above, 0.0 if below (no uncertainty)
        - strike == current_price: returns 0.5
    """
    # Edge case: window closed
    if seconds_left <= 0:
        if current_price > strike:
            return 0.999
        elif current_price < strike:
            return 0.001
        return 0.5

    # Edge case: zero volatility
    if vol_1min <= 0:
        if current_price > strike:
            return 0.999
        elif current_price < strike:
            return 0.001
        return 0.5

    # Edge case: at-the-money
    if current_price == strike:
        return 0.5

    minutes_left = seconds_left / 60.0
    log_distance = math.log(current_price / strike)
    standard_deviations_of_remaining_motion = vol_1min * math.sqrt(minutes_left)

    z = log_distance / standard_deviations_of_remaining_motion
    prob_above = norm.cdf(z)

    # Clamp for numerical safety
    return max(0.001, min(0.999, prob_above))
