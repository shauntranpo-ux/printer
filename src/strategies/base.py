"""
Abstract base class every per-market strategy inherits.

Decision pipeline (15m markets only):
  1. Market prob     -- Kalshi AMM implied probability (reference only)
  2. Direction       -- D3-hybrid ensemble vote (compute_15m_signal)
  3. EV check        -- calibrated BS p_yes
  4. Vol ratio gate  -- buffer durability
  5. Entry range     -- [20c, 76c)
  6. Trade
"""

from __future__ import annotations

import collections
import logging
from abc import ABC
from typing import Optional

from strategies.features import MarketFeatures, Decision
from strategies.skip_layer import check_entry_range, check_vol_ratio, SkipConfig
from strategies.ev import compute_bidirectional_ev
from strategies.calibration import AssetCalibrator

_log = logging.getLogger(__name__)
_BIAS_WINDOW  = 50   # rolling trade window for YES-bias check
_BIAS_LIMIT   = 0.80 # warn when rolling YES fraction exceeds this


class BaseStrategy(ABC):

    def __init__(
        self,
        asset: str,
        skip_config: SkipConfig,
        min_ev: float,
        stake_dollars: float,
        calibrator: Optional[AssetCalibrator] = None,
        maker: bool = False,
    ):
        self.asset = asset
        self.skip_config = skip_config
        self.min_ev = min_ev
        self.stake_dollars = stake_dollars
        self.calibrator = calibrator or AssetCalibrator(asset)
        self.maker = maker
        self._side_rolling: collections.deque = collections.deque(maxlen=_BIAS_WINDOW)

    def decide(self, features: MarketFeatures, macro_event_active: bool = False) -> Decision:
        # Step 1: market-implied probability from AMM prices (reference only)
        _yes = features.yes_ask / 100.0
        _no = features.no_ask / 100.0
        _total = _yes + _no
        market_prob = _yes / _total if _total > 0 else 0.5

        # Step 2: direction signal (D3-hybrid ensemble)
        from strategies.signals.fifteen_min_signal import compute_15m_signal
        result_15m = compute_15m_signal(features)
        if result_15m is None:
            return Decision(
                action="skip",
                side=None,
                p_model=market_prob,
                reason="fifteen_min_signal_insufficient_data",
                contributing_signals={
                    "signal_name": "d3_hybrid",
                    "market_prob": market_prob,
                    "ev_pass": False,
                    "vol_pass": True,
                    "final_decision": "skip",
                    "skip_reason": "fifteen_min_signal_insufficient_data",
                },
            )
        st_side, signal_raw_p_yes, vote_count = result_15m
        st = 1 if st_side == "yes" else -1
        features.supertrend_direction = st

        # Step 3: EV gate — calibrated BS p_yes
        p_ev = self.calibrator.calibrate(signal_raw_p_yes)
        ev = compute_bidirectional_ev(
            p_model=p_ev,  # P(YES wins); ev.py applies (1-p_model) for NO internally
            yes_ask_cents=features.yes_ask,
            no_ask_cents=features.no_ask,
            stake_dollars=self.stake_dollars,
            maker=self.maker,
        )
        side_ev = ev.yes_ev if st_side == "yes" else ev.no_ev
        entry_cents = features.yes_ask if st_side == "yes" else features.no_ask

        _signal_name = "d3_hybrid"
        base_signals = {
            "supertrend_direction": st,
            "supertrend_side": st_side,
            "market_prob": market_prob,
            "p_ev": p_ev,
            "p_ev_source": _signal_name,
            "yes_ev": ev.yes_ev,
            "no_ev": ev.no_ev,
            "entry_cents": entry_cents,
            "raw_p_yes": signal_raw_p_yes,
            "calibrated_p_yes": p_ev,
            "signal_name": _signal_name,
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

        # Step 4: vol ratio gate (buffer durability)
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

        # Step 5: entry range [20c, 76c)
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

        # Step 6: trade — update rolling bias guard before returning
        self._side_rolling.append(1 if st_side == "yes" else 0)
        if len(self._side_rolling) == _BIAS_WINDOW:
            yes_frac = sum(self._side_rolling) / _BIAS_WINDOW
            if yes_frac > _BIAS_LIMIT:
                _log.critical(
                    "[%s] YES-bias alert: %.0f%% YES in last %d trades "
                    "(signal=%s) — check for directional signal fault",
                    self.asset, yes_frac * 100, _BIAS_WINDOW, _signal_name,
                )

        return Decision(
            action="trade",
            side=st_side,
            p_model=p_ev if st_side == "yes" else 1.0 - p_ev,
            reason=(
                f"{st_side} {_signal_name} EV={side_ev:+.3f} "
                f"market={market_prob:.3f}"
            ),
            contributing_signals={
                **base_signals,
                "ev_pass": True,
                "vol_pass": True,
                "final_decision": "trade",
                "skip_reason": None,
                "decision_mode": _signal_name,
            },
            expected_value=side_ev,
        )
