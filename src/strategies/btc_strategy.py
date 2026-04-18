"""
BTCStrategy — current BTC logic re-homed on the BaseStrategy foundation.

Behavior-preserving canary for the refactor. Outputs must match legacy
printer_brain() for BTC to within floating-point noise when given the same
inputs. Any deviation indicates a bug in the foundation or the port.

Key differences vs ETH/SOL/XRP/DOGE strategies (added in later sections):
- Uses BV3 table for raw win probability, not Brownian-bridge baseline
- Applies legacy momentum adjustment (+/-5%) not variance-ratio
- Applies legacy velocity adjustment (+/-1%)
- Continuation-side-only by default (bidirectional enabled in Section 3
  via config flag)

Imports from bot.py are read-only — we use the existing BV3 loader rather
than duplicating it.
"""

from __future__ import annotations

from typing import Optional

from strategies.base import BaseStrategy
from strategies.features import MarketFeatures, Decision
from strategies.skip_layer import SkipConfig, check_skip
from strategies.calibration import AssetCalibrator
from strategies.ev import compute_bidirectional_ev


class BTCStrategy(BaseStrategy):
    """
    Legacy BTC behavior on new foundation.

    Uses the BV3 empirical table for raw win prob; applies legacy momentum
    and velocity adjustments. Final decision routed through BaseStrategy's
    pipeline (skip layer, calibration, EV, side selection).
    """

    def __init__(
        self,
        skip_config: SkipConfig,
        min_ev: float,
        stake_dollars: float,
        calibrator: Optional[AssetCalibrator] = None,
        maker: bool = False,
        continuation_only: bool = True,  # Section 3 will allow False
    ):
        super().__init__(
            asset="BTC",
            skip_config=skip_config,
            min_ev=min_ev,
            stake_dollars=stake_dollars,
            calibrator=calibrator,
            maker=maker,
        )
        self.continuation_only = continuation_only

    def compute_raw_p_model(
        self,
        features: MarketFeatures,
        baseline_p_above: float,  # unused for BTC (legacy uses BV3)
    ) -> tuple[float, dict]:
        """
        Replicate the legacy printer_brain() probability computation.

        Returns (p_yes, signals_dict) where p_yes is P(YES wins at close).
        """
        # Import locally to avoid circular import with bot.py
        import bot

        abs_pct = abs(features.current_price - features.strike) / features.strike
        above = features.current_price > features.strike
        mins_left = features.seconds_left / 60.0

        # Step 1: BV3 raw probability (same-side probability)
        raw_same_side = bot._win_prob_for_asset("BTC", abs_pct, mins_left)

        # Step 2: legacy momentum adjustment
        # Use features.prices_1m (new foundation) instead of legacy
        # btc_prices (module global) so this is deterministic for tests.
        mom_pct, mom_label = self._legacy_momentum(features)
        if mom_label == "bullish":
            mom_adj = +0.05 if above else -0.05
        elif mom_label == "bearish":
            mom_adj = +0.05 if not above else -0.05
        else:
            mom_adj = 0.0

        # Step 3: legacy velocity adjustment (1% nudge based on contract
        # price trajectory)
        vel_signal = self._legacy_velocity(features)
        vel_adj = (
            +0.01 if vel_signal == "favorable"
            else (-0.01 if vel_signal == "unfavorable" else 0.0)
        )

        # Step 4: combined same-side prob + legacy calibration scale
        cal_scale = getattr(bot, "_brain_cal", {}).get("prob_scale", 1.0)
        combined = raw_same_side + mom_adj + vel_adj
        combined = 0.50 + (combined - 0.50) * cal_scale
        combined = max(0.10, min(0.997, combined))

        # Step 5: convert same-side prob to YES-side prob
        p_yes = combined if above else (1.0 - combined)

        signals = {
            "bv3_raw_same_side": raw_same_side,
            "abs_pct_from_strike": abs_pct,
            "mins_left": mins_left,
            "above_strike": above,
            "mom_label": mom_label,
            "mom_adj": mom_adj,
            "vel_signal": vel_signal,
            "vel_adj": vel_adj,
            "cal_scale": cal_scale,
            "combined_same_side_prob": combined,
        }
        return p_yes, signals

    def _legacy_momentum(self, features: MarketFeatures) -> tuple[float, str]:
        """
        Port of bot.calculate_momentum() using features.prices_1m.

        Threshold: +/- 0.005 (0.5%) over 180 seconds.
        """
        prices = features.prices_1m
        if not prices:
            return 0.0, "neutral"

        cutoff = features.timestamp - 180.0
        oldest = None
        for ts, price in prices:
            if ts >= cutoff:
                oldest = price
                break

        if oldest is None:
            return 0.0, "neutral"

        current = prices[-1][1]
        pct = (current - oldest) / oldest

        if pct > 0.005:
            return pct, "bullish"
        if pct < -0.005:
            return pct, "bearish"
        return pct, "neutral"

    def _legacy_velocity(self, features: MarketFeatures) -> str:
        """
        Port of bot.contract_velocity() reading from features.kalshi_price_history.

        Returns "favorable", "unfavorable", or "neutral".

        Legacy logic: compare the current Kalshi YES price to a recent
        reference to infer direction. Uses last 30 samples.
        Threshold: 5% change (same as contract_velocity's 0.05).
        """
        hist = features.kalshi_price_history
        if len(hist) < 3:
            return "neutral"

        recent = list(hist)
        first_price = recent[0][1] if isinstance(recent[0], tuple) else recent[0]
        last_price = recent[-1][1] if isinstance(recent[-1], tuple) else recent[-1]

        if first_price <= 0:
            return "neutral"

        delta = (last_price - first_price) / first_price
        # YES buys: falling price = favorable
        if delta < -0.05:
            return "favorable"
        if delta > 0.05:
            return "unfavorable"
        return "neutral"

    def decide(
        self,
        features: MarketFeatures,
        macro_event_active: bool = False,
    ) -> Decision:
        """
        Override BaseStrategy.decide() to implement continuation-only mode
        when self.continuation_only is True (default for Section 2).

        When continuation_only=False (Section 3), defer to BaseStrategy's
        bidirectional EV-based side selection.
        """
        if not self.continuation_only:
            return super().decide(features, macro_event_active)

        # Continuation-only path (legacy behavior): still run the skip layer
        skip_reason = check_skip(features, self.skip_config, macro_event_active)
        if skip_reason:
            return Decision(
                action="skip",
                side=None,
                p_model=0.5,
                reason=f"skip_layer: {skip_reason}",
            )

        # Compute raw p_yes (baseline is unused for BTC)
        raw_p_yes, signals = self.compute_raw_p_model(features, baseline_p_above=0.5)

        # Calibrate
        calibrated_p_yes = self.calibrator.calibrate(raw_p_yes)

        # Continuation side: if above strike → YES, else → NO
        above = features.current_price > features.strike
        forced_side = "yes" if above else "no"

        # Compute EV for both sides (we only use the forced side's EV)
        ev_result = compute_bidirectional_ev(
            p_model=calibrated_p_yes,
            yes_ask_cents=features.yes_ask,
            no_ask_cents=features.no_ask,
            stake_dollars=self.stake_dollars,
            maker=self.maker,
        )

        forced_ev = ev_result.yes_ev if forced_side == "yes" else ev_result.no_ev

        if forced_ev < self.min_ev:
            return Decision(
                action="skip",
                side=None,
                p_model=calibrated_p_yes,
                reason=(
                    f"continuation-only {forced_side} EV {forced_ev:+.3f} "
                    f"below min_ev {self.min_ev:+.3f}"
                ),
                contributing_signals={
                    **signals,
                    "raw_p_yes": raw_p_yes,
                    "calibrated_p_yes": calibrated_p_yes,
                    "forced_side": forced_side,
                    "forced_ev": forced_ev,
                    "continuation_only": True,
                },
                expected_value=forced_ev,
            )

        return Decision(
            action="trade",
            side=forced_side,
            p_model=calibrated_p_yes,
            reason=(
                f"continuation-only {forced_side} EV={forced_ev:+.3f} "
                f"(p_yes={calibrated_p_yes:.3f}, above={above})"
            ),
            contributing_signals={
                **signals,
                "raw_p_yes": raw_p_yes,
                "calibrated_p_yes": calibrated_p_yes,
                "forced_side": forced_side,
                "forced_ev": forced_ev,
                "continuation_only": True,
            },
            expected_value=forced_ev,
        )
