"""
Per-strike moneyness feature computation for Strategy C.

Moneyness buckets are determined by log(K/S), with boundaries from config.
Bucket assignment feeds the per-bucket calibration layer in model.py.
"""
from __future__ import annotations
import math


def compute_moneyness_features(
    current_price: float,
    strike_price: float,
    sigma_hat: float,
    time_to_expiry_seconds: float,
    config: dict,
) -> dict:
    """
    Compute moneyness features for a single (spot, strike) pair.

    Args:
        current_price: current spot price S_t
        strike_price: strike price K
        sigma_hat: annualized volatility estimate (e.g. 0.43 for BTC)
        time_to_expiry_seconds: seconds until contract expiry; used to scale σ̂
        config: asset config dict (moneyness section required for bucket boundaries)

    Returns:
        dict with keys:
            log_moneyness          float  — ln(K / S)
            moneyness_bucket       str    — deep_itm | itm | atm | otm | deep_otm
            distance_to_spot_bps   float  — (K/S − 1) × 10_000
            distance_to_spot_sigma float  — |ln(K/S)| / (σ̂ · √(T−t)) in annualized units
    """
    log_m = math.log(strike_price / current_price)
    dist_bps = (strike_price / current_price - 1.0) * 10_000.0

    t_years = time_to_expiry_seconds / (365.25 * 24.0 * 3600.0)
    sigma_expiry = sigma_hat * math.sqrt(t_years) if t_years > 0 else 0.0
    dist_sigma = abs(log_m) / sigma_expiry if sigma_expiry > 0 else 0.0

    mono_cfg = config.get("moneyness", {})
    deep_itm_cut: float = float(mono_cfg.get("deep_itm_log_moneyness_cutoff", -0.02))
    itm_cut: float      = float(mono_cfg.get("itm_log_moneyness_cutoff",      -0.005))
    atm_cut: float      = float(mono_cfg.get("atm_log_moneyness_cutoff",       0.005))
    otm_cut: float      = float(mono_cfg.get("otm_log_moneyness_cutoff",       0.02))

    if log_m <= deep_itm_cut:
        bucket = "deep_itm"
    elif log_m <= itm_cut:
        bucket = "itm"
    elif log_m <= atm_cut:
        bucket = "atm"
    elif log_m <= otm_cut:
        bucket = "otm"
    else:
        bucket = "deep_otm"

    return {
        "log_moneyness": log_m,
        "moneyness_bucket": bucket,
        "distance_to_spot_bps": dist_bps,
        "distance_to_spot_sigma": dist_sigma,
    }
