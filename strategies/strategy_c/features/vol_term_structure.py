"""
Volatility term-structure integrator for Strategy C.

Breaks [t, T] into sub-intervals, applies regime-conditional multipliers to the
HAR forecast at each sub-interval, and sums to get total integrated variance
suitable for binary_call_probability()'s integrated_variance argument.
"""
from __future__ import annotations
import pandas as pd


def integrate_forecasted_variance(
    har_forecast_fn,
    timestamp_now: pd.Timestamp,
    timestamp_expiry: pd.Timestamp,
    regime_lookup_fn,
    config: dict,
) -> float:
    """
    Integrate forecasted variance over [timestamp_now, timestamp_expiry].

    Sub-intervals default to 15 minutes (configurable via
    config.vol_term_structure.sub_interval_minutes).

    har_forecast_fn(t) must return the forecasted variance for one *full*
    sub-interval starting at t.  The last sub-interval may be shorter; the
    returned value is scaled by the fraction of the interval covered.

    Regime multipliers (config.vol_term_structure.regime_multipliers) scale
    the per-sub-interval variance.  null entries in config default to 1.0.

    Args:
        har_forecast_fn:   callable(pd.Timestamp) -> float
        timestamp_now:     integration start (UTC)
        timestamp_expiry:  integration end (UTC)
        regime_lookup_fn:  callable(pd.Timestamp) -> str
        config:            asset config dict

    Returns:
        Total integrated variance >= 0.  Returns 0.0 when timestamp_now >= timestamp_expiry.
    """
    if timestamp_now >= timestamp_expiry:
        return 0.0

    vts_cfg = config.get("vol_term_structure", {}) or {}
    sub_min: float = float(vts_cfg.get("sub_interval_minutes", 15))
    regime_mults: dict = vts_cfg.get("regime_multipliers", {}) or {}

    sub_td = pd.Timedelta(minutes=sub_min)
    full_seconds = sub_td.total_seconds()

    total_variance = 0.0
    t = timestamp_now
    while t < timestamp_expiry:
        end = min(t + sub_td, timestamp_expiry)
        fraction = (end - t).total_seconds() / full_seconds

        point_variance = har_forecast_fn(t)
        regime = regime_lookup_fn(t)
        raw_mult = regime_mults.get(regime)
        multiplier = float(raw_mult) if raw_mult is not None else 1.0

        total_variance += point_variance * multiplier * fraction
        t = end

    return total_variance
