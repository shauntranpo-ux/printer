"""
BTCHourlyStrategy V2 -- evidence-based strategy for BTC 60-min Kalshi binaries.

Core insight: Edge arises when REALIZED vol is significantly lower than recent
history (quiet regime). In quiet regimes, the Brownian bridge probability is
reliable and continuation bets have genuine edge. In volatile regimes, skip.

Signal pipeline:
1. Vol Z-score: if current RV << recent 48h mean, quiet regime -> continuation more reliable.
   If RV >> recent mean, volatile period -> reduce or skip.
2. Distance Z-score: abs(price - strike) / current_rv. High Z (>2.5) means
   BTC is many standard deviations from strike -> very safe continuation.
3. Session filter: US hours (13-21 UTC) -> apply signals.
                   Asian dead hours (01-07 UTC) -> skip entirely.

Explicitly NOT used: variance ratio, 30-min momentum -- those were anti-predictive in V1.
"""

from __future__ import annotations
import datetime
import math
from typing import Optional

from strategies.base import BaseStrategy
from strategies.features import MarketFeatures
from strategies.skip_layer import SkipConfig
from strategies.calibration import AssetCalibrator
from strategies.signals.taper import magnitude_taper
from strategies.signals.rolling_beta import log_returns_from_prices


VOL_ZSCORE_QUIET_THRESHOLD   = -0.5   # current_rv < mean - 0.5*std -> quiet
VOL_ZSCORE_NOISY_THRESHOLD   =  1.0   # current_rv > mean + 1.0*std -> volatile
QUIET_CONTINUATION_BOOST     =  0.06  # boost p_continuation in quiet regime
NOISY_CONTINUATION_REDUCE    =  0.04  # reduce p_continuation in volatile regime

DISTANCE_ZSCORE_HIGH         =  2.5   # abs_pct / rv > 2.5 -> very safe continuation
DISTANCE_ZSCORE_BOOST        =  0.08  # boost when far from strike

ASIAN_SESSION_SKIP_START     =  1     # UTC hour: skip from 01:00
ASIAN_SESSION_SKIP_END       =  7     # UTC hour: skip before 07:00
US_SESSION_START             = 13
US_SESSION_END               = 21


def _compute_vol_zscore(prices_60m: list, current_rv: float) -> Optional[float]:
    """
    Z-score of current_rv relative to rolling 48h vol distribution.
    Uses per-hour realized vol computed from the 60m price history.
    Returns z = (current_rv - mean_rv) / std_rv or None if insufficient data.
    """
    # We have up to 60 minutes of 1-min prices in prices_60m.
    # To estimate 48h history we need the full prices array.
    # Compute hourly-binned RV from what we have (typically 60 minutes).
    # With only 60 minutes of data, estimate using sub-windows.
    if len(prices_60m) < 20:
        return None

    prices = [p for _, p in prices_60m]
    returns = []
    for i in range(1, len(prices)):
        if prices[i-1] > 0 and prices[i] > 0:
            returns.append(math.log(prices[i] / prices[i-1]))

    if len(returns) < 15:
        return None

    # Compute rolling 10-min RV windows to get a distribution
    window_rvs = []
    w = 10
    for start in range(0, len(returns) - w, w):
        chunk = returns[start:start+w]
        if len(chunk) >= 5:
            mean_c = sum(chunk) / len(chunk)
            var_c = sum((r - mean_c)**2 for r in chunk) / (len(chunk) - 1)
            window_rvs.append(math.sqrt(var_c) if var_c > 0 else 0.0)

    if len(window_rvs) < 3:
        return None

    mean_rv = sum(window_rvs) / len(window_rvs)
    std_rv = math.sqrt(sum((r - mean_rv)**2 for r in window_rvs) / (len(window_rvs) - 1))

    if std_rv <= 0:
        return None

    return (current_rv - mean_rv) / std_rv


