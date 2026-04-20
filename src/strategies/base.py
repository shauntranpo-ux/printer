"""
Abstract base class every per-market strategy inherits.

Concrete strategies override only `compute_raw_p_model()`. Everything else
is shared infrastructure.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from strategies.features import MarketFeatures, Decision
from strategies.skip_layer import check_skip, SkipConfig
from strategies.ev import compute_bidirectional_ev
from strategies.calibration import AssetCalibrator


class BaseStrategy(ABC):
    """
    Every per-asset strategy inherits this.

    The decide() method receives raw features and returns a Decision.
    The base class handles:
      - skip layer (pre-strategy filters)
      - baseline computation (market-implied probability from Kalshi AMM price)
      - calibration of p_model
      - bidirectional EV calculation
      - final skip if no positive-EV side exists

    Concrete strategies override `compute_raw_p_model(features, baseline)`
    which returns the strategy's raw probability estimate. The base class
    handles the rest.
    """

    def __init__(
        self,
        asset: str,
        skip_config: SkipConfig,
        min_ev: float,
        stake_dollars: float,
        calibrator: Optional[AssetCalibrator] = None,
        maker: bool = False,
    ):
        self.asset = asset
        self.skip_config = skip_config
        self.min_ev = min_ev
        self.stake_dollars = stake_dollars
        self.calibrator = calibrator or AssetCalibrator(asset)
        self.maker = maker

    @abstractmethod
    def compute_raw_p_model(
        self,
        features: MarketFeatures,
        baseline_p_above: float,
    ) -> tuple[float, dict]:
        """
        Subclasses implement this.

        Args:
            features: all available market features
            baseline_p_above: market-implied P(close above strike), derived from
                             Kalshi AMM ask prices: yes_ask / (yes_ask + no_ask)

        Returns:
            (p_model_yes, contributing_signals)
            where p_model_yes is P(YES wins), i.e. P(close > strike)
            contributing_signals is a dict of signal_name -> value for logging
        """
        ...

    def decide(self, features: MarketFeatures, macro_event_active: bool = False) -> Decision:
        """
        Full decision pipeline. Strategies don't override this.
        """
        # Step 1: skip layer
        skip_reason = check_skip(features, self.skip_config, macro_event_active)
        if skip_reason:
            return Decision(
                action="skip",
                side=None,
                p_model=0.5,
                reason=f"skip_layer: {skip_reason}",
            )

        # Step 2: baseline probability — use market-implied probability.
        # The Kalshi AMM already prices in price distance vs strike and vol.
        # Our signals should be the only source of edge, not a physics model
        # that disagrees with the market on information it already has.
        _yes = features.yes_ask / 100.0
        _no = features.no_ask / 100.0
        _total = _yes + _no
        baseline_p_above = _yes / _total if _total > 0 else 0.5

        # Step 3: strategy's raw p_model (P(yes wins) = P(close > strike))
        raw_p_yes, signals = self.compute_raw_p_model(features, baseline_p_above)

        # Step 4: calibrate
        calibrated_p_yes = self.calibrator.calibrate(raw_p_yes)

        # Step 5: bidirectional EV
        ev = compute_bidirectional_ev(
            p_model=calibrated_p_yes,
            yes_ask_cents=features.yes_ask,
            no_ask_cents=features.no_ask,
            stake_dollars=self.stake_dollars,
            maker=self.maker,
        )

        # Step 6: EV threshold check
        if ev.best_side is None or ev.best_ev < self.min_ev:
            return Decision(
                action="skip",
                side=None,
                p_model=calibrated_p_yes,
                reason=(
                    f"EV below threshold: best_ev={ev.best_ev:+.3f} "
                    f"(yes={ev.yes_ev:+.3f}, no={ev.no_ev:+.3f}, min={self.min_ev:+.3f})"
                ),
                contributing_signals={
                    **signals,
                    "baseline_p_above": baseline_p_above,
                    "raw_p_yes": raw_p_yes,
                    "calibrated_p_yes": calibrated_p_yes,
                    "yes_ev": ev.yes_ev,
                    "no_ev": ev.no_ev,
                },
                expected_value=ev.best_ev,
            )

        # Step 7: trade decision
        return Decision(
            action="trade",
            side=ev.best_side,
            p_model=calibrated_p_yes,
            reason=(
                f"{ev.best_side} EV={ev.best_ev:+.3f} "
                f"(p_model={calibrated_p_yes:.3f}, "
                f"yes_ask={features.yes_ask:.0f}c, no_ask={features.no_ask:.0f}c)"
            ),
            contributing_signals={
                **signals,
                "baseline_p_above": baseline_p_above,
                "raw_p_yes": raw_p_yes,
                "calibrated_p_yes": calibrated_p_yes,
                "yes_ev": ev.yes_ev,
                "no_ev": ev.no_ev,
            },
            expected_value=ev.best_ev,
        )
