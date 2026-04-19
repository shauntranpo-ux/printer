"""
BTCStrategy — evidence-based strategy for BTC 15-min binaries.

Evidence base:
- Wen, Bouri, Xu & Zhao 2022 (NAJEF): intraday momentum-reversal on BTC
  high-frequency data, 2013-2020. Variance-ratio test confirmed.
- Grobys & Sapkota 2021 (IRFA): large-cap cryptos (BTC firmly) exhibit
  momentum rather than reversion in cross-section.
- Cont, Kukanov, Stoikov and successors: order-flow imbalance predictive
  at short horizons. Kalshi contract velocity is a proxy.

Components:
1. Brownian-bridge baseline (primary anchor — physics-based fair value)
2. Variance-ratio regime detector on BTC 1-min returns
3. Momentum-biased prior (large-cap default, small magnitude)
4. Kalshi contract velocity
5. BV3 lookup as secondary signal blended at 20% weight

Rollback path: set use_new_strategies.BTC_continuation_only=true in
config.json to revert to the legacy BV3+momentum+velocity path from
Sections 2-3. No code change required.
"""

from __future__ import annotations
from typing import Optional

from strategies.base import BaseStrategy
from strategies.features import MarketFeatures, Decision
from strategies.skip_layer import SkipConfig, check_skip
from strategies.calibration import AssetCalibrator
from strategies.ev import compute_bidirectional_ev

from strategies.signals.rolling_beta import log_returns_from_prices
from strategies.signals.variance_ratio import variance_ratio, variance_ratio_to_regime
from strategies.signals.kalshi_velocity import contract_velocity
from strategies.signals.bv3_lookup import bv3_p_yes
from strategies.signals.taper import magnitude_taper


MOMENTUM_BIAS    = 0.02
REGIME_ADJ       = 0.03
VELOCITY_ADJ     = 0.02
BV3_BLEND_WEIGHT = 0.20


