"""
ETHStrategy — pass-through strategy for ETH 15-min binaries.

Uses only the market-implied probability (Kalshi AMM price) as the baseline.
No additional signals are applied. BaseStrategy handles the full gate stack:
  1. Entry price floor (10c)
  2. EV threshold
  3. Confidence threshold
  4. Entry price ceiling (76c)
  5. Octagon AI gate
"""

from __future__ import annotations
from typing import Optional

from strategies.base import BaseStrategy
from strategies.features import MarketFeatures
from strategies.skip_layer import SkipConfig
from strategies.calibration import AssetCalibrator


class ETHStrategy(BaseStrategy):
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
            asset="ETH",
            skip_config=skip_config,
            min_ev=min_ev,
            stake_dollars=stake_dollars,
            calibrator=calibrator,
            maker=maker,
            is_15m=True,
            confidence_threshold=confidence_threshold,
        )

    def compute_raw_p_model(
        self,
        features: MarketFeatures,
        baseline_p_above: float,
    ) -> tuple[float, dict]:
        return baseline_p_above, {"baseline_p_above": round(baseline_p_above, 4)}
