"""
FifteenMinStrategy — unified 15-minute binary strategy for BTC, ETH, SOL, XRP.

Pipeline (enforced by BaseStrategy.decide):
  1. Market prob   — Kalshi AMM implied probability (reference only)
  2. Direction     — D3-hybrid ensemble vote (compute_15m_signal)
  3. EV gate       — calibrated BS p_yes; per-asset minimum threshold
  4. Vol ratio     — buffer durability: rv * sqrt(mins) / dist < threshold
  5. Confidence    — disabled by default (confidence_threshold_15m=0)
  6. Entry range   — entry must be in [20c, 76c)
  7. Trade
"""

from __future__ import annotations
from typing import Optional

from strategies.base import BaseStrategy
from strategies.skip_layer import SkipConfig
from strategies.calibration import AssetCalibrator


class FifteenMinStrategy(BaseStrategy):
    def __init__(
        self,
        asset: str,
        skip_config: SkipConfig,
        min_ev: float,
        stake_dollars: float,
        calibrator: Optional[AssetCalibrator] = None,
        maker: bool = False,
        confidence_threshold: float = 0.0,
        supertrend_atr_period: int = 10,
        supertrend_atr_multiplier: float = 4.0,
        momentum_lookback: int = 4,
        min_votes: int = 3,
    ):
        super().__init__(
            asset=asset,
            skip_config=skip_config,
            min_ev=min_ev,
            stake_dollars=stake_dollars,
            calibrator=calibrator,
            maker=maker,
            confidence_threshold=confidence_threshold,
            supertrend_atr_period=supertrend_atr_period,
            supertrend_atr_multiplier=supertrend_atr_multiplier,
            momentum_lookback=momentum_lookback,
            min_votes=min_votes,
        )
