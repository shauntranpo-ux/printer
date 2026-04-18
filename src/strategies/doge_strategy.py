"""
DOGEStrategy — selective, session-aware strategy for DOGE 15-min binaries.

Design philosophy: DOGE is hostile to pure TA. The strategy is conservative
by construction — aggressive skip rules, higher Min EV, session-adjusted
thresholds. Low trade frequency is expected.

Components:
1. Concurrent BTC beta signal (primary, smaller magnitude than SOL/ETH)
2. Momentum-biased prior (large-cap default, small magnitude)
3. Kalshi velocity feature
4. Idiosyncratic mode detector → SKIP
5. Session-aware Min EV multiplier
6. Base Min EV is higher (10% vs 8% for others) set in config

Not included: variance-ratio regime detector (unvalidated on DOGE),
volume-spike detector (same rationale), sentiment (out of scope).
"""

from __future__ import annotations
from typing import Optional
import math

from strategies.base import BaseStrategy
from strategies.features import MarketFeatures, Decision
from strategies.skip_layer import SkipConfig, check_skip_with_asset_hook
from strategies.calibration import AssetCalibrator

from strategies.signals.kalshi_velocity import contract_velocity
from strategies.signals.session_awareness import (
    current_session, session_min_ev_multiplier
)
from strategies.signals.idiosyncratic_detector import detect_idiosyncratic_mode
from strategies.signals.beta_cache import load_beta
from strategies.signals.btc_context import three_min_return


BETA_ADJ_MAX  = 0.08
MOMENTUM_BIAS = 0.015
VELOCITY_ADJ  = 0.02


class DOGEStrategy(BaseStrategy):
    def __init__(
        self,
        skip_config: SkipConfig,
        min_ev: float,
        stake_dollars: float,
        calibrator: Optional[AssetCalibrator] = None,
        maker: bool = False,
    ):
        super().__init__(
            asset="DOGE",
            skip_config=skip_config,
            min_ev=min_ev,
            stake_dollars=stake_dollars,
            calibrator=calibrator,
            maker=maker,
        )
        self.beta = load_beta("DOGE")

    def decide(self, features: MarketFeatures, macro_event_active: bool = False) -> Decision:
        is_idio, idio_signals = detect_idiosyncratic_mode(
            list(features.prices_60m),
            list(features.btc_prices_60m),
            beta=self.beta,
            sigma_threshold=2.5,
        )

        if is_idio:
            return Decision(
                action="skip",
                side=None,
                p_model=0.5,
                reason=f"idiosyncratic_mode: {idio_signals.get('reason', 'unknown')}",
                contributing_signals={**idio_signals, "idiosyncratic_skip": True},
            )

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

        session = current_session()
        mult = session_min_ev_multiplier(session)
        effective_min_ev = self.min_ev * mult

        return self._decide_after_skip(features, session, effective_min_ev, idio_signals)

    def _decide_after_skip(
        self,
        features: MarketFeatures,
        session: str,
        effective_min_ev: float,
        idio_signals: dict,
    ) -> Decision:
        from strategies.baseline import brownian_bridge_prob_above
        from strategies.ev import compute_bidirectional_ev

        baseline_p_above = brownian_bridge_prob_above(
            features.current_price,
            features.strike,
            features.seconds_left,
            features.realized_vol_1min or 0.001,
        )

        raw_p_yes, signals = self.compute_raw_p_model(features, baseline_p_above)
        signals["session"] = session
        signals["session_min_ev_multiplier"] = mult = effective_min_ev / self.min_ev
        signals["effective_min_ev"] = effective_min_ev
        signals.update(idio_signals)

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

        if ev_result.best_side is None or ev_result.best_ev < effective_min_ev:
            return Decision(
                action="skip",
                side=None,
                p_model=calibrated_p_yes,
                reason=(
                    f"EV below session-adjusted threshold: "
                    f"best={ev_result.best_ev:+.3f} "
                    f"(session={session}, min={effective_min_ev:+.3f})"
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
                f"(p_yes={calibrated_p_yes:.3f}, session={session})"
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
        above = features.current_price > features.strike

        btc_3m = three_min_return(features.btc_prices_60m)
        beta_adj = 0.0
        if btc_3m is not None:
            implied_doge_move = self.beta * btc_3m
            rv = features.realized_vol_1min or 0.004
            expected_remaining_move = rv * math.sqrt(max(1.0, features.seconds_left / 60.0))
            if expected_remaining_move > 0:
                nudge = (implied_doge_move / expected_remaining_move) * BETA_ADJ_MAX
                beta_adj = max(-BETA_ADJ_MAX, min(BETA_ADJ_MAX, nudge))
        signals["btc_3m_return"] = btc_3m
        signals["beta"] = self.beta
        signals["beta_adj"] = beta_adj
        p_yes += beta_adj

        base_bias = +MOMENTUM_BIAS if above else -MOMENTUM_BIAS
        signals["momentum_bias"] = base_bias
        p_yes += base_bias

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
        p_yes += velocity_adj

        p_yes = max(0.05, min(0.95, p_yes))
        signals["final_p_yes"] = p_yes
        return p_yes, signals

