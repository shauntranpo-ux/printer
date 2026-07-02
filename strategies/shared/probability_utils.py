from __future__ import annotations
import numpy as np
from scipy.stats import norm


def drift_vol_to_prob(mu: float, sigma: float, dt: float) -> float:
    """
    P(S_T > S_0) under Brownian-with-drift over horizon dt.

    Args:
        mu:    annualized log-drift (log-return per year)
        sigma: annualized log-volatility
        dt:    horizon in years (e.g. 15/(365*24*60) for 15 minutes)

    Returns Φ((mu * dt) / (sigma * sqrtdt)) per Black-Scholes probability.
    Returns 0.5 when sigma or dt is non-positive (degenerate input guard).
    """
    if sigma <= 0.0 or dt <= 0.0:
        return 0.5
    return float(norm.cdf((mu * dt) / (sigma * np.sqrt(dt))))


def prob_to_contract_price(p: float) -> float:
    """
    Convert probability to Kalshi contract price in cents.
    Kalshi contract price IS the probability: a YES at 70c pays $1 with P=0.70.
    Named wrapper to make the conversion point explicit in call sites.
    """
    return p * 100.0


def contract_price_change_from_prob_change(dp: float) -> float:
    """
    Convert a change in probability (dimensionless) to a contract price change (cents).
    The mapping is 1:1: dp=0.05 -> 5 cents. Named for clarity at call sites.
    """
    return dp * 100.0
