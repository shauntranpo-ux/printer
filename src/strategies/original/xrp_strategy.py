"""
XRPStrategy — decoupled, event-aware strategy for XRP 15-min binaries.

XRP behaves fundamentally differently from BTC/ETH/SOL. The research
establishes that XRP does NOT reliably follow BTC (Ozaydin 2021), is
predictable but from its own dynamics (Verma et al. 2022), and follows
the same intraday momentum-reversal patterns as BTC on own returns
(Wen et al. 2022). Live data confirmed the coupling assumption
systematically loses money (41.7% YES hit vs 62.5% NO hit).

Components:
1. Decoupled prior — BTC signal weight capped at 30%, dynamically
   adjusted by live correlation
2. Live correlation monitor — zero out BTC signal when decoupled
3. Kalshi velocity extreme event detector — proxy for news/volume spikes,
   switches to continuation mode
4. Variance-ratio regime detector on XRP's own returns
5. XRP/BTC ratio divergence (secondary, reduced weight)
6. Event calendar hard skip
"""

from __future__ import annotations
from typing import Optional
import math

from strategies.original.base import BaseStrategy
from strategies.features import MarketFeatures, Decision
from strategies.skip_layer import SkipConfig, check_skip_with_asset_hook
from strategies.calibration import AssetCalibrator

from strategies.original.signals.rolling_beta import log_returns_from_prices
from strategies.original.signals.variance_ratio import variance_ratio, variance_ratio_to_regime
from strategies.original.signals.correlation_monitor import (
    rolling_correlation, btc_signal_weight_from_correlation
)
from strategies.original.signals.ratio_divergence import ratio_z_score
from strategies.original.signals.kalshi_velocity import (
    contract_velocity, extreme_velocity_event
)
from strategies.original.signals.event_calendar import EventCalendar
from strategies.original.signals.session_clock import (
    is_decoupling_window,
    prior_session_return,
    is_event_day,
)
from strategies.original.signals.beta_cache import load_beta
from strategies.original.signals.btc_context import three_min_return
from strategies.original.signals.taper import magnitude_taper


BETA_ADJ_MAX_CAP           = 0.06
REGIME_ADJ                 = 0.04   # XRP's own regime gets higher weight
RATIO_ADJ_MAX              = 0.02
VELOCITY_ADJ               = 0.02
NEWS_MODE_CONTINUATION_ADJ = 0.06

# X3 — APAC decoupling + event continuation
APAC_DECOUPLING_CORR_MAX   = 0.35
APAC_PRIOR_RETURN_MIN_ABS  = 0.005   # 0.5% over the trailing-60m proxy
APAC_CONTINUATION_ADJ      = 0.05
APAC_EVENT_DAY_BOOST       = 0.02


