"""
LateWindowStrategy — pure persistence-bias strategy for hourly crypto binaries.

Mechanism: Brownian Bridge prices contracts assuming zero drift. When price is
already >= DIST_PCT above/below strike with only SEC_LEFT_THRESHOLD seconds
remaining, momentum persistence means the contract stays ITM ~97%+ of the time.
The BB systematically underprices this by 5-8%.

Rule: skip everything before t=45min. At t>=45min, trade the ITM side whenever
|pct_from_strike| >= 0.3% AND that side's entry price >= MIN_ENTRY_CENTS.

Validated in BOTH 2022-23 (train) and 2024-26 (test) for ETH and BTC:
  ETH: 97.1-97.4% WR  |  BTC: 97.6-97.8% WR
"""

from __future__ import annotations
from typing import Optional

from strategies.base import BaseStrategy
from strategies.calibration import AssetCalibrator
from strategies.features import MarketFeatures, Decision
from strategies.skip_layer import SkipConfig


SEC_LEFT_THRESHOLD = 900    # 15 min remaining = t>=45min in 60min window
DIST_PCT           = 0.3    # min % distance from strike
MIN_ENTRY_CENTS    = 85.0   # skip if trade side entry < this


class LateWindowStrategy(BaseStrategy):
    """
    Skips all early-window evals. At t>=45min with deep ITM cushion, trades
    the in-the-money side without any directional signals or momentum filters.
    """

    def __init__(
        self,
        asset: str,
        skip_config: SkipConfig,
        stake_dollars: float,
        calibrator: Optional[AssetCalibrator] = None,
    ):
        # min_ev not used — we override decide() entirely
        super().__init__(
            asset=asset,
            skip_config=skip_config,
            min_ev=0.0,
            stake_dollars=stake_dollars,
            calibrator=calibrator,
        )

    def compute_raw_p_model(
        self,
        features: MarketFeatures,
        baseline_p_above: float,
    ) -> tuple[float, dict]:
        # Not called; decide() is overridden. Stub to satisfy abstract requirement.
        return baseline_p_above, {}

    def decide(self, features: MarketFeatures, macro_event_active: bool = False) -> Decision:
        """
        Direct rule-based decide — bypasses EV / calibration / momentum machinery.
        Returns a trade when all late-window conditions are met, skip otherwise.
        """
        # Hard block: too close to expiry for execution
        if features.seconds_left < self.skip_config.min_seconds_left:
            return Decision(
                action="skip",
                side=None,
                p_model=0.5,
                reason=f"too_close: {features.seconds_left:.0f}s left",
            )

        # Macro news freeze
        if macro_event_active:
            return Decision(action="skip", side=None, p_model=0.5, reason="macro_event")

        # Patience: wait until t>=45min
        if features.seconds_left > SEC_LEFT_THRESHOLD:
            return Decision(
                action="skip",
                side=None,
                p_model=0.5,
                reason=f"patience: {features.seconds_left:.0f}s > {SEC_LEFT_THRESHOLD}s",
            )

        # Need enough price history for vol computation
        if len(features.prices_60m) < self.skip_config.cold_start_samples:
            return Decision(action="skip", side=None, p_model=0.5, reason="cold_start")

        # Compute distance from strike
        pct = (features.current_price - features.strike) / features.strike * 100.0

        if pct >= DIST_PCT:
            side = "yes"
            entry_cents = features.yes_ask
        elif pct <= -DIST_PCT:
            side = "no"
            entry_cents = features.no_ask
        else:
            return Decision(
                action="skip",
                side=None,
                p_model=0.5,
                reason=f"dead_zone: pct={pct:+.2f}% within ±{DIST_PCT}%",
            )

        # Entry price floor: skip if market prices the side below our threshold
        if entry_cents < MIN_ENTRY_CENTS:
            return Decision(
                action="skip",
                side=None,
                p_model=0.5,
                reason=f"entry_too_cheap: {side}_ask={entry_cents:.0f}c < {MIN_ENTRY_CENTS:.0f}c",
            )

        signals = {
            "pct_from_strike": round(pct, 3),
            "seconds_left": features.seconds_left,
            "entry_cents": entry_cents,
            "dist_threshold": DIST_PCT,
            "sec_left_threshold": SEC_LEFT_THRESHOLD,
        }

        return Decision(
            action="trade",
            side=side,
            p_model=(entry_cents / 100.0 + 0.05) if side == "yes" else (1.0 - (entry_cents / 100.0 + 0.05)),  # P(YES wins)
            reason=(
                f"late_window_{side.upper()}: pct={pct:+.2f}% "
                f"entry={entry_cents:.0f}c "
                f"sec_left={features.seconds_left:.0f}"
            ),
            contributing_signals=signals,
            expected_value=0.05,  # approximate; true EV is 5-8% above market
        )
