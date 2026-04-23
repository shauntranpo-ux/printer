"""
MidWindowStrategy — ETH hourly market entry at t=15min using dual cross=0 signal.

Mechanism: At 15 minutes into the hourly window, if BOTH ETH AND BTC have been on
the same side of their respective strikes without ever crossing back (cross_count=0),
the market significantly underprices the probability of continuation.

Why this works: Kalshi's BB pricing model treats ETH and BTC independently and
assumes historical volatility. When both assets are in a clean directional trend
(no strike crossings in either), it's a macro regime signal the BB model can't price.
The joint "no crossing" condition is unpriced by the market.

OOS TEST 2024-2026 (t=10min, ETH_dist>=0.30%, ETH_cross=0 AND BTC_cross=0):
  N=1,128/119.3weeks = 9.5/wk   WR=83.5%   entry=79.2c   EV=+$1.14/trade
  EV/wk at $25 stake: $10.8/wk

TRAIN 2022-2023 (same signal):
  WR=84.6%  entry=82.6c  EV=+$0.40/trade  (consistent across both regimes)

Designed to fire BEFORE DwellWindowStrategy (t=30-42min) and LateWindowStrategy (t>=45min).
This strategy takes the cleanest windows early; remaining windows fall through to Dwell/Late.
"""

from __future__ import annotations
from typing import Optional

from strategies.base import BaseStrategy
from strategies.calibration import AssetCalibrator
from strategies.features import MarketFeatures, Decision
from strategies.skip_layer import SkipConfig, check_entry_price_cap


MIN_ELAPSED_SEC  = 550     # fire at ~t=9.2min (catch 10-min eval point)
MAX_ELAPSED_SEC  = 650     # stop at ~t=10.8min
MIN_DIST_PCT     = 0.30    # ETH must be >=0.30% from strike (signal, not price)
SKIP_HOURS_UTC   = {12, 13}