class BTCStrategy(BaseStrategy):
    """
    Evidence-based BTC strategy. When continuation_only=True, falls back
    to pure BV3 + legacy momentum/velocity as a rollback path.
    """

    def __init__(
        self,
        skip_config: SkipConfig,
        min_ev: float,
        stake_dollars: float,
        calibrator: Optional[AssetCalibrator] = None,
        maker: bool = False,
        continuation_only: bool = False,
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

    def decide(
        self,
        features: MarketFeatures,
        macro_event_active: bool = False,
    ) -> Decision:
        if self.continuation_only:
            return self._decide_legacy_fallback(features, macro_event_active)
        return super().decide(features, macro_event_active)

    def compute_raw_p_model(
        self,
        features: MarketFeatures,
        baseline_p_above: float,
    ) -> tuple[float, dict]:
        """
        Evidence-based probability model for BTC.

        Layers adjustments onto the Brownian-bridge baseline using
        variance-ratio regime, momentum bias, Kalshi velocity, and BV3
        as a 20% secondary blend.
        """
        signals: dict = {}
        p_yes = baseline_p_above
        taper = magnitude_taper(baseline_p_above)
        above = features.current_price > features.strike

        # ── Component 1: variance-ratio regime detector ─────────────────
        btc_returns = log_returns_from_prices(list(features.prices_60m))
        vr = variance_ratio(btc_returns, q=5)
        regime = variance_ratio_to_regime(vr)
        regime_adj = 0.0
        if regime == "momentum":
            regime_adj = +REGIME_ADJ if above else -REGIME_ADJ
        elif regime == "reversion":
            regime_adj = -REGIME_ADJ if above else +REGIME_ADJ
        signals["variance_ratio"] = vr
        signals["regime"] = regime
        signals["regime_adj"] = regime_adj
        p_yes += regime_adj * taper

        # ── Component 2: momentum-biased prior ──────────────────────────
        momentum_bias = +MOMENTUM_BIAS if above else -MOMENTUM_BIAS
        signals["momentum_bias"] = momentum_bias
        p_yes += momentum_bias * taper

        # ── Component 3: Kalshi contract velocity ───────────────────────
        velocity = contract_velocity(
            list(features.kalshi_price_history),
            lookback_samples=30,
            threshold_pct=0.02,
        )
        velocity_adj = 0.0
        if velocity == "rising":
            velocity_adj = +VELOCITY_ADJ
        elif velocity == "falling":
            velocity_adj = -VELOCITY_ADJ
        signals["velocity"] = velocity
        signals["velocity_adj"] = velocity_adj
        p_yes += velocity_adj * taper

        # ── Component 4: BV3 blend (secondary, 20% weight) ──────────────
        bv3 = bv3_p_yes(
            "BTC",
            features.current_price,
            features.strike,
            features.seconds_left,
        )
        signals["bv3_p_yes"] = bv3
        signals["p_yes_before_bv3_blend"] = p_yes

        if bv3 is not None:
            p_yes = (1.0 - BV3_BLEND_WEIGHT) * p_yes + BV3_BLEND_WEIGHT * bv3
            signals["bv3_blend_weight"] = BV3_BLEND_WEIGHT
        else:
            signals["bv3_blend_weight"] = 0.0

        p_yes = max(0.05, min(0.95, p_yes))

        signals["final_p_yes"] = p_yes
        signals["baseline_p_above"] = baseline_p_above
        signals["decision_mode"] = "evidence_based"
        return p_yes, signals

    def _decide_legacy_fallback(
        self,
        features: MarketFeatures,
        macro_event_active: bool = False,
    ) -> Decision:
        """
        Legacy rollback path — pure BV3 + legacy momentum/velocity,
        continuation-side-only. Preserved from Sections 2-3 as an escape
        hatch if the evidence-based logic underperforms.
        """
        skip_reason = check_skip(features, self.skip_config, macro_event_active)
        if skip_reason:
            return Decision(
                action="skip",
                side=None,
                p_model=0.5,
                reason=f"skip_layer: {skip_reason}",
                contributing_signals={"decision_mode": "continuation_only"},
            )

        try:
            import bot
        except ImportError:
            return Decision(
                action="skip",
                side=None,
                p_model=0.5,
                reason="legacy_fallback: bot module not importable",
                contributing_signals={"decision_mode": "continuation_only"},
            )

        abs_pct = abs(features.current_price - features.strike) / features.strike
        above = features.current_price > features.strike
        mins_left = features.seconds_left / 60.0

        raw_same_side = bot._win_prob_for_asset("BTC", abs_pct, mins_left)
        if raw_same_side is None:
            return Decision(
                action="skip",
                side=None,
                p_model=0.5,
                reason="legacy_fallback: bv3_lookup_failed",
                contributing_signals={"decision_mode": "continuation_only"},
            )

        mom_pct, mom_label = self._legacy_momentum(features)
        if mom_label == "bullish":
            mom_adj = +0.05 if above else -0.05
        elif mom_label == "bearish":
            mom_adj = +0.05 if not above else -0.05
        else:
            mom_adj = 0.0

        vel_signal = self._legacy_velocity(features)
        vel_adj = (
            +0.01 if vel_signal == "favorable"
            else (-0.01 if vel_signal == "unfavorable" else 0.0)
        )

        cal_scale = getattr(bot, "_brain_cal", {}).get("prob_scale", 1.0)
        combined = raw_same_side + mom_adj + vel_adj
        combined = 0.50 + (combined - 0.50) * cal_scale
        combined = max(0.10, min(0.997, combined))

        p_yes = combined if above else (1.0 - combined)
        calibrated_p_yes = self.calibrator.calibrate(p_yes)
        forced_side = "yes" if above else "no"

        ev_result = compute_bidirectional_ev(
            p_model=calibrated_p_yes,
            yes_ask_cents=features.yes_ask,
            no_ask_cents=features.no_ask,
            stake_dollars=self.stake_dollars,
            maker=self.maker,
        )
        forced_ev = ev_result.yes_ev if forced_side == "yes" else ev_result.no_ev

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
            "raw_p_yes": p_yes,
            "calibrated_p_yes": calibrated_p_yes,
            "forced_side": forced_side,
            "forced_ev": forced_ev,
            "yes_ev": ev_result.yes_ev,
            "no_ev": ev_result.no_ev,
            "decision_mode": "continuation_only",
        }

        if forced_ev < self.min_ev:
            return Decision(
                action="skip",
                side=None,
                p_model=calibrated_p_yes,
                reason=(
                    f"continuation-only {forced_side} EV {forced_ev:+.3f} "
                    f"below min_ev {self.min_ev:+.3f}"
                ),
                contributing_signals=signals,
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
            contributing_signals=signals,
            expected_value=forced_ev,
        )

    def _legacy_momentum(self, features: MarketFeatures) -> tuple[float, str]:
        """Port of bot.calculate_momentum() — threshold ±0.5% over 180s."""
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
        Port of legacy bot.contract_velocity() — threshold ±5% over full history.
        Preserved verbatim for A/B harness parity with legacy printer_brain().
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
        if delta < -0.05:
            return "favorable"
        if delta > 0.05:
            return "unfavorable"
        return "neutral"
