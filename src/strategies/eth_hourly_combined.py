"""
ETHHourlyCombinedStrategy — chains MidWindow -> DwellWindow -> LateWindow.

Priority order for each hourly window:
  1. MidWindowStrategy  : t=9-11min, ETH+BTC dual cross=0, dist>=0.30%
  2. DwellWindowStrategy: t=30-42min, dwell>=80%, streak>=60%
  3. LateWindowStrategy : t>=45min,  dist>=0.3%, persistence bias

Each sub-strategy is time-gated; only the one matching the current window phase
will fire. Earlier passes take priority — once Mid trades a window, Dwell and
Late naturally skip it because the bot won't open a second position.
"""

from __future__ import annotations
from typing import Optional

from strategies.base import BaseStrategy
from strategies.features import MarketFeatures, Decision
from strategies.skip_layer import SkipConfig
from strategies.calibration import AssetCalibrator
from strategies.mid_window_strategy import MidWindowStrategy
from strategies.dwell_window_strategy import DwellWindowStrategy
from strategies.late_window_strategy import LateWindowStrategy


class ETHHourlyCombinedStrategy(BaseStrategy):
    """
    Dispatches to the correct time-gated strategy for the current window phase.
    """

    def __init__(
        self,
        skip_config: SkipConfig,
        stake_dollars: float,
        calibrator: Optional[AssetCalibrator] = None,
        confidence_threshold: float = 0.0,
    ):
        super().__init__(
            asset="ETH",
            skip_config=skip_config,
            min_ev=0.0,
            stake_dollars=stake_dollars,
            calibrator=calibrator,
            confidence_threshold=confidence_threshold,
        )
        self.mid   = MidWindowStrategy("ETH", skip_config, stake_dollars, calibrator, confidence_threshold=confidence_threshold)
        self.dwell = DwellWindowStrategy("ETH", skip_config, stake_dollars, calibrator, confidence_threshold=confidence_threshold)
        self.late  = LateWindowStrategy("ETH", skip_config, stake_dollars, calibrator, confidence_threshold=confidence_threshold)

    def compute_raw_p_model(
        self, features: MarketFeatures, baseline_p_above: float
    ) -> tuple[float, dict]:
        return baseline_p_above, {}  # decide() overrides

    def decide(self, features: MarketFeatures, macro_event_active: bool = False) -> Decision:
        last = Decision(action="skip", side=None, p_model=0.5, reason="no_window_match")
        for strat in (self.mid, self.dwell, self.late):
            d = strat.decide(features, macro_event_active)
            last = d
            if d.action == "trade":
                return d
        return last
