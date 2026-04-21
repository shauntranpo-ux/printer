"""
BTCHourlyStrategy V3 -- Mean-reversion strategy for BTC 60-min Kalshi binaries.

Core insight: BTC hourly markets overshoot in the short term. When BTC has run
up significantly relative to VWAP / RSI / Bollinger bands AND is above strike,
the hourly contract is overpriced relative to reversion probability.

Signal pipeline:
1. VWAP deviation z-score: fade overbought/oversold relative to session VWAP.
2. RSI extremes: fade RSI > 68 (overbought) or < 32 (oversold).
3. Bollinger bands: fade moves outside 2-sigma bands.
4. Momentum reversal timing: after 35+ min, strong directional moves tend to fade.
5. Asian session skip: 01-07 UTC, low liquidity, signals unreliable.

All signals are contrarian/mean-reversion. They reduce p_yes when above strike
(overbought = bet NO), increase when below strike (oversold = bet YES).
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
from strategies.signals.intraday_signals import (
    vwap_deviation,
    rsi,
    momentum_reversal_signal,
    bollinger_signal,
)


ASIAN_SESSION_SKIP_START = 1   # UTC hour: skip from 01:00
ASIAN_SESSION_SKIP_END   = 7   # UTC hour: skip before 07:00

# VWAP signal thresholds
VWAP_Z_STRONG = 1.5    # strong overbought/oversold
VWAP_STRONG_ADJ = 0.10  # p_yes adjustment for strong VWAP signal

# RSI thresholds
RSI_OVERBOUGHT_STRONG = 68
RSI_OVERSOLD_STRONG   = 32
RSI_OVERBOUGHT_MILD   = 55
RSI_OVERSOLD_MILD     = 45
RSI_STRONG_ADJ        = 0.07
RSI_MILD_ADJ          = 0.03

# Bollinger band adjustment
BB_ADJ = 0.05

# Momentum reversal adjustment
MOM_REV_ADJ = 0.06


def _session(ts: float) -> str:
    """Return 'asian' or 'other'."""
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    h = dt.hour
    if ASIAN_SESSION_SKIP_START <= h < ASIAN_SESSION_SKIP_END:
        return "asian"
    return "other"


class BTCHourlyStrategy(BaseStrategy):
    """
    BTC Hourly V3: mean-reversion using VWAP, RSI, Bollinger, momentum reversal.
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

        # ---- Session filter: skip Asian dead hours --------------------------
        session = _session(features.timestamp)
        signals["session"] = session

        if session == "asian":
            signals["skip_reason"] = "asian_session"
            signals["final_p_yes"] = round(p_yes, 4)
            signals["baseline_p_above"] = round(baseline_p_above, 4)
            signals["raw_p_yes"] = round(p_yes, 4)
            return p_yes, signals

        prices_list = list(features.prices_60m)
        signals["n_prices"] = len(prices_list)

        # ---- Signal 1: VWAP deviation z-score ------------------------------
        vwap_result = None
        vwap_adj = 0.0
        if prices_list:
            # Build dummy volume list (all ones) if no volume -- signal degrades gracefully
            # In backtest, we pass volume from the parquet; in live, we use 1-min bars.
            # For now use uniform volume (equal weight) which reduces to price z-score.
            dummy_vols = [(ts, 1.0) for ts, _ in prices_list]
            vwap_result = vwap_deviation(prices_list, dummy_vols)

        if vwap_result is not None:
            vwap_val, vwap_z = vwap_result
            signals["vwap"] = round(vwap_val, 2)
            signals["vwap_z"] = round(vwap_z, 3)

            if vwap_z > VWAP_Z_STRONG and above:
                # Overbought vs VWAP AND above strike: expect reversion, bet NO
                vwap_adj = -VWAP_STRONG_ADJ
            elif vwap_z < -VWAP_Z_STRONG and not above:
                # Oversold vs VWAP AND below strike: expect reversion, bet YES
                vwap_adj = VWAP_STRONG_ADJ
        else:
            signals["vwap"] = None
            signals["vwap_z"] = None

        p_yes += vwap_adj * taper
        signals["vwap_adj"] = round(vwap_adj, 4)

        # ---- Signal 2: RSI extreme -----------------------------------------
        rsi_val = rsi(prices_list, period=14)
        signals["rsi"] = round(rsi_val, 2) if rsi_val is not None else None

        rsi_adj = 0.0
        if rsi_val is not None:
            if rsi_val > RSI_OVERBOUGHT_STRONG and above:
                rsi_adj = -RSI_STRONG_ADJ  # overbought, above strike: bet NO
            elif rsi_val < RSI_OVERSOLD_STRONG and not above:
                rsi_adj = RSI_STRONG_ADJ   # oversold, below strike: bet YES
            elif RSI_OVERBOUGHT_MILD < rsi_val <= RSI_OVERBOUGHT_STRONG and above:
                rsi_adj = -RSI_MILD_ADJ    # mildly overbought
            elif RSI_OVERSOLD_STRONG <= rsi_val < RSI_OVERSOLD_MILD and not above:
                rsi_adj = RSI_MILD_ADJ     # mildly oversold

        p_yes += rsi_adj * taper
        signals["rsi_adj"] = round(rsi_adj, 4)

        # ---- Signal 3: Bollinger bands -------------------------------------
        bb = bollinger_signal(prices_list, period=20, num_std=2.0)
        signals["bollinger"] = bb

        bb_adj = 0.0
        if bb == "above_upper" and above:
            bb_adj = -BB_ADJ   # price outside upper band AND above strike: revert
        elif bb == "below_lower" and not above:
            bb_adj = BB_ADJ    # price outside lower band AND below strike: revert

        p_yes += bb_adj * taper
        signals["bb_adj"] = round(bb_adj, 4)

        # ---- Signal 4: Momentum reversal timing ----------------------------
        # Use first price in history as window open price proxy
        window_open_price = prices_list[0][1] if prices_list else features.current_price
        mom_signal = momentum_reversal_signal(
            prices_list,
            elapsed_seconds=features.elapsed_seconds,
            window_open_price=window_open_price,
        )
        signals["momentum_reversal"] = mom_signal

        mom_adj = 0.0
        if mom_signal == "fade_up" and above:
            mom_adj = -MOM_REV_ADJ  # strong run-up, above strike: expect fade
        elif mom_signal == "fade_down" and not above:
            mom_adj = MOM_REV_ADJ   # strong drop, below strike: expect bounce

        p_yes += mom_adj * taper
        signals["mom_adj"] = round(mom_adj, 4)

        # ---- Total signal sum for diagnostics ------------------------------
        total_adj = vwap_adj + rsi_adj + bb_adj + mom_adj
        signals["total_adj_before_taper"] = round(total_adj, 4)

        p_yes = max(0.05, min(0.95, p_yes))
        signals["final_p_yes"] = round(p_yes, 4)
        signals["baseline_p_above"] = round(baseline_p_above, 4)
        signals["raw_p_yes"] = round(p_yes, 4)
        return p_yes, signals
