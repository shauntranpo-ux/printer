"""
EarlyWindowStrategy — BTC momentum signal for ETH hourly binary markets.

Mechanism: When BTC moves 0.5%+ in 10 minutes (t=10-20min into the hourly window),
ETH Kalshi hasn't fully repriced yet. The AMM lags spot by 60-120 seconds and the
market-implied probability is stale. We follow BTC direction.

Validated out-of-sample (TEST 2024-2026):
  WR=73.5%  AvgEntry=69.5c  EV=+$0.87/trade at $25 stake
  ~5 trades/week

Designed to run alongside LateWindowStrategy:
  - EarlyWindow fires at t=10-20min on BTC momentum (entries ~65-75c)
  - LateWindow fires at t>=45min on persistence bias (entries ~90-97c)
  - Bot-level position tracking prevents double-trading a window
"""

from __future__ import annotations
from typing import Optional

from strategies.base import BaseStrategy
from strategies.calibration import AssetCalibrator
from strategies.features import MarketFeatures, Decision
from strategies.skip_layer import SkipConfig


MIN_ELAPSED_SEC   = 10 * 60    # start at t=10min
MAX_ELAPSED_SEC   = 20 * 60    # stop at t=20min (late window takes over at t=45)
BTC_SIGNAL_PCT    = 0.5        # |BTC 10-min return| threshold
BTC_LOOKBACK_SEC  = 10 * 60    # 10-minute lookback for BTC return


class EarlyWindowStrategy(BaseStrategy):
    """
    ETH early-window entries using BTC 10-minute momentum.
    Fires at t=10-20min when BTC makes a strong directional move.
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
        return baseline_p_above, {}  # stub — decide() is overridden

    def _btc_10m_return(self, features: MarketFeatures) -> Optional[float]:
        """Return BTC % change over the last 10 minutes. None if data insufficient."""
        prices = list(features.btc_prices_60m)  # list of (timestamp, price) tuples
        if len(prices) < 5:
            return None
        now_ts  = prices[-1][0]
        cutoff  = now_ts - BTC_LOOKBACK_SEC
        earlier = [(ts, p) for ts, p in prices if ts <= cutoff]
        if not earlier:
            return None
        p_start = earlier[-1][1]   # latest price at or before cutoff
        p_end   = prices[-1][1]    # current BTC price
        if p_start <= 0:
            return None
        return (p_end - p_start) / p_start * 100.0

    def decide(self, features: MarketFeatures, macro_event_active: bool = False) -> Decision:
        # Hard block: too close to expiry
        if features.seconds_left < self.skip_config.min_seconds_left:
            return Decision(
                action="skip",
                side=None,
                p_model=0.5,
                reason=f"too_close: {features.seconds_left:.0f}s left",
            )

        if macro_event_active:
            return Decision(action="skip", side=None, p_model=0.5, reason="macro_event")

        # Only operate in the early window
        if features.elapsed_seconds < MIN_ELAPSED_SEC:
            return Decision(
                action="skip",
                side=None,
                p_model=0.5,
                reason=f"too_early: {features.elapsed_seconds:.0f}s < {MIN_ELAPSED_SEC}s",
            )
        if features.elapsed_seconds > MAX_ELAPSED_SEC:
            return Decision(
                action="skip",
                side=None,
                p_model=0.5,
                reason=f"past_early_window: {features.elapsed_seconds:.0f}s > {MAX_ELAPSED_SEC}s",
            )

        # Need BTC price history
        if len(features.btc_prices_60m) < 5:
            return Decision(action="skip", side=None, p_model=0.5, reason="no_btc_data")

        btc_ret = self._btc_10m_return(features)
        if btc_ret is None:
            return Decision(action="skip", side=None, p_model=0.5, reason="btc_insufficient_history")

        if abs(btc_ret) < BTC_SIGNAL_PCT:
            return Decision(
                action="skip",
                side=None,
                p_model=0.5,
                reason=f"weak_btc: {btc_ret:+.2f}% < {BTC_SIGNAL_PCT}%",
            )

        if btc_ret > 0:
            side        = "yes"
            entry_cents = features.yes_ask
        else:
            side        = "no"
            entry_cents = features.no_ask

        signals = {
            "btc_10m_return": round(btc_ret, 3),
            "elapsed_seconds": features.elapsed_seconds,
            "entry_cents": entry_cents,
            "btc_signal_threshold": BTC_SIGNAL_PCT,
        }

        return Decision(
            action="trade",
            side=side,
            p_model=0.735,   # empirical WR from OOS test (2024-2026)
            reason=(
                f"early_btc_{side.upper()}: btc_10m={btc_ret:+.2f}% "
                f"entry={entry_cents:.0f}c elapsed={features.elapsed_seconds:.0f}s"
            ),
            contributing_signals=signals,
            expected_value=0.035,   # ~$0.87/$25 at avg 69.5c entry
        )
