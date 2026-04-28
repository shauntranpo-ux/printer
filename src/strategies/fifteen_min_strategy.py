"""
FifteenMinStrategy — unified 15-minute binary strategy for BTC, ETH, SOL, XRP.

Pipeline (enforced by BaseStrategy.decide):
  1. (no pre-filter for 15m)
  2. Supertrend    — sole direction signal; SKIP if insufficient data
  3. Direction     — supertrend=1 -> YES, supertrend=-1 -> NO
  4. EV gate       — fixed p_ev=0.70; per-asset minimum (BTC 7%, ETH 9%, SOL/XRP 16%)
  5. Vol ratio     — buffer durability: rv * sqrt(mins) / dist < 1.80
  6. Confidence    — disabled by default (confidence_threshold_15m=0)
  7. Entry range   — entry must be in [20c, 76c)
  8. Trade
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
    ):
        super().__init__(
            asset=asset,
            skip_config=skip_config,
            min_ev=min_ev,
            stake_dollars=stake_dollars,
            calibrator=calibrator,
            maker=maker,
            is_15m=True,
            confidence_threshold=confidence_threshold,
            supertrend_atr_period=supertrend_atr_period,
            supertrend_atr_multiplier=supertrend_atr_multiplier,
        )