def _compute_distance_zscore(
    current_price: float,
    strike: float,
    current_rv: float,
    seconds_left: float,
) -> Optional[float]:
    """
    Distance Z-score: how many RV-standard-deviations is the price from strike?
    distance_z = abs(log(current_price/strike)) / (rv * sqrt(minutes_left))
    """
    if current_rv <= 0 or seconds_left <= 0:
        return None
    minutes_left = seconds_left / 60.0
    expected_move = current_rv * math.sqrt(minutes_left)
    if expected_move <= 0:
        return None
    log_dist = abs(math.log(current_price / strike)) if strike > 0 and current_price > 0 else 0.0
    return log_dist / expected_move


def _session(ts: float) -> str:
    """Return 'us', 'asian', or 'other'."""
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    h = dt.hour
    if ASIAN_SESSION_SKIP_START <= h < ASIAN_SESSION_SKIP_END:
        return "asian"
    if US_SESSION_START <= h < US_SESSION_END:
        return "us"
    return "other"


class BTCHourlyStrategy(BaseStrategy):
    """
    BTC Hourly V2: vol-regime + distance-normalization continuation strategy.
    Drops variance ratio and 30-min momentum (anti-predictive in V1).
    """

    def __init__(
        self,
        skip_config: SkipConfig,
        min_ev: float,
        stake_dollars: float,
        calibrator: Optional[AssetCalibrator] = None,
        maker: bool = False,
    ):
        super().__init__(
            asset="BTC",
            skip_config=skip_config,
            min_ev=min_ev,
            stake_dollars=stake_dollars,
            calibrator=calibrator,
            maker=maker,
        )

    def compute_raw_p_model(
        self,
        features: MarketFeatures,
        baseline_p_above: float,
    ) -> tuple[float, dict]:
        signals: dict = {}
        p_yes = baseline_p_above
        taper = magnitude_taper(baseline_p_above)
        above = features.current_price > features.strike

        # ---- Session filter ------------------------------------------------
        session = _session(features.timestamp)
        signals["session"] = session

        if session == "asian":
            # Skip Asian dead hours -- low liquidity, signals unreliable
            signals["skip_reason"] = "asian_session"
            signals["final_p_yes"] = round(p_yes, 4)
            signals["baseline_p_above"] = round(baseline_p_above, 4)
            # Return baseline unchanged -- the EV won't clear min_ev threshold
            # without a real signal boost, effectively skipping.
            return p_yes, signals

        # ---- Signal 1: Vol Z-score (quiet/volatile regime) -----------------
        rv = features.realized_vol_1min or 0.003
        prices_list = list(features.prices_60m)
        vol_z = _compute_vol_zscore(prices_list, rv)
        signals["vol_z"] = round(vol_z, 3) if vol_z is not None else None

        vol_adj = 0.0
        if vol_z is not None:
            if vol_z < VOL_ZSCORE_QUIET_THRESHOLD:
                # Quiet regime: continuation is more reliable
                vol_adj = QUIET_CONTINUATION_BOOST if above else -QUIET_CONTINUATION_BOOST
            elif vol_z > VOL_ZSCORE_NOISY_THRESHOLD:
                # Volatile regime: continuation less reliable, reduce confidence
                vol_adj = -NOISY_CONTINUATION_REDUCE if above else NOISY_CONTINUATION_REDUCE

        # Scale by session: US session vol signals are more reliable
        if session == "us":
            vol_adj *= 1.2

        p_yes += vol_adj * taper
        signals["vol_adj"] = round(vol_adj, 4)

        # ---- Signal 2: Distance Z-score (safe continuation when far away) --
        dist_z = _compute_distance_zscore(
            features.current_price, features.strike, rv, features.seconds_left
        )
        signals["dist_z"] = round(dist_z, 3) if dist_z is not None else None

        dist_adj = 0.0
        if dist_z is not None and dist_z > DISTANCE_ZSCORE_HIGH:
            # Very far from strike: continuation extremely likely
            dist_adj = DISTANCE_ZSCORE_BOOST if above else -DISTANCE_ZSCORE_BOOST

        p_yes += dist_adj * taper
        signals["dist_adj"] = round(dist_adj, 4)

        p_yes = max(0.05, min(0.95, p_yes))
        signals["final_p_yes"] = round(p_yes, 4)
        signals["baseline_p_above"] = round(baseline_p_above, 4)
        signals["raw_p_yes"] = round(p_yes, 4)
        return p_yes, signals
