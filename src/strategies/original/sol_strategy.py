"""
SOLStrategy — evidence-based strategy for SOL 15-min binaries.

Components (post-S1 update):
1. **PRIMARY:** S1 cross-venue funding-rate dispersion (Binance vs
   Hyperliquid) — captures positioning imbalance independently of price.
   Replaces the old high-beta BTC signal as the dominant directional driver.
2. Solana network health kill switch (fail-safe, skip if unhealthy)
3. Variance-ratio regime (SOL's own returns)
4. Reduced BTC-beta signal kept as a secondary "consensus" check at
   half its previous magnitude — unwound if the funding signal disagrees
   with it.
5. Final-2-minute exhaustion fade on extreme moves
6. Kalshi contract velocity

The funding-dispersion monitor must be refreshed by an async background
task in bot.py (call `await monitor.refresh()` ~ every 60 s).  Until that
wiring lands, `current_dispersion()` returns None and the funding signal
is silently zero — graceful degradation, not a hidden source of PnL.

Uses Brownian-bridge baseline adjusted by evidence signals.
BaseStrategy handles calibration, EV, and bidirectional side selection.
"""

from __future__ import annotations
import math
from typing import Optional

from strategies.base import BaseStrategy
from strategies.features import MarketFeatures, Decision
from strategies.skip_layer import SkipConfig, check_skip_with_asset_hook
from strategies.calibration import AssetCalibrator

from strategies.original.signals.rolling_beta import log_returns_from_prices
from strategies.original.signals.variance_ratio import variance_ratio, variance_ratio_to_regime
from strategies.original.signals.solana_health import check_solana_health
from strategies.original.signals.exhaustion_fade import exhaustion_fade_adjustment
from strategies.original.signals.kalshi_velocity import contract_velocity
from strategies.original.signals.beta_cache import load_beta
from strategies.original.signals.btc_context import three_min_return
from strategies.original.signals.taper import magnitude_taper
from strategies.original.signals.funding_dispersion import (
    FundingDispersionMonitor,
    funding_dispersion_adjustment,
    S1_FUNDING_ADJ_MAX,
)


# S1 funding signal is now primary; legacy BTC-beta path is halved so it
# acts as a sanity check rather than a driver.
BETA_ADJ_MAX   = 0.06
MOMENTUM_BIAS  = 0.02   # always-on continuation nudge (large-cap default)
REGIME_ADJ     = 0.03
VELOCITY_ADJ   = 0.02
EXHAUSTION_ADJ = 0.03
FUNDING_ADJ_MAX = S1_FUNDING_ADJ_MAX


class SOLStrategy(BaseStrategy):
    def __init__(
        self,
        skip_config: SkipConfig,
        min_ev: float,
        stake_dollars: float,
        calibrator: Optional[AssetCalibrator] = None,
        maker: bool = False,
        funding_monitor: Optional[FundingDispersionMonitor] = None,
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
        # Lazy-construct an empty monitor if the caller didn't inject one;
        # bot.py wires the live monitor + background refresh task.
        self.funding_monitor = funding_monitor or FundingDispersionMonitor("SOL")

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
        from strategies.original.baseline import brownian_bridge_prob_above
        from strategies.ev import compute_bidirectional_ev

        baseline_p_above = brownian_bridge_prob_above(
            features.current_price,
            features.strike,
            features.seconds_left,
            features.realized_vol_1min or 0.001,
        )

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

        # ── Component 1 (PRIMARY): S1 cross-venue funding dispersion ────
        dispersion = self.funding_monitor.current_dispersion()
        funding_adj, funding_info = funding_dispersion_adjustment(dispersion)
        signals.update(funding_info)
        signals["funding_adj"] = funding_adj
        p_yes += funding_adj * taper

        # ── Component 1b (secondary): half-magnitude BTC beta sanity ────
        # If funding implies down and BTC beta also implies down, conviction
        # is reinforced; if they disagree, halve BTC beta so the dominant
        # signal (funding) wins out.
        btc_3m = three_min_return(features.btc_prices_60m)
        beta_adj = 0.0
        if btc_3m is not None:
            implied_sol_move = self.beta * btc_3m
            rv = features.realized_vol_1min or 0.003
            expected_remaining_move = rv * math.sqrt(max(1.0, features.seconds_left / 60.0))
            if expected_remaining_move > 0:
                nudge = (implied_sol_move / expected_remaining_move) * BETA_ADJ_MAX
                beta_adj = max(-BETA_ADJ_MAX, min(BETA_ADJ_MAX, nudge))
            # Disagreement attenuator: same sign keeps full magnitude;
            # opposite signs cut the beta nudge in half.
            if funding_adj != 0.0 and (funding_adj * beta_adj) < 0:
                beta_adj *= 0.5
                signals["beta_adj_attenuated"] = True
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


