"""
ETHHourlyStrategy V2 -- evidence-based strategy for ETH 60-min Kalshi binaries.

Core insight: ETH follows BTC with ~15-20 minute lag. When BTC moves significantly,
the ETH Kalshi contract hasn't fully repriced yet -- that lag is the edge.

Signal pipeline:
1. BTC 15-min lead return (primary, weight up to 0.12):
   BTC log return over last 15 min * beta. If BTC is down 0.5% and ETH is above
   strike, ETH will likely follow BTC below strike -> big p_yes reduction.
2. BTC 5-min acceleration (secondary, weight 0.04):
   Is the BTC move accelerating or decelerating?
   Accelerating = stronger lead-lag signal.
3. ETH/BTC correlation stability (weight 0.03):
   Recent 2h beta vs long-term beta. Tight correlation -> lead signal more reliable.
4. Vol filter: If ETH RV > 3x BTC RV, correlation breaks down -> scale all signals 0.5x.

Key change from V1: 15-min BTC return instead of 30-min. The 15-min window is the
sweet spot for ETH-BTC lead-lag before the information gets priced in.
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
from strategies.signals.beta_cache import load_beta


BTC_LEAD_WINDOW_SECONDS  = 900   # 15 minutes
BTC_ACCEL_WINDOW_SECONDS = 300   # 5 minutes for acceleration
BETA_ADJ_MAX             = 0.12  # max BTC lead contribution
ACCEL_ADJ_MAX            = 0.04  # max acceleration contribution
CORR_ADJ_MAX             = 0.03  # max correlation stability contribution
VOL_RATIO_BREAK          = 3.0   # if eth_rv / btc_rv > this, scale down signals
VOL_BREAK_SCALE          = 0.5   # scale factor when correlation breaks


def _lead_return(prices: list, window_seconds: float) -> Optional[float]:
    """Log return over the last `window_seconds` seconds."""
    if not prices:
        return None
    entries = list(prices)
    if not entries:
        return None
    now_ts = entries[-1][0]
    cutoff = now_ts - window_seconds
    anchor = None
    for ts, p in entries:
        if ts >= cutoff:
            anchor = p
            break
    if anchor is None or anchor <= 0:
        return None
    current = entries[-1][1]
    if current <= 0:
        return None
    return math.log(current / anchor)


def _compute_recent_beta(eth_prices: list, btc_prices: list, window_seconds: float = 7200) -> Optional[float]:
    """
    Compute recent ETH/BTC beta over the last `window_seconds`.
    Returns OLS slope or None if insufficient data.
    """
    if not eth_prices or not btc_prices:
        return None

    eth_arr = list(eth_prices)
    btc_arr = list(btc_prices)

    if not eth_arr or not btc_arr:
        return None

    cutoff = eth_arr[-1][0] - window_seconds

    # Build aligned return arrays
    eth_map = {int(ts): p for ts, p in eth_arr if ts >= cutoff}
    btc_map = {int(ts): p for ts, p in btc_arr if ts >= cutoff}

    common_ts = sorted(set(eth_map) & set(btc_map))
    if len(common_ts) < 20:
        return None

    eth_rets = []
    btc_rets = []
    prev_e = prev_b = None
    for ts in common_ts:
        e = eth_map[ts]
        b = btc_map[ts]
        if prev_e is not None and prev_b is not None and prev_e > 0 and prev_b > 0 and e > 0 and b > 0:
            eth_rets.append(math.log(e / prev_e))
            btc_rets.append(math.log(b / prev_b))
        prev_e = e
        prev_b = b

    if len(eth_rets) < 15:
        return None

    n = len(eth_rets)
    mean_e = sum(eth_rets) / n
    mean_b = sum(btc_rets) / n

    cov = sum((e - mean_e) * (b - mean_b) for e, b in zip(eth_rets, btc_rets))
    var_b = sum((b - mean_b)**2 for b in btc_rets)

    if var_b <= 0:
        return None

    return cov / var_b


def _compute_rv(prices: list, window_seconds: float = 3600) -> Optional[float]:
    """Realized volatility (std of 1-min log returns) over last window_seconds."""
    if not prices:
        return None
    entries = list(prices)
    if len(entries) < 5:
        return None
    cutoff = entries[-1][0] - window_seconds
    subset = [(ts, p) for ts, p in entries if ts >= cutoff]
    if len(subset) < 5:
        return None
    rets = log_returns_from_prices(subset)
    if len(rets) < 4:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean)**2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) if var > 0 else None


class ETHHourlyStrategy(BaseStrategy):
    """
    ETH Hourly V2: BTC lead-lag strategy with 15-min window.
    Primary signal is BTC's 15-min return scaled by long-run ETH/BTC beta.
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
            asset="ETH",
            skip_config=skip_config,
            min_ev=min_ev,
            stake_dollars=stake_dollars,
            calibrator=calibrator,
            maker=maker,
        )
        self.long_term_beta = load_beta("ETH")

    def compute_raw_p_model(
        self,
        features: MarketFeatures,
        baseline_p_above: float,
    ) -> tuple[float, dict]:
        signals: dict = {}
        p_yes = baseline_p_above
        taper = magnitude_taper(baseline_p_above)
        above = features.current_price > features.strike

        btc_prices = list(features.btc_prices_60m)
        eth_prices = list(features.prices_60m)

        # ---- Vol filter: check if ETH/BTC correlation has broken down ------
        eth_rv = features.realized_vol_1min or 0.003
        btc_rv = _compute_rv(btc_prices, 3600) or 0.003
        vol_ratio = eth_rv / btc_rv if btc_rv > 0 else 1.0
        corr_broken = vol_ratio > VOL_RATIO_BREAK
        signal_scale = VOL_BREAK_SCALE if corr_broken else 1.0
        signals["eth_rv"] = round(eth_rv, 5)
        signals["btc_rv"] = round(btc_rv, 5)
        signals["vol_ratio"] = round(vol_ratio, 2)
        signals["corr_broken"] = corr_broken

        # ---- Signal 1: BTC 15-min lead return (primary) --------------------
        btc_15m = _lead_return(btc_prices, BTC_LEAD_WINDOW_SECONDS)
        signals["btc_15m"] = round(btc_15m, 5) if btc_15m is not None else None

        beta_adj = 0.0
        if btc_15m is not None and self.long_term_beta is not None:
            # BTC 15-min return implies ETH direction
            # Normalize by expected remaining move (RV * sqrt(minutes_left))
            rv = eth_rv
            minutes_left = max(1.0, features.seconds_left / 60.0)
            expected_remaining = rv * math.sqrt(minutes_left)
            if expected_remaining > 0:
                implied_eth_move = self.long_term_beta * btc_15m
                # nudge = how many expected-moves is the implied ETH move?
                nudge = (implied_eth_move / expected_remaining) * BETA_ADJ_MAX
                beta_adj = max(-BETA_ADJ_MAX, min(BETA_ADJ_MAX, nudge))
            signals["beta"] = self.long_term_beta
        else:
            signals["beta"] = self.long_term_beta

        p_yes += beta_adj * taper * signal_scale
        signals["beta_adj"] = round(beta_adj, 4)

        # ---- Signal 2: BTC 5-min acceleration (is momentum building?) ------
        btc_5m = _lead_return(btc_prices, BTC_ACCEL_WINDOW_SECONDS)
        signals["btc_5m"] = round(btc_5m, 5) if btc_5m is not None else None

        accel_adj = 0.0
        if btc_15m is not None and btc_5m is not None:
            # Acceleration: is the 5-min move in the same direction and stronger?
            # accel > 0 means move is accelerating (same sign, 5m per-min rate > 15m rate)
            rate_15m = btc_15m / 15.0  # per-minute rate
            rate_5m = btc_5m / 5.0
            acceleration = rate_5m - rate_15m
            # If accelerating in same direction as 15m, boost the beta_adj
            if (btc_15m > 0 and acceleration > 0) or (btc_15m < 0 and acceleration < 0):
                accel_sign = 1.0 if btc_15m > 0 else -1.0
                accel_magnitude = min(1.0, abs(acceleration) / max(abs(rate_15m), 1e-8))
                accel_adj = accel_sign * accel_magnitude * ACCEL_ADJ_MAX
                accel_adj = max(-ACCEL_ADJ_MAX, min(ACCEL_ADJ_MAX, accel_adj))

        p_yes += accel_adj * taper * signal_scale
        signals["accel_adj"] = round(accel_adj, 4)

        # ---- Signal 3: Correlation stability (recent beta vs long-term) ----
        recent_beta = _compute_recent_beta(eth_prices, btc_prices, window_seconds=7200)
        signals["recent_beta"] = round(recent_beta, 3) if recent_beta is not None else None

        corr_adj = 0.0
        if recent_beta is not None and self.long_term_beta is not None and self.long_term_beta != 0:
            beta_ratio = recent_beta / self.long_term_beta
            # If recent_beta is close to long-term beta, correlation is tight -> boost beta_adj
            # If they diverge significantly, reduce confidence
            beta_stability = max(0.0, 1.0 - abs(beta_ratio - 1.0))
            # Only add stability boost if beta_adj is already nonzero (has direction)
            if abs(beta_adj) > 0.01:
                corr_adj = beta_stability * CORR_ADJ_MAX * (1.0 if beta_adj > 0 else -1.0)

        p_yes += corr_adj * taper * signal_scale
        signals["corr_adj"] = round(corr_adj, 4)

        p_yes = max(0.05, min(0.95, p_yes))
        signals["final_p_yes"] = round(p_yes, 4)
        signals["baseline_p_above"] = round(baseline_p_above, 4)
        signals["raw_p_yes"] = round(p_yes, 4)
        return p_yes, signals
