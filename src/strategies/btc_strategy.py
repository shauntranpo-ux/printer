"""
BTCStrategy — B3: BTC time-of-day-conditioned order-book imbalance.

Plan source (Part 2.5): the most robust BTC microstructure result in
the literature is the diurnal liquidity pattern.  Trading order-book
imbalance (OBI) signals only when depth is high — and skipping the
21:00 UTC trough plus the funding-reset windows — turns a noisy
mean-revert signal into a positive-EV one.

Key sources:
- https://blog.amberdata.io/the-rhythm-of-liquidity-temporal-patterns-in-market-depth
- https://concretumgroup.com/seasonality-in-bitcoin-intraday-trend-trading/
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4080253

Honest scope: the bot only consumes top-of-book quotes, not L2 depth.
The OBI is approximated by the Kalshi binary-book imbalance (see
signals/btc_diurnal_obi.kalshi_book_obi).  The diurnal regime gating
is exact.

Components:
1. Diurnal regime classifier — peak / trough / neutral hour band
2. Funding-reset guard — skip +/- 5 min around 00:00, 08:00, 16:00 UTC
3. Kalshi-book OBI — directional pressure on the contract book
4. Final p_yes nudge magnitude is liquidity-conditioned (full at peak,
   half at neutral, zero at trough)
5. Kalshi velocity — same off-the-shelf signal used by the other strats
"""

from __future__ import annotations
from typing import Optional

from strategies.base import BaseStrategy
from strategies.features import MarketFeatures, Decision
from strategies.skip_layer import SkipConfig, check_skip_with_asset_hook
from strategies.calibration import AssetCalibrator

from strategies.signals.kalshi_velocity import contract_velocity
from strategies.signals.taper import magnitude_taper
from strategies.signals.btc_diurnal_obi import (
    current_btc_diurnal_band,
    is_funding_reset_window,
    kalshi_book_obi,
    b3_obi_adjustment,
)


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
    ):
        super().__init__(
            asset="BTC",
            skip_config=skip_config,
            min_ev=min_ev,
            stake_dollars=stake_dollars,
            calibrator=calibrator,
            maker=maker,
        )

    def decide(self, features: MarketFeatures, macro_event_active: bool = False) -> Decision:
        """
        BTC-specific skip layer: hard-skip during the diurnal trough
        (18-21 UTC) and inside funding-reset guard windows.  Per the
        Amberdata data set, the trough has 1.42x less depth than the
        peak; running OBI signals there is net-negative EV after fees.
        """
        band = current_btc_diurnal_band(features.timestamp)
        funding_reset = is_funding_reset_window(features.timestamp)

        if band == "trough":
            return Decision(
                action="skip",
                side=None,
                p_model=0.5,
                reason="b3_skip: diurnal trough (18-21 UTC, low depth)",
                contributing_signals={
                    "diurnal_band": band,
                    "funding_reset": funding_reset,
                },
            )
        if funding_reset:
            return Decision(
                action="skip",
                side=None,
                p_model=0.5,
                reason="b3_skip: funding-reset guard (+-5 min around 00/08/16 UTC)",
                contributing_signals={
                    "diurnal_band": band,
                    "funding_reset": funding_reset,
                },
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
                contributing_signals={
                    "diurnal_band": band,
                    "funding_reset": funding_reset,
                },
            )

        return self._decide_after_skip(features, band, funding_reset)

    def _decide_after_skip(
        self,
        features: MarketFeatures,
        band: str,
        funding_reset: bool,
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
        signals["diurnal_band"] = band
        signals["funding_reset"] = funding_reset

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
                reason=(
                    f"EV below threshold: best={ev.best_ev:+.3f} "
                    f"(min={self.min_ev:+.3f}, band={band})"
                ),
                contributing_signals=merged,
                expected_value=ev.best_ev,
            )

        return Decision(
            action="trade",
            side=ev.best_side,
            p_model=calibrated_p_yes,
            reason=(
                f"{ev.best_side} EV={ev.best_ev:+.3f} "
                f"(p_yes={calibrated_p_yes:.3f}, band={band})"
            ),
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
        band = current_btc_diurnal_band(features.timestamp)
        funding_reset = is_funding_reset_window(features.timestamp)

        # ── B3 OBI signal, liquidity-conditioned ────────────────────────
        obi = kalshi_book_obi(
            features.yes_bid,
            features.no_bid,
            features.yes_ask,
            features.no_ask,
        )
        obi_adj, obi_info = b3_obi_adjustment(
            obi=obi,
            band=band,
            funding_reset=funding_reset,
            obi_threshold=B3_OBI_THRESHOLD,
            adj_magnitude=B3_OBI_ADJ_MAX,
        )
        signals.update(obi_info)
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
