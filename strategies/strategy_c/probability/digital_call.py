"""
Digital (binary) call probability evaluator under geometric Brownian motion.

ALL TIME UNITS IN THIS MODULE ARE SECONDS.

The integrated_variance input is σ²·(T−t), pre-integrated over the horizon.
This decouples the probability evaluator from the term-structure integrator:
the caller is responsible for supplying the correct integrated variance, which
may come from a flat-vol assumption or from the vol_term_structure integrator.

Risk-free rate defaults to 0.0 for all crypto calculations.
"""
from __future__ import annotations
import math
from scipy.stats import norm


def flat_vol_to_integrated_variance(sigma_per_second: float, dt_seconds: float) -> float:
    """Return σ²_per_second × dt_seconds for a flat-vol horizon."""
    return sigma_per_second ** 2 * dt_seconds


def binary_call_probability(
    current_price: float,
    strike_price: float,
    integrated_variance: float,
    time_to_expiry_seconds: float,
    risk_free_rate: float = 0.0,
) -> float:
    """
    P(S_T > K) under GBM with risk-neutral drift = risk_free_rate.

    Formula:
        d₂ = [ln(S_t / K) + r·(T−t) − integrated_variance/2] / √(integrated_variance)
        P  = N(d₂)

    where integrated_variance = σ²·(T−t).

    Args:
        current_price:           S_t > 0; current spot price
        strike_price:            K > 0; contract strike
        integrated_variance:     σ²·(T−t) > 0; pre-integrated variance over [t, T]
        time_to_expiry_seconds:  T−t in seconds; ≤ 0 returns deterministic outcome
        risk_free_rate:          r; defaults to 0.0 for crypto

    Returns:
        P(S_T > K) ∈ [0, 1].  Returns 0.5 as a neutral sentinel when inputs are
        non-positive (degenerate inputs should be filtered upstream).
    """
    if time_to_expiry_seconds <= 0:
        return 1.0 if current_price > strike_price else 0.0

    if current_price <= 0 or strike_price <= 0 or integrated_variance <= 0:
        return 0.5

    log_fwd = math.log(current_price / strike_price) + risk_free_rate * time_to_expiry_seconds
    d2 = (log_fwd - integrated_variance / 2.0) / math.sqrt(integrated_variance)
    return float(norm.cdf(d2))
