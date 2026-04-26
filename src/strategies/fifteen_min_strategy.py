"""
FifteenMinStrategy — unified 15-minute binary strategy for BTC, ETH, SOL, XRP.

Pipeline (enforced by BaseStrategy.decide):
  1. Price floor   — max(yes_ask, no_ask) >= 35c (we always trade the expensive side)
  2. Octagon       — queried for signal telemetry; direction ignores oct_prob for 15m
  3. Direction     — above strike -> YES, below strike -> NO (price-continuation)
  4. BV3           — P(YES=above strike), converted in bot.py from P(stays on current side)
  5. EV gate       — min 11% after Kalshi fee drag
  6. Confidence    — BV3 must have >= 74% conviction price stays on its side
  7. Price cap     — entry must be < 76c
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
