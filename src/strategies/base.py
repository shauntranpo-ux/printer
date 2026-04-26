"""
Abstract base class every per-market strategy inherits.

Decision pipeline (same for 15m and hourly, all modes):
  1. Skip layer      â€” price floor (35c); hourly also: spread, cold-start
  2. Octagon         â€” required direction signal; SKIP if unavailable/timeout/error
  3. Direction       â€” YES if octagon_prob > market_prob, NO if octagon_prob < market_prob
  4. EV check        â€” for Octagon's chosen direction; must meet configured minimum
  5. Vol ratio gate  â€” buffer durability (hourly only; 15m auto-pass)
  6. Confidence gate - p_ev (bv3_prob on 15m, oct_prob on hourly) >= threshold (76%)
  7. Price cap       â€” 76c ceiling (15m) or 80c ceiling (hourly)
  8. Trade
"""

from __future__ import annotations

from abc import ABC
from typing import Optional

from strategies.features import MarketFeatures, Decision
from strategies.skip_layer import check_skip, check_skip_15m, check_entry_price_cap, check_vol_ratio, SkipConfig
from strategies.ev import compute_bidirectional_ev
from strategies.calibration import AssetCalibrator


class BaseStrategy(ABC):

    def __init__(
        self,
        asset: str,
        skip_config: SkipConfig,
        min_ev: float,
        stake_dollars: float,
        calibrator: Optional[AssetCalibrator] = None,
        maker: bool = False,
        is_15m: bool = False,
        confidence_threshold: float = 0.0,  # BV3 fallback threshold (0-1); 0 = disabled
    ):
        self.asset = asset
        self.skip_config = skip_config
        self.min_ev = min_ev
        self.stake_dollars = stake_dollars
        self.calibrator = calibrator or AssetCalibrator(asset)
        self.maker = maker
        self.is_15m = is_15m
        self.confidence_threshold = confidence_threshold

    def compute_raw_p_model(
        self,
        features: MarketFeatures,
        baseline_p_above: float,
    ) -> tuple[float, dict]:
        """Legacy hook â€” no longer called in the decision pipeline."""
        return baseline_p_above, {}

    def decide(self, features: MarketFeatures, macro_event_active: bool = False) -> Decision:
        # Step 1: skip layer
        if self.is_15m:
            skip_reason = check_skip_15m(
                features,
                min_price_cents=self.skip_config.min_entry_price_cents,
            )
        else:
            skip_reason = check_skip(features, self.skip_config, macro_event_active)
        if skip_reason:
            return Decision(
                action="skip",
                side=None,
                p_model=0.5,
                reason=f"skip_layer: {skip_reason}",
            )

        # Step 2: market-implied probability from AMM prices
        _yes = features.yes_ask / 100.0
        _no = features.no_ask / 100.0
        _total = _yes + _no
        market_prob = _yes / _total if _total > 0 else 0.5

        # Step 3: Octagon â€” required direction signal
        from strategies.signals import octagon_client as _octagon
        oct_prob, _, oct_conf, oct_hit = _octagon.query(
            features.ticker, features.strike,
            features.yes_ask, features.no_ask,
            None, self.is_15m,
        )
        features.octagon_model_prob = oct_prob
        features.octagon_confidence = oct_conf
        features.octagon_cache_hit = oct_hit

        if oct_prob is None:
            features.octagon_direction_agrees = None
            # BV3 fallback: if confidence_threshold is set and bv3_prob is available,
            # use BV3 table direction when Octagon is down
            if self.confidence_threshold > 0 and features.bv3_prob is not None:
                bv3 = features.bv3_prob
                if bv3 >= self.confidence_threshold:
                    bv3_side = "yes"
                elif bv3 <= 1.0 - self.confidence_threshold:
                    bv3_side = "no"
                else:
                    bv3_side = None

                if bv3_side is not None:
                    ev = compute_bidirectional_ev(
                        p_model=bv3,
                        yes_ask_cents=features.yes_ask,
                        no_ask_cents=features.no_ask,
                        stake_dollars=self.stake_dollars,
                        maker=self.maker,
                    )
                    side_ev = ev.yes_ev if bv3_side == "yes" else ev.no_ev
                    entry_cents = features.yes_ask if bv3_side == "yes" else features.no_ask

                    if side_ev >= self.min_ev:
                        cap_reason = check_entry_price_cap(entry_cents, bv3_side, self.skip_config)
                        if not cap_reason:
                            return Decision(
                                action="trade",
                                side=bv3_side,
                                p_model=bv3,
                                reason=(
                                    f"{bv3_side} BV3-fallback EV={side_ev:+.3f} "
                                    f"(bv3={bv3:.3f} confâ‰¥{self.confidence_threshold:.0%})"
                                ),
                                contributing_signals={
                                    "octagon_direction": None,
                                    "octagon_model_prob": None,
                                    "octagon_market_prob": market_prob,
                                    "octagon_confidence": None,
                                    "octagon_cache_hit": False,
                                    "bv3_prob": bv3,
                                    "bv3_side": bv3_side,
                                    "yes_ev": ev.yes_ev,
                                    "no_ev": ev.no_ev,
                                    "entry_cents": entry_cents,
                                    "ev_pass": True,
                                    "vol_pass": True,
                                    "final_decision": "trade",
                                    "skip_reason": None,
                                    "decision_mode": "bv3_fallback",
                                },
                                expected_value=side_ev,
                            )

            return Decision(
                action="skip",
                side=None,
                p_model=market_prob,
                reason="octagon_unavailable",
                contributing_signals={
                    "octagon_direction": None,
                    "octagon_model_prob": None,
                    "octagon_market_prob": market_prob,
                    "octagon_confidence": oct_conf,
                    "octagon_cache_hit": oct_hit,
                    "ev_pass": False,
                    "vol_pass": True,
                    "final_decision": "skip",
                    "skip_reason": "octagon_unavailable",
                },
            )

        # Step 3 continued: Determine direction.
        # 15m: oct_prob is Octagon's P(YES=above strike) for this specific strike.
        # Compare to 0.5: >= 0.5 means Octagon thinks YES more likely → YES,
        # < 0.5 means Octagon thinks NO more likely → NO.
        # BV3 EV + confidence gates filter out low-conviction entries.
        if self.is_15m:
            oct_side = "yes" if oct_prob >= 0.5 else "no"
        else:
            if oct_prob > market_prob:
                oct_side = "yes"
            elif oct_prob < market_prob:
                oct_side = "no"
            else:
                features.octagon_direction_agrees = None
                return Decision(
                    action="skip",
                    side=None,
                    p_model=market_prob,
                    reason=(
                        f"octagon_neutral: model_prob={oct_prob:.3f} == "
                        f"market_prob={market_prob:.3f}"
                    ),
                    contributing_signals={
                        "octagon_direction": None,
                        "octagon_model_prob": oct_prob,
                        "octagon_market_prob": market_prob,
                        "octagon_confidence": oct_conf,
                        "octagon_cache_hit": oct_hit,
                        "ev_pass": False,
                        "vol_pass": True,
                        "final_decision": "skip",
                        "skip_reason": "octagon_neutral",
                    },
                )

        features.octagon_direction_agrees = True  # we follow the direction signal

        # Probability used for EV + confidence gates.
        # 15m: use BV3 empirical win rate (calibrated from real Kalshi binary outcomes).
        # Octagon's absolute probability is tuned for hourly/daily directional sentiment
        # and returns 0.78-0.91 for ALL 15m trades — rendering EV and confidence gates useless.
        # Hourly: use Octagon directly (better calibrated for longer timeframes).
        if self.is_15m and features.bv3_prob is not None:
            p_ev = features.bv3_prob
            p_ev_source = "bv3"
        else:
            p_ev = oct_prob
            p_ev_source = "octagon"

        # Step 4: EV gate using calibrated probability
        ev = compute_bidirectional_ev(
            p_model=p_ev,
            yes_ask_cents=features.yes_ask,
            no_ask_cents=features.no_ask,
            stake_dollars=self.stake_dollars,
            maker=self.maker,
        )
        side_ev = ev.yes_ev if oct_side == "yes" else ev.no_ev
        entry_cents = features.yes_ask if oct_side == "yes" else features.no_ask

        base_signals = {
            "octagon_direction": oct_side,
            "octagon_model_prob": oct_prob,
            "octagon_market_prob": market_prob,
            "octagon_confidence": oct_conf,
            "octagon_cache_hit": oct_hit,
            "p_ev": p_ev,
            "p_ev_source": p_ev_source,
            "bv3_prob": features.bv3_prob,
            "yes_ev": ev.yes_ev,
            "no_ev": ev.no_ev,
            "entry_cents": entry_cents,
        }

        if side_ev < self.min_ev:
            return Decision(
                action="skip",
                side=None,
                p_model=p_ev,
                reason=(
                    f"EV below threshold: {oct_side}_ev={side_ev:+.3f} "
                    f"< {self.min_ev:+.3f} (p_ev={p_ev:.3f} [{p_ev_source}])"
                ),
                contributing_signals={
                    **base_signals,
                    "ev_pass": False,
                    "vol_pass": True,
                    "final_decision": "skip",
                    "skip_reason": "ev_below_threshold",
                },
                expected_value=side_ev,
            )

        # Step 5: vol ratio gate (buffer durability; 15m markets auto-pass)
        vol_skip = check_vol_ratio(features, self.skip_config)
        if vol_skip:
            return Decision(
                action="skip",
                side=None,
                p_model=p_ev,
                reason=f"vol_ratio: {vol_skip}",
                contributing_signals={
                    **base_signals,
                    "ev_pass": True,
                    "vol_pass": False,
                    "final_decision": "skip",
                    "skip_reason": "vol_ratio",
                },
                expected_value=side_ev,
            )

        # Step 6: confidence gate — final entry confirmation using calibrated probability.
        # 15m: bv3_prob >= threshold means price is clearly beyond the strike.
        # Hourly: oct_prob >= threshold means Octagon has high conviction.
        # YES passes if p_ev >= threshold; NO passes if p_ev <= (1 - threshold).
        if self.confidence_threshold > 0:
            _ct = self.confidence_threshold
            _below = (oct_side == "yes" and p_ev < _ct)
            _above = (oct_side == "no"  and p_ev > 1.0 - _ct)
            if _below or _above:
                return Decision(
                    action="skip",
                    side=None,
                    p_model=p_ev,
                    reason=(
                        f"confidence_gate: {oct_side}_p_ev={p_ev:.3f} [{p_ev_source}] "
                        f"outside threshold {_ct:.0%}/{1.0-_ct:.0%}"
                    ),
                    contributing_signals={
                        **base_signals,
                        "ev_pass": True,
                        "vol_pass": True,
                        "final_decision": "skip",
                        "skip_reason": "confidence_gate",
                    },
                    expected_value=side_ev,
                )

        # Step 7: entry price cap (76c for 15m, 80c for hourly)
        cap_reason = check_entry_price_cap(entry_cents, oct_side, self.skip_config)
        if cap_reason:
            return Decision(
                action="skip",
                side=None,
                p_model=p_ev,
                reason=cap_reason,
                contributing_signals={
                    **base_signals,
                    "ev_pass": True,
                    "vol_pass": True,
                    "final_decision": "skip",
                    "skip_reason": "entry_price_cap",
                },
                expected_value=side_ev,
            )

        # Step 8: trade
        return Decision(
            action="trade",
            side=oct_side,
            p_model=p_ev,
            reason=(
                f"{oct_side} EV={side_ev:+.3f} "
                f"(octagon={oct_prob:.3f} market={market_prob:.3f})"
            ),
            contributing_signals={
                **base_signals,
                "ev_pass": True,
                "vol_pass": True,
                "final_decision": "trade",
                "skip_reason": None,
            },
            expected_value=side_ev,
        )
