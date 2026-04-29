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
        confidence_threshold: float = 0.0,
    ):
        super().__init__(
            asset="BTC",
            skip_config=skip_config,
            min_ev=min_ev,
            stake_dollars=stake_dollars,
            calibrator=calibrator,
            maker=maker,
            confidence_threshold=confidence_threshold,
        )