class MidWindowStrategy(BaseStrategy):
    """
    ETH hourly entries at t=15min using ETH+BTC dual cross=0 signal.
    Both ETH and BTC must have never crossed their respective window-open strikes.
    """

    def __init__(
        self,
        asset: str,
        skip_config: SkipConfig,
        stake_dollars: float,
        calibrator: Optional[AssetCalibrator] = None,
    ):
        super().__init__(
            asset=asset,
            skip_config=skip_config,
            min_ev=0.0,
            stake_dollars=stake_dollars,
            calibrator=calibrator,
        )

    def compute_raw_p_model(
        self,
        features: MarketFeatures,
        baseline_p_above: float,
    ) -> tuple[float, dict]:
        return baseline_p_above, {}  # decide() is overridden

    def _cross_count(self, price_series, strike: float) -> int:
        """Count how many times prices crossed the strike level."""
        if len(price_series) < 2:
            return 0
        above = [p > strike for p in price_series]
        return sum(1 for i in range(len(above) - 1) if above[i] != above[i + 1])

    def _eth_window_prices(self, features: MarketFeatures) -> list[float]:
        """ETH prices from window start to now."""
        window_start = features.timestamp - features.elapsed_seconds
        return [p for ts, p in list(features.prices_60m) if ts >= window_start]

    def _btc_window_prices_and_strike(
        self, features: MarketFeatures
    ) -> tuple[list[float], Optional[float]]:
        """
        BTC prices from window start to now, and BTC strike (price at window start).
        Returns (prices, strike) or ([], None) if insufficient data.
        """
        window_start = features.timestamp - features.elapsed_seconds
        btc_prices_list = list(features.btc_prices_60m)
        if not btc_prices_list:
            return [], None

        # BTC price closest to window start = BTC strike for this window
        window_prices = [(ts, p) for ts, p in btc_prices_list if ts >= window_start]
        if len(window_prices) < 3:
            return [], None

        # Strike = first price observed at or after window start
        btc_strike = window_prices[0][1]
        return [p for _, p in window_prices], btc_strike

    def _utc_hour(self, features: MarketFeatures) -> int:
        import datetime
        return datetime.datetime.fromtimestamp(
            features.timestamp, tz=datetime.timezone.utc
        ).hour

    def decide(self, features: MarketFeatures, macro_event_active: bool = False) -> Decision:
        if features.seconds_left < self.skip_config.min_seconds_left:
            return Decision(
                action="skip", side=None, p_model=0.5,
                reason=f"too_close: {features.seconds_left:.0f}s left",
            )

        if macro_event_active:
            return Decision(action="skip", side=None, p_model=0.5, reason="macro_event")

        if not (MIN_ELAPSED_SEC <= features.elapsed_seconds <= MAX_ELAPSED_SEC):
            return Decision(
                action="skip", side=None, p_model=0.5,
                reason=f"outside_mid_window: {features.elapsed_seconds/60:.1f}min",
            )

        if self._utc_hour(features) in SKIP_HOURS_UTC:
            return Decision(
                action="skip", side=None, p_model=0.5,
                reason=f"skip_hour: {self._utc_hour(features):02d}:00 UTC",
            )

        eth_prices = self._eth_window_prices(features)
        if len(eth_prices) < 5:
            return Decision(action="skip", side=None, p_model=0.5, reason="insufficient_eth_data")

        eth_cross = self._cross_count(eth_prices, features.strike)
        if eth_cross > 0:
            return Decision(
                action="skip", side=None, p_model=0.5,
                reason=f"eth_crossed: {eth_cross} crossings in first 15min",
            )

        eth_itm = eth_prices[-1] > features.strike

        # BTC confirmation: BTC must also have 0 crossings
        btc_prices, btc_strike = self._btc_window_prices_and_strike(features)
        if not btc_prices or btc_strike is None:
            return Decision(action="skip", side=None, p_model=0.5, reason="no_btc_data")

        btc_cross = self._cross_count(btc_prices, btc_strike)
        if btc_cross > 0:
            return Decision(
                action="skip", side=None, p_model=0.5,
                reason=f"btc_crossed: {btc_cross} crossings in first 15min",
            )

        btc_itm = btc_prices[-1] > btc_strike

        # Require ETH and BTC to be on the SAME side (correlated macro trend)
        if eth_itm != btc_itm:
            return Decision(
                action="skip", side=None, p_model=0.5,
                reason=f"diverged: ETH={'ITM' if eth_itm else 'OTM'} BTC={'ITM' if btc_itm else 'OTM'}",
            )

        eth_dist_pct = abs(eth_prices[-1] - features.strike) / features.strike * 100.0
        if eth_dist_pct < MIN_DIST_PCT:
            return Decision(
                action="skip", side=None, p_model=0.5,
                reason=f"dist_too_small: {eth_dist_pct:.3f}% < {MIN_DIST_PCT:.2f}% required",
            )

        side        = "yes" if eth_itm else "no"
        entry_cents = features.yes_ask if eth_itm else features.no_ask

        # Entry price cap — reject at 80c+ (fee drag)
        cap_reason = check_entry_price_cap(entry_cents, side, self.skip_config)
        if cap_reason:
            return Decision(
                action="skip", side=None, p_model=0.5,
                reason=cap_reason,
            )

        signals = {
            "eth_cross":    eth_cross,
            "btc_cross":    btc_cross,
            "eth_itm":      eth_itm,
            "btc_itm":      btc_itm,
            "eth_dist_pct": round(eth_dist_pct, 3),
            "elapsed_min":  round(features.elapsed_seconds / 60, 1),
            "entry_cents":  entry_cents,
        }

        return Decision(
            action="trade",
            side=side,
            p_model=0.835 if eth_itm else (1.0 - 0.835),  # P(YES wins); WR=83.5% OOS
            reason=(
                f"mid_{side.upper()}: ETH+BTC cross=0 dist={eth_dist_pct:.2f}% at t=10min "
                f"entry={entry_cents:.0f}c elapsed={features.elapsed_seconds/60:.0f}min"
            ),
            contributing_signals=signals,
            expected_value=0.046,   # ~$1.14/$25 at avg 79.2c entry
        )
