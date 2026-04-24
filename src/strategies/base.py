"""
Abstract base class every per-market strategy inherits.

Concrete strategies override only `compute_raw_p_model()`. Everything else
is shared infrastructure.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from strategies.features import MarketFeatures, Decision
from strategies.skip_layer import check_skip, check_skip_15m, check_entry_price_cap, SkipConfig
from strategies.ev import compute_bidirectional_ev
from strategies.calibration import AssetCalibrator


class BaseStrategy(ABC):
    """
    Every per-asset strategy inherits this.

    The decide() method receives raw features and returns a Decision.
    The base class handles:
      - skip layer (vol gate + pre-strategy filters)
      - baseline computation (market-implied probability from Kalshi AMM price)
      - calibration of p_model
      - bidirectional EV calculation
      - confidence threshold check
      - entry price cap
      - Octagon AI confirmation gate

    Concrete strategies override `compute_raw_p_model(features, baseline)`
    which returns the strategy's raw probability estimate. The base class
    handles the rest.
    """

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
    ):
        self.asset = asset
        self.skip_config = skip_config
        self.min_ev = min_ev
        self.stake_dollars = stake_dollars
        self.calibrator = calibrator or AssetCalibrator(asset)
        self.maker = maker
        self.is_15m = is_15m
        self.confidence_threshold = confidence_threshold

    @abstractmethod
    def compute_raw_p_model(
        self,
        features: MarketFeatures,
        baseline_p_above: float,
    ) -> tuple[float, dict]:
        """
        Subclasses implement this.

        Args:
            features: all available market features
            baseline_p_above: market-implied P(close above strike), derived from
                             Kalshi AMM ask prices: yes_ask / (yes_ask + no_ask)

        Returns:
            (p_model_yes, contributing_signals)
            where p_model_yes is P(YES wins), i.e. P(close > strike)
            contributing_signals is a dict of signal_name -> value for logging
        """
        ...

    def decide(self, features: MarketFeatures, macro_event_active: bool = False) -> Decision:
        """
        Full decision pipeline. Strategies don't override this.
        """
        # Step 1: skip layer
        # 15m markets use a minimal gate (price floor only); hourly uses full stack.
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

        # Step 2: baseline probability — use market-implied probability.
        # The Kalshi AMM already prices in price distance vs strike and vol.
        # Our signals should be the only source of edge, not a physics model
        # that disagrees with the market on information it already has.
        _yes = features.yes_ask / 100.0
        _no = features.no_ask / 100.0
        _total = _yes + _no
        baseline_p_above = _yes / _total if _total > 0 else 0.5

        # Step 3: strategy's raw p_model (P(yes wins) = P(close > strike))
        raw_p_yes, signals = self.compute_raw_p_model(features, baseline_p_above)

        # Step 4: calibrate
        calibrated_p_yes = self.calibrator.calibrate(raw_p_yes)

        # Step 5: bidirectional EV
        ev = compute_bidirectional_ev(
            p_model=calibrated_p_yes,
            yes_ask_cents=features.yes_ask,
            no_ask_cents=features.no_ask,
            stake_dollars=self.stake_dollars,
            maker=self.maker,
        )

        # Step 6: EV threshold check
        if ev.best_side is None or ev.best_ev < self.min_ev:
            return Decision(
                action="skip",
                side=None,
                p_model=calibrated_p_yes,
                reason=(
                    f"EV below threshold: best_ev={ev.best_ev:+.3f} "
                    f"(yes={ev.yes_ev:+.3f}, no={ev.no_ev:+.3f}, min={self.min_ev:+.3f})"
                ),
                contributing_signals={
                    **signals,
                    "baseline_p_above": baseline_p_above,
                    "raw_p_yes": raw_p_yes,
                    "calibrated_p_yes": calibrated_p_yes,
                    "yes_ev": ev.yes_ev,
                    "no_ev": ev.no_ev,
                },
                expected_value=ev.best_ev,
            )

        # Step 6.5: confidence threshold check
        if self.confidence_threshold > 0:
            win_prob = calibrated_p_yes if ev.best_side == "yes" else (1.0 - calibrated_p_yes)
            if win_prob < self.confidence_threshold:
                return Decision(
                    action="skip",
                    side=None,
                    p_model=calibrated_p_yes,
                    reason=(
                        f"confidence below threshold: win_prob={win_prob:.3f} "
                        f"< {self.confidence_threshold:.3f} ({ev.best_side} side)"
                    ),
                    contributing_signals={
                        **signals,
                        "baseline_p_above": baseline_p_above,
                        "raw_p_yes": raw_p_yes,
                        "calibrated_p_yes": calibrated_p_yes,
                        "yes_ev": ev.yes_ev,
                        "no_ev": ev.no_ev,
                        "win_prob": win_prob,
                    },
                    expected_value=ev.best_ev,
                )

        # Step 6.75: entry price cap — reject trades at or above max_entry_price_cents (fee drag)
        _entry_cents = features.yes_ask if ev.best_side == "yes" else features.no_ask
        _cap_reason = check_entry_price_cap(_entry_cents, ev.best_side, self.skip_config)
        if _cap_reason:
            return Decision(
                action="skip",
                side=None,
                p_model=calibrated_p_yes,
                reason=_cap_reason,
                contributing_signals={
                    **signals,
                    "baseline_p_above": baseline_p_above,
                    "raw_p_yes": raw_p_yes,
                    "calibrated_p_yes": calibrated_p_yes,
                    "yes_ev": ev.yes_ev,
                    "no_ev": ev.no_ev,
                    "entry_cents": _entry_cents,
                },
                expected_value=ev.best_ev,
            )

        # Step 6.8: Octagon AI confirmation gate
        # Falls through (allows trade) on API error, timeout, or missing key.
        from strategies.signals import octagon_client as _octagon
        _oct_prob, _oct_agrees, _oct_conf, _oct_hit = _octagon.query(
            features.ticker, features.strike,
            features.yes_ask, features.no_ask,
            ev.best_side, self.is_15m,
        )
        features.octagon_model_prob       = _oct_prob
        features.octagon_direction_agrees = _oct_agrees
        features.octagon_confidence       = _oct_conf
        features.octagon_cache_hit        = _oct_hit
        signals.update({
            "octagon_model_prob":       _oct_prob,
            "octagon_direction_agrees": _oct_agrees,
            "octagon_confidence":       _oct_conf,
            "octagon_cache_hit":        _oct_hit,
        })

        if _oct_prob is not None:
            _oct_signals = {
                **signals,
                "baseline_p_above": baseline_p_above,
                "raw_p_yes": raw_p_yes,
                "calibrated_p_yes": calibrated_p_yes,
                "yes_ev": ev.yes_ev,
                "no_ev": ev.no_ev,
            }
            if self.is_15m:
                # Skip only when direction disagrees AND confidence is meaningful
                if _oct_agrees is False and _oct_conf != "low":
                    return Decision(
                        action="skip",
                        side=None,
                        p_model=calibrated_p_yes,
                        reason=f"octagon_veto: conf={_oct_conf} direction_disagrees",
                        contributing_signals=_oct_signals,
                        expected_value=ev.best_ev,
                    )
            else:
                # Hourly: only trade on high/very_high confidence + direction agrees
                if not (_oct_agrees is True and _oct_conf in ("high", "very_high")):
                    return Decision(
                        action="skip",
                        side=None,
                        p_model=calibrated_p_yes,
                        reason=f"octagon_veto: conf={_oct_conf} agrees={_oct_agrees}",
                        contributing_signals=_oct_signals,
                        expected_value=ev.best_ev,
                    )

        # Step 7: trade decision
        return Decision(
            action="trade",
            side=ev.best_side,
            p_model=calibrated_p_yes,
            reason=(
                f"{ev.best_side} EV={ev.best_ev:+.3f} "
                f"(p_model={calibrated_p_yes:.3f}, "
                f"yes_ask={features.yes_ask:.0f}c, no_ask={features.no_ask:.0f}c)"
            ),
            contributing_signals={
                **signals,
                "baseline_p_above": baseline_p_above,
                "raw_p_yes": raw_p_yes,
                "calibrated_p_yes": calibrated_p_yes,
                "yes_ev": ev.yes_ev,
                "no_ev": ev.no_ev,
                "entry_cents": features.yes_ask if ev.best_side == "yes" else features.no_ask,
            },
            expected_value=ev.best_ev,
        )
