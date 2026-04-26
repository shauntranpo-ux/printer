"""
FifteenMinStrategy — unified 15-minute binary strategy for BTC, ETH, SOL, XRP.

Pipeline (enforced by BaseStrategy.decide):
  1. (no pre-filter for 15m)
  2. Octagon       — required direction signal; SKIP if unavailable/timeout/error
  3. Direction     — oct_prob >= 0.5 -> YES, < 0.5 -> NO
  4. EV gate       — BV3 prob used; per-asset minimum (BTC 7%, ETH 9%, SOL/XRP 16%)
  5. Vol ratio     — buffer durability: rv * sqrt(mins) / dist < 1.80
  6. Confidence    — BV3 >= 74% (YES) or <= 26% (NO)
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
        )
