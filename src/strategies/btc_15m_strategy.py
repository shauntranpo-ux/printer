"""
BTC15mStrategy — minimal pass-through strategy for BTC 15-minute binaries.

Uses only the market-implied probability (Kalshi AMM price) as the baseline.
No additional signals are applied. The simplified 15m gate stack applies:
  1. Entry price floor (10c) — first gate, enforced in check_skip_15m()
  2. EV threshold — enforced in BaseStrategy.decide()
  3. Entry price ceiling (76c) — enforced post-EV in BaseStrategy.decide()

Momentum lock, spread cap, cold-start, macro-event, and all other hourly
gates are intentionally absent for this strategy.
"""

from __future__ import annotations
from typing import Optional

from strategies.base import BaseStrategy
from strategies.features import MarketFeatures
from strategies.skip_layer import SkipConfig
from strategies.calibration import AssetCalibrator


class BTC15mStrategy(BaseStrategy):
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
            is_15m=True,
            confidence_threshold=confidence_threshold,
        )

    def compute_raw_p_model(
        self,
        features: MarketFeatures,
        baseline_p_above: float,
    ) -> tuple[float, dict]:
        return baseline_p_above, {"baseline_p_above": round(baseline_p_above, 4)}
