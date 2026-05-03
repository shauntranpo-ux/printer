"""
BTCStrategy — B3: Kalshi order-book imbalance + velocity.

Components:
1. Kalshi-book OBI — directional pressure on the contract book
2. Kalshi velocity — same off-the-shelf signal used by the other strats
"""

from __future__ import annotations
from typing import Optional

from strategies.base import BaseStrategy
from strategies.features import MarketFeatures, Decision
from strategies.skip_layer import SkipConfig, check_skip_with_asset_hook
from strategies.calibration import AssetCalibrator

from strategies.signals.kalshi_velocity import contract_velocity
from strategies.signals.taper import magnitude_taper
from strategies.signals.btc_diurnal_obi import kalshi_book_obi


B3_OBI_THRESHOLD = 0.04   # min |OBI| before the signal fires
B3_OBI_ADJ_MAX   = 0.04   # peak-band magnitude
B3_VELOCITY_ADJ  = 0.02


class BTCStrategy(BaseStrategy):
    def __init__(
        self,
        skip_config: SkipConfig,
        min_ev: float,
        stake_dollars: float,
        calibrator: Optional[AssetCalibrator] = None,
        maker: bool = False,
        min_votes: int = 3,
    ):
        super().__init__(
            asset="BTC",
            skip_config=skip_config,
            min_ev=min_ev,
            stake_dollars=stake_dollars,
            calibrator=calibrator,
            maker=maker,
            min_votes=min_votes,
        )

    def decide(self, features: MarketFeatures, macro_event_active: bool = False) -> Decision:
        skip_reason = check_skip_with_asset_hook(
            features, self.skip_config, macro_event_active, None
        )
        if skip_reason:
            return Decision(
                action="skip",
                side=None,
                p_model=0.5,
                reason=f"skip_layer: {skip_reason}",
            )

        return self._decide_after_skip(features)

    def _decide_after_skip(self, features: MarketFeatures) -> Decision:
        from strategies.baseline import brownian_bridge_prob_above
        from strategies.ev import compute_bidirectional_ev

        baseline_p_above = brownian_bridge_prob_above(
            features.current_price,
            features.strike,
            features.seconds_left,
            features.realized_vol_1min or 0.001,
        )

        raw_p_yes, signals = self.compute_raw_p_model(features, baseline_p_above)

        calibrated_p_yes = self.calibrator.calibrate(raw_p_yes)
        ev = compute_bidirectional_ev(
            p_model=calibrated_p_yes,
            yes_ask_cents=features.yes_ask,
            no_ask_cents=features.no_ask,
            stake_dollars=self.stake_dollars,
            maker=self.maker,
        )

        merged = {
            **signals,
            "baseline_p_above": baseline_p_above,
            "raw_p_yes": raw_p_yes,
            "calibrated_p_yes": calibrated_p_yes,
            "yes_ev": ev.yes_ev,
            "no_ev": ev.no_ev,
        }

        if ev.best_side is None or ev.best_ev < self.min_ev:
            return Decision(
                action="skip",
                side=None,
                p_model=calibrated_p_yes,
                reason=f"EV below threshold: best={ev.best_ev:+.3f} (min={self.min_ev:+.3f})",
                contributing_signals=merged,
                expected_value=ev.best_ev,
            )

        return Decision(
            action="trade",
            side=ev.best_side,
            p_model=calibrated_p_yes,
            reason=f"{ev.best_side} EV={ev.best_ev:+.3f} (p_yes={calibrated_p_yes:.3f})",
            contributing_signals=merged,
            expected_value=ev.best_ev,
        )

    def compute_raw_p_model(
        self,
        features: MarketFeatures,
        baseline_p_above: float,
    ) -> tuple[float, dict]:
        signals: dict = {}
        p_yes = baseline_p_above
        taper = magnitude_taper(baseline_p_above)

        # ── B3 OBI signal ────────────────────────────────────────────────
        obi = kalshi_book_obi(
            features.yes_bid,
            features.no_bid,
            features.yes_ask,
            features.no_ask,
        )
        obi_adj = 0.0
        if obi is not None and abs(obi) >= B3_OBI_THRESHOLD:
            obi_adj = B3_OBI_ADJ_MAX if obi > 0 else -B3_OBI_ADJ_MAX
        signals["obi"] = obi
        signals["obi_adj"] = obi_adj
        p_yes += obi_adj * taper

        # ── Kalshi velocity (off-the-shelf) ─────────────────────────────
        velocity = contract_velocity(
            list(features.kalshi_price_history),
            lookback_samples=30,
            threshold_pct=0.02,
        )
        velocity_adj = 0.0
        if velocity == "rising":
            velocity_adj = +B3_VELOCITY_ADJ
        elif velocity == "falling":
            velocity_adj = -B3_VELOCITY_ADJ
        signals["velocity"] = velocity
        signals["velocity_adj"] = velocity_adj
        p_yes += velocity_adj * taper

        p_yes = max(0.05, min(0.95, p_yes))
        signals["final_p_yes"] = p_yes
        return p_yes, signals
