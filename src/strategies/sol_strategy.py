"""
SOLStrategy — evidence-based strategy for SOL 15-min binaries.

Components:
1. High-beta concurrent-BTC signal (primary, ~1.7x beta default)
2. Momentum-biased prior (large-cap continuation — Grobys & Sapkota 2021)
3. Solana network health kill switch (fail-safe, skip if unhealthy)
4. Final-2-minute exhaustion fade on extreme moves

Uses market-implied probability baseline (Kalshi AMM price) adjusted by evidence signals.
BaseStrategy handles calibration, EV, and bidirectional side selection.
"""

from __future__ import annotations
import math
from typing import Optional

from strategies.base import BaseStrategy
from strategies.features import MarketFeatures, Decision
from strategies.skip_layer import SkipConfig, check_skip_with_asset_hook
from strategies.calibration import AssetCalibrator

from strategies.signals.rolling_beta import log_returns_from_prices
from strategies.signals.variance_ratio import variance_ratio, variance_ratio_to_regime
from strategies.signals.solana_health import check_solana_health
from strategies.signals.exhaustion_fade import exhaustion_fade_adjustment
from strategies.signals.kalshi_velocity import contract_velocity
from strategies.signals.beta_cache import load_beta
from strategies.signals.btc_context import three_min_return
from strategies.signals.taper import magnitude_taper


BETA_ADJ_MAX   = 0.12   # larger than ETH's (SOL moves harder)
MOMENTUM_BIAS  = 0.02   # always-on continuation nudge (large-cap default)
REGIME_ADJ     = 0.03
VELOCITY_ADJ   = 0.02
EXHAUSTION_ADJ = 0.03


class SOLStrategy(BaseStrategy):
    def __init__(
        self,
        skip_config: SkipConfig,
        min_ev: float,
        stake_dollars: float,
        calibrator: Optional[AssetCalibrator] = None,
        maker: bool = False,
    ):
        super().__init__(
            asset="SOL",
            skip_config=skip_config,
            min_ev=min_ev,
            stake_dollars=stake_dollars,
            calibrator=calibrator,
            maker=maker,
        )
        self.beta = load_beta("SOL")

    def decide(self, features: MarketFeatures, macro_event_active: bool = False) -> Decision:
        """
        Override BaseStrategy.decide() to inject Solana network health check
        into the skip layer before any signal computation.
        """
        is_healthy, health_reason = check_solana_health()

        skip_reason = check_skip_with_asset_hook(
            features, self.skip_config, macro_event_active,
            asset_hook_result=(is_healthy, health_reason),
        )
        if skip_reason:
            return Decision(
                action="skip",
                side=None,
                p_model=0.5,
                reason=f"skip_layer: {skip_reason}",
                contributing_signals={
                    "solana_healthy": is_healthy,
                    "solana_health_reason": health_reason,
                },
            )

        return self._decide_after_skip(features)

    def _decide_after_skip(self, features: MarketFeatures) -> Decision:
        """Full decision pipeline after the skip layer has passed."""
        from strategies.ev import compute_bidirectional_ev

        _yes = features.yes_ask / 100.0
        _no = features.no_ask / 100.0
        _total = _yes + _no
        baseline_p_above = _yes / _total if _total > 0 else 0.5

        raw_p_yes, signals = self.compute_raw_p_model(features, baseline_p_above)
        calibrated_p_yes = self.calibrator.calibrate(raw_p_yes)

        ev_result = compute_bidirectional_ev(
            p_model=calibrated_p_yes,
            yes_ask_cents=features.yes_ask,
            no_ask_cents=features.no_ask,
            stake_dollars=self.stake_dollars,
            maker=self.maker,
        )

        merged_signals = {
            **signals,
            "baseline_p_above": baseline_p_above,
            "raw_p_yes": raw_p_yes,
            "calibrated_p_yes": calibrated_p_yes,
            "yes_ev": ev_result.yes_ev,
            "no_ev": ev_result.no_ev,
        }

        if ev_result.best_side is None or ev_result.best_ev < self.min_ev:
            return Decision(
                action="skip",
                side=None,
                p_model=calibrated_p_yes,
                reason=(
                    f"EV below threshold: best={ev_result.best_ev:+.3f} "
                    f"(yes={ev_result.yes_ev:+.3f} no={ev_result.no_ev:+.3f} "
                    f"min={self.min_ev:+.3f})"
                ),
                contributing_signals=merged_signals,
                expected_value=ev_result.best_ev,
            )

        return Decision(
            action="trade",
            side=ev_result.best_side,
            p_model=calibrated_p_yes,
            reason=(
                f"{ev_result.best_side} EV={ev_result.best_ev:+.3f} "
                f"(p_yes={calibrated_p_yes:.3f})"
            ),
            contributing_signals=merged_signals,
            expected_value=ev_result.best_ev,
        )

    def compute_raw_p_model(
        self,
        features: MarketFeatures,
        baseline_p_above: float,
    ) -> tuple[float, dict]:
        signals: dict = {}
        p_yes = baseline_p_above
        taper = magnitude_taper(baseline_p_above)
        above = features.current_price > features.strike

        # ── Component 1: BTC beta signal ────────────────────────────────
        btc_3m = three_min_return(features.btc_prices_60m)

        beta_adj = 0.0
        if btc_3m is not None:
            implied_sol_move = self.beta * btc_3m
            rv = features.realized_vol_1min or 0.003
            expected_remaining_move = rv * math.sqrt(max(1.0, features.seconds_left / 60.0))
            if expected_remaining_move > 0:
                nudge = (implied_sol_move / expected_remaining_move) * BETA_ADJ_MAX
                beta_adj = max(-BETA_ADJ_MAX, min(BETA_ADJ_MAX, nudge))
        signals["btc_3m_return"] = btc_3m
        signals["beta"] = self.beta
        signals["beta_adj"] = beta_adj
        p_yes += beta_adj * taper

        # ── Component 2: momentum-biased prior + regime detector ────────
        base_bias = +MOMENTUM_BIAS if above else -MOMENTUM_BIAS
        signals["momentum_bias"] = base_bias

        sol_returns = log_returns_from_prices(list(features.prices_60m))
        vr = variance_ratio(sol_returns, q=5)
        regime = variance_ratio_to_regime(vr)
        regime_adj = 0.0
        if regime == "momentum":
            regime_adj = +REGIME_ADJ if above else -REGIME_ADJ
        elif regime == "reversion":
            regime_adj = -REGIME_ADJ if above else +REGIME_ADJ
        signals["variance_ratio"] = vr
        signals["regime"] = regime
        signals["regime_adj"] = regime_adj
        p_yes += (base_bias + regime_adj) * taper

        # ── Component 3: Kalshi velocity ────────────────────────────────
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

        # ── Component 4: exhaustion fade ────────────────────────────────
        exh_adj, exh_signals = exhaustion_fade_adjustment(
            features.prices_1m,
            features.realized_vol_1min,
            features.seconds_left,
            adj_magnitude=EXHAUSTION_ADJ,
        )
        signals.update(exh_signals)
        signals["exhaustion_adj"] = exh_adj
        p_yes += exh_adj * taper

        p_yes = max(0.05, min(0.95, p_yes))
        signals["final_p_yes"] = p_yes
        return p_yes, signals

