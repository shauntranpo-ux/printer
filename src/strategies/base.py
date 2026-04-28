"""
Abstract base class every per-market strategy inherits.

Decision pipeline (same for 15m and hourly, all modes):
  1. Skip layer      -- hourly only: spread, cold-start, seconds_left, deep-OTM (20c floor)
                        15m: no pre-filter; range enforced post-decision at step 6
  2. Market prob     -- Kalshi AMM implied probability (reference only)
  3. Supertrend      -- sole direction signal; SKIP if insufficient data
  4. EV check        -- p_ev=0.70 assumed probability; per-asset minimum
  5. Vol ratio gate  -- buffer durability; applies to all markets
  6. Confidence gate -- p_ev >= threshold (disabled by default; set in config)
  7. Entry range     -- [20c, 76c) for 15m, [20c, 80c) for hourly
  8. Trade
"""

from __future__ import annotations

from abc import ABC
from typing import Optional

from strategies.features import MarketFeatures, Decision
from strategies.skip_layer import check_skip, check_entry_range, check_vol_ratio, SkipConfig
from strategies.ev import compute_bidirectional_ev
from strategies.calibration import AssetCalibrator

_SUPERTREND_P_MODEL = 0.70  # assumed win probability when Supertrend fires


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
        confidence_threshold: float = 0.0,
        supertrend_atr_period: int = 10,
        supertrend_atr_multiplier: float = 4.0,
    ):
        self.asset = asset
        self.skip_config = skip_config
        self.min_ev = min_ev
        self.stake_dollars = stake_dollars
        self.calibrator = calibrator or AssetCalibrator(asset)
        self.maker = maker
        self.is_15m = is_15m
        self.confidence_threshold = confidence_threshold
        self.supertrend_atr_period = supertrend_atr_period
        self.supertrend_atr_multiplier = supertrend_atr_multiplier

    def compute_raw_p_model(
        self,
        features: MarketFeatures,
        baseline_p_above: float,
    ) -> tuple[float, dict]:
        """Legacy hook — no longer called in the decision pipeline."""
        return baseline_p_above, {}

    def decide(self, features: MarketFeatures, macro_event_active: bool = False) -> Decision:
        # Step 1: skip layer (hourly only; 15m range enforced post-decision in step 7)
        if not self.is_15m:
            skip_reason = check_skip(features, self.skip_config, macro_event_active)
            if skip_reason:
                return Decision(
                    action="skip",
                    side=None,
                    p_model=0.5,
                    reason=f"skip_layer: {skip_reason}",
                )

        # Step 2: market-implied probability from AMM prices (reference only)
        _yes = features.yes_ask / 100.0
        _no = features.no_ask / 100.0
        _total = _yes + _no
        market_prob = _yes / _total if _total > 0 else 0.5

        # Step 3: Supertrend direction
        from strategies.signals.supertrend import supertrend_direction
        st = supertrend_direction(
            features.prices_60m,
            self.supertrend_atr_period,
            self.supertrend_atr_multiplier,
        )
        features.supertrend_direction = st

        if st is None:
            return Decision(
                action="skip",
                side=None,
                p_model=market_prob,
                reason="supertrend_insufficient_data",
                contributing_signals={
                    "supertrend_direction": None,
                    "market_prob": market_prob,
                    "ev_pass": False,
                    "vol_pass": True,
                    "final_decision": "skip",
                    "skip_reason": "supertrend_insufficient_data",
                },
            )

        st_side = "yes" if st == 1 else "no"

        # Step 4: EV gate using fixed assumed probability
        p_ev = _SUPERTREND_P_MODEL
        # p(YES)=0.70 for YES direction; p(YES)=0.30 (=1-0.70) for NO direction
        p_model_for_ev = p_ev if st_side == "yes" else 1.0 - p_ev
        ev = compute_bidirectional_ev(
            p_model=p_model_for_ev,
            yes_ask_cents=features.yes_ask,
            no_ask_cents=features.no_ask,
            stake_dollars=self.stake_dollars,
            maker=self.maker,
        )
        side_ev = ev.yes_ev if st_side == "yes" else ev.no_ev
        entry_cents = features.yes_ask if st_side == "yes" else features.no_ask

        base_signals = {
            "supertrend_direction": st,
            "supertrend_side": st_side,
            "market_prob": market_prob,
            "p_ev": p_ev,
            "p_ev_source": "supertrend",
            "yes_ev": ev.yes_ev,
            "no_ev": ev.no_ev,
            "entry_cents": entry_cents,
            "raw_p_yes": p_ev if st_side == "yes" else 1.0 - p_ev,
        }

        if side_ev < self.min_ev:
            return Decision(
                action="skip",
                side=None,
                p_model=p_ev,
                reason=(
                    f"EV below threshold: {st_side}_ev={side_ev:+.3f} "
                    f"< {self.min_ev:+.3f} (p_ev={p_ev:.2f})"
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

        # Step 5: vol ratio gate (buffer durability)
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

        # Step 6: confidence gate (disabled by default; set confidence_threshold > 0 to enable)
        if self.confidence_threshold > 0:
            _ct = self.confidence_threshold
            _below = (st_side == "yes" and p_ev < _ct)
            _above = (st_side == "no"  and p_ev > 1.0 - _ct)
            if _below or _above:
                return Decision(
                    action="skip",
                    side=None,
                    p_model=p_ev,
                    reason=(
                        f"confidence_gate: {st_side}_p_ev={p_ev:.3f} "
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

        # Step 7: entry range ([20c, 76c) for 15m, [20c, 80c) for hourly)
        range_reason = check_entry_range(entry_cents, st_side, self.skip_config)
        if range_reason:
            return Decision(
                action="skip",
                side=None,
                p_model=p_ev,
                reason=range_reason,
                contributing_signals={
                    **base_signals,
                    "ev_pass": True,
                    "vol_pass": True,
                    "final_decision": "skip",
                    "skip_reason": "entry_range",
                },
                expected_value=side_ev,
            )

        # Step 8: trade
        return Decision(
            action="trade",
            side=st_side,
            p_model=p_ev,
            reason=(
                f"{st_side} supertrend={st} EV={side_ev:+.3f} "
                f"market={market_prob:.3f}"
            ),
            contributing_signals={
                **base_signals,
                "ev_pass": True,
                "vol_pass": True,
                "final_decision": "trade",
                "skip_reason": None,
                "decision_mode": "supertrend",
            },
            expected_value=side_ev,
        )
