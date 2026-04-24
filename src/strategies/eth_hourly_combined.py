"""
ETHHourlyCombinedStrategy — pass-through strategy for ETH hourly binaries.

Uses only the market-implied probability (Kalshi AMM price) as the baseline.
Octagon determines direction. BaseStrategy handles the full gate stack:
  1. Skip layer (spread, cold-start, vol-ratio)
  2. Octagon direction
  3. EV threshold
  4. Entry price ceiling (80c)
"""

from __future__ import annotations
from typing import Optional

from strategies.base import BaseStrategy
from strategies.features import MarketFeatures
from strategies.skip_layer import SkipConfig
from strategies.calibration import AssetCalibrator


class ETHHourlyCombinedStrategy(BaseStrategy):

    def __init__(
        self,
        skip_config: SkipConfig,
        min_ev: float,
        stake_dollars: float,
        calibrator: Optional[AssetCalibrator] = None,
        confidence_threshold: float = 0.0,
    ):
        super().__init__(
            asset="ETH",
            skip_config=skip_config,
            min_ev=min_ev,
            stake_dollars=stake_dollars,
            calibrator=calibrator,
            confidence_threshold=confidence_threshold,
        )

    def compute_raw_p_model(
        self, features: MarketFeatures, baseline_p_above: float
    ) -> tuple[float, dict]:
        return baseline_p_above, {}
