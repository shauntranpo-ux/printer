"""Binary option price via Black-Scholes (digital call)."""
from __future__ import annotations

import math


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def compute_bs_p_yes(
    current_price: float,
    strike: float,
    realized_vol_1min: float,
    seconds_left: float,
) -> float | None:
    """
    P(price > strike at expiry) via log-normal model.

    Returns None when inputs are invalid or vol is zero.
    """
    if current_price <= 0 or strike <= 0 or seconds_left <= 0:
        return None
    mins = seconds_left / 60.0
    sigma = realized_vol_1min * math.sqrt(max(mins, 1e-6))
    if sigma < 1e-8:
        return None
    d = math.log(current_price / strike) / sigma
    return _norm_cdf(d)
