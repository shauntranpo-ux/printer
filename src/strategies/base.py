"""
Abstract base class every per-market strategy inherits.

Decision pipeline (same for 15m and hourly, all modes):
  1. Skip layer   — price floor (10c); hourly also: spread, cold-start, vol-ratio
  2. Octagon      — required direction signal; SKIP if unavailable/timeout/error
  3. Direction    — YES if octagon_prob > market_prob, NO if octagon_prob < market_prob
  4. EV check     — for Octagon's chosen direction; must meet configured minimum
  5. Price cap    — 76c ceiling (15m) or 80c ceiling (hourly)
  6. Trade
"""

from __future__ import annotations

from abc import ABC
from typing import Optional

from strategies.features import MarketFeatures, Decision
from strategies.skip_layer import check_skip, check_skip_15m, check_entry_price_cap, SkipConfig
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
        confidence_threshold: float = 0.0,  # accepted but unused — gate removed
    ):
        self.asset = asset
        self.skip_config = skip_config
        self.min_ev = min_ev
        self.stake_dollars = stake_dollars
        self.calibrator = calibrator or AssetCalibrator(asset)
        self.maker = maker
        self.is_15m = is_15m

    def compute_raw_p_model(
        self,
        features: MarketFeatures,
        baseline_p_above: float,
    ) -> tuple[float, dict]:
        """Legacy hook — no longer called in the decision pipeline."""
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

        # Step 3: Octagon — required direction signal
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

        # Step 4: Octagon determines direction
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

        features.octagon_direction_agrees = True  # we follow Octagon's direction

        # Step 5: EV for Octagon's chosen direction, using Octagon's model_prob
        ev = compute_bidirectional_ev(
            p_model=oct_prob,
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
            "yes_ev": ev.yes_ev,
            "no_ev": ev.no_ev,
            "entry_cents": entry_cents,
        }

        if side_ev < self.min_ev:
            return Decision(
                action="skip",
                side=None,
                p_model=oct_prob,
                reason=(
                    f"EV below threshold: {oct_side}_ev={side_ev:+.3f} "
                    f"< {self.min_ev:+.3f}"
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

        # Step 6: entry price cap (76c for 15m, 80c for hourly)
        cap_reason = check_entry_price_cap(entry_cents, oct_side, self.skip_config)
        if cap_reason:
            return Decision(
                action="skip",
                side=None,
                p_model=oct_prob,
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

        # Step 7: trade
        return Decision(
            action="trade",
            side=oct_side,
            p_model=oct_prob,
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