class XRPStrategy(BaseStrategy):
    def __init__(
        self,
        skip_config: SkipConfig,
        min_ev: float,
        stake_dollars: float,
        calibrator: Optional[AssetCalibrator] = None,
        maker: bool = False,
        event_calendar: Optional[EventCalendar] = None,
    ):
        super().__init__(
            asset="XRP",
            skip_config=skip_config,
            min_ev=min_ev,
            stake_dollars=stake_dollars,
            calibrator=calibrator,
            maker=maker,
        )
        self.beta = load_beta("XRP")
        self.event_calendar = event_calendar or EventCalendar()

    def decide(self, features: MarketFeatures, macro_event_active: bool = False) -> Decision:
        event_active, event_reason = self.event_calendar.is_event_active()
        calendar_hook = (not event_active, event_reason)

        skip_reason = check_skip_with_asset_hook(
            features, self.skip_config, macro_event_active, calendar_hook
        )
        if skip_reason:
            return Decision(
                action="skip",
                side=None,
                p_model=0.5,
                reason=f"skip_layer: {skip_reason}",
                contributing_signals={
                    "event_calendar_active": event_active,
                    "event_reason": event_reason,
                },
            )

        return self._decide_after_skip(features)

    def _decide_after_skip(self, features: MarketFeatures) -> Decision:
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
                    f"(min={self.min_ev:+.3f})"
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

        btc_prices_60m = list(features.btc_prices_60m)

        # ── Step A: news-mode detector (switch-mode, evaluated first) ───
        is_news, news_direction = extreme_velocity_event(
            list(features.kalshi_price_history),
            lookback_samples=30,
            extreme_threshold_pct=0.05,
        )
        signals["news_mode"] = is_news
        signals["news_direction"] = news_direction

        news_mode_adj = 0.0
        if is_news:
            if news_direction == "up":
                news_mode_adj = +NEWS_MODE_CONTINUATION_ADJ
            elif news_direction == "down":
                news_mode_adj = -NEWS_MODE_CONTINUATION_ADJ
        signals["news_mode_adj"] = news_mode_adj
        p_yes += news_mode_adj * taper

        # ── Step B: dynamic BTC-signal weight from correlation ──────────
        correlation = rolling_correlation(
            list(features.prices_60m),
            btc_prices_60m,
            lookback_minutes=60,
        )
        signals["xrp_btc_correlation"] = correlation
        beta_weight = btc_signal_weight_from_correlation(
            correlation,
            decoupling_threshold=0.3,
            max_weight=BETA_ADJ_MAX_CAP,
        )
        signals["btc_signal_weight"] = beta_weight

        # ── Step C: BTC beta adjustment (weighted by correlation) ───────
        btc_3m = three_min_return(features.btc_prices_60m)
        beta_adj = 0.0
        if btc_3m is not None and beta_weight > 0:
            implied_xrp_move = self.beta * btc_3m
            rv = features.realized_vol_1min or 0.003
            expected_remaining_move = rv * math.sqrt(max(1.0, features.seconds_left / 60.0))
            if expected_remaining_move > 0:
                nudge = (implied_xrp_move / expected_remaining_move) * beta_weight
                beta_adj = max(-beta_weight, min(beta_weight, nudge))
        signals["btc_3m_return"] = btc_3m
        signals["beta"] = self.beta
        signals["beta_adj"] = beta_adj
        p_yes += beta_adj * taper

        # ── Step D: variance-ratio regime on XRP's own returns ──────────
        xrp_returns = log_returns_from_prices(list(features.prices_60m))
        vr = variance_ratio(xrp_returns, q=5)
        regime = variance_ratio_to_regime(vr)
        regime_adj = 0.0
        if regime == "momentum":
            regime_adj = +REGIME_ADJ if above else -REGIME_ADJ
        signals["variance_ratio"] = vr
        signals["regime"] = regime
        signals["regime_adj"] = regime_adj
        p_yes += regime_adj * taper

        # ── Step E: XRP/BTC ratio divergence (secondary) ────────────────
        z = ratio_z_score(
            list(features.prices_60m),
            btc_prices_60m,
            lookback_minutes=240,
        )
        ratio_adj = 0.0
        signals["ratio_z"] = z
        signals["ratio_adj"] = ratio_adj

        # ── Step F: Kalshi velocity (non-extreme) ───────────────────────
        velocity_adj = 0.0
        velocity_label = "suppressed_news_mode" if is_news else "neutral"
        if not is_news:
            velocity = contract_velocity(
                list(features.kalshi_price_history),
                lookback_samples=30,
                threshold_pct=0.02,
            )
            velocity_label = velocity
            if velocity == "rising":
                velocity_adj = +VELOCITY_ADJ
            elif velocity == "falling":
                velocity_adj = -VELOCITY_ADJ
        signals["velocity"] = velocity_label
        signals["velocity_adj"] = velocity_adj
        p_yes += velocity_adj * taper

        # ── Step G: X3 APAC decoupling + event continuation ─────────────
        # Trade prior-session direction continuation when XRP is decoupled
        # from BTC during the Asia-open or EU-peak windows. News-mode
        # already provides its own continuation nudge, so suppress this
        # signal when news_mode is active to avoid double-counting.
        apac_active, apac_label = is_decoupling_window(features.timestamp)
        signals["apac_window"] = apac_label or "off"
        apac_adj = 0.0
        if apac_active and not is_news:
            corr_for_apac = correlation
            prior_ret = prior_session_return(list(features.prices_60m))
            event_day = is_event_day(self.event_calendar, features.timestamp)
            signals["apac_correlation"] = corr_for_apac
            signals["apac_prior_return"] = prior_ret
            signals["apac_event_day"] = event_day
            decoupled = (
                corr_for_apac is not None
                and corr_for_apac < APAC_DECOUPLING_CORR_MAX
            )
            sufficient = (
                prior_ret is not None
                and abs(prior_ret) >= APAC_PRIOR_RETURN_MIN_ABS
            )
            if decoupled and sufficient:
                magnitude = APAC_CONTINUATION_ADJ + (
                    APAC_EVENT_DAY_BOOST if event_day else 0.0
                )
                apac_adj = magnitude if prior_ret > 0 else -magnitude
        signals["apac_adj"] = apac_adj
        p_yes += apac_adj * taper

        p_yes = max(0.05, min(0.95, p_yes))
        signals["final_p_yes"] = p_yes
        return p_yes, signals


