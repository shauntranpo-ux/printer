"""
Clean ETH diagnostic — no disk calibration loaded.
Tests raw signal quality without any calibration contamination.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from datetime import datetime, timezone
from collections import deque
import numpy as np

from strategies.skip_layer import SkipConfig
from strategies.eth_hourly_strategy import ETHHourlyStrategy
from strategies.calibration import AssetCalibrator
from strategies.backtest.runner import _settle_trade, BacktestTrade
from strategies.features import MarketFeatures
from strategies.fees import taker_fee
from scripts.backtest_hourly import (
    load_prices, load_btc_prices, slice_hourly_events,
    compute_metrics, quarterly_breakdown,
)


class IdentityCalibrator(AssetCalibrator):
    """Never loads from disk, always returns raw p unchanged."""
    def __init__(self):
        self.asset = "_identity"
        self._method = None
        self._isotonic = None
        self._platt = None
        self.sample_count = 0

    def _load_if_exists(self):
        pass  # never load from disk

    def calibrate(self, raw_p: float) -> float:
        return raw_p

    def refit(self, *args, **kwargs):
        pass


def run_backtest_raw(strategy, events, stake, btc_prices_df=None):
    trades = []
    traded_windows = set()

    btc_history = None
    if btc_prices_df is not None:
        btc_history = list(
            zip(btc_prices_df["timestamp"].values, btc_prices_df["close"].values)
        )
        btc_arr = np.array([ts for ts, _ in btc_history])

    for event in events:
        window_key = (event.asset, event.window_start_ts)
        if window_key in traded_windows:
            continue

        prices_60m = deque(maxlen=3600)
        for ts, p in event.price_history:
            prices_60m.append((ts, p))

        btc_60m = deque(maxlen=3600)
        if btc_history is not None:
            cutoff = event.eval_ts - 3600
            lo = int(np.searchsorted(btc_arr, cutoff))
            hi = int(np.searchsorted(btc_arr, event.eval_ts, side="right"))
            for ts, p in btc_history[lo:hi]:
                btc_60m.append((ts, p))

        kalshi_hist = deque(maxlen=60)
        for i in range(30):
            kalshi_hist.append((event.eval_ts - (30 - i) * 10, event.orderbook.yes_ask))

        features = MarketFeatures(
            asset=event.asset, ticker=f"KH-{event.asset}",
            timestamp=event.eval_ts, current_price=event.current_price,
            strike=event.strike, btc_price=0.0,
            seconds_left=event.seconds_left, elapsed_seconds=event.elapsed_seconds,
            yes_ask=event.orderbook.yes_ask, no_ask=event.orderbook.no_ask,
            yes_bid=event.orderbook.yes_bid, no_bid=event.orderbook.no_bid,
            spread_yes=event.orderbook.yes_ask - event.orderbook.yes_bid,
            spread_no=event.orderbook.no_ask - event.orderbook.no_bid,
            prices_1m=deque(maxlen=60), prices_5m=deque(maxlen=300),
            prices_60m=prices_60m, btc_prices_60m=btc_60m,
            kalshi_price_history=kalshi_hist,
            realized_vol_1min=event.realized_vol_1min,
        )

        decision = strategy.decide(features)
        if decision.action == "skip":
            continue

        side = decision.side
        entry_cents = event.orderbook.yes_ask if side == "yes" else event.orderbook.no_ask
        entry_price = entry_cents / 100.0
        if entry_price <= 0 or entry_price >= 1.0:
            continue

        contracts = max(1, int(stake / entry_price))
        actual_stake = contracts * entry_price
        fee = taker_fee(contracts, entry_price)

        outcome, payout, pnl = _settle_trade(
            side=side, entry_cents=entry_cents, contracts=contracts,
            stake=actual_stake, fee=fee,
            strike=event.strike, close_price=event.close_price,
        )

        traded_windows.add(window_key)
        trades.append(BacktestTrade(
            asset=event.asset, eval_ts=event.eval_ts,
            window_start_ts=event.window_start_ts, window_close_ts=event.window_close_ts,
            side=side, entry_price_cents=entry_cents,
            raw_p_model=float(decision.contributing_signals.get("raw_p_yes", decision.p_model)),
            calibrated_p_model=float(decision.p_model),
            contracts=contracts, stake_dollars=actual_stake, fee_dollars=fee,
            close_price=event.close_price, strike=event.strike,
            outcome=outcome, payout_dollars=payout, pnl_dollars=pnl,
            ev_at_entry=float(decision.expected_value or 0.0),
            reason=decision.reason,
            signals_dump=dict(decision.contributing_signals),
        ))

    return trades


def ts(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def main():
    eth_df = load_prices("ETH")
    btc_df = load_btc_prices()

    test_start = ts("2024-01-01")
    test_end   = ts("2026-04-15")

    test_events = slice_hourly_events("ETH", eth_df, test_start, test_end, 42)
    total_windows = len(set(e.window_start_ts for e in test_events))
    print(f"Test windows: {total_windows:,}")

    skip_cfg = SkipConfig(
        max_spread_cents=4.0, min_seconds_left=120.0, min_entry_price_cents=35.0,
        cold_start_samples=60, vol_ratio_threshold=4.0, vol_confirm_mult=1.25,
        vol_oppose_mult=0.70, mom_lock_enabled=True, mom_lock_neutral_tighten=1.0,
        mom_accel_scale=3.0,
    )

    configs = [
        ("raw ev=0.05", 0.05),
        ("raw ev=0.04", 0.04),
        ("raw ev=0.03", 0.03),
        ("raw ev=0.02", 0.02),
        ("raw ev=0.01", 0.01),
    ]

    print(f"\n{'Config':<15}  {'Trades':>7}  {'Rate%':>6}  {'WR%':>6}  "
          f"{'PnL($)':>9}  {'Ann/yr':>10}  {'AvgPnL':>8}")
    print("-" * 75)

    for name, min_ev in configs:
        strat = ETHHourlyStrategy(
            skip_config=skip_cfg, min_ev=min_ev, stake_dollars=25,
            calibrator=IdentityCalibrator(),
        )
        trades = run_backtest_raw(strat, test_events, stake=25, btc_prices_df=btc_df)
        m = compute_metrics(trades, total_windows)
        ann = m["total_pnl"] * 12 / 27
        print(
            f"{name:<15}  {m['total_trades']:>7}  {m['trade_rate_pct']:>5.1f}%"
            f"  {m['win_rate_pct']:>5.1f}%  {m['total_pnl']:>+9.2f}"
            f"  {ann:>+10.2f}/yr  {m['avg_pnl']:>+8.4f}"
        )

    # Best config detail
    print("\n-- Quarterly breakdown (raw ev=0.04) --")
    strat = ETHHourlyStrategy(
        skip_config=skip_cfg, min_ev=0.04, stake_dollars=25,
        calibrator=IdentityCalibrator(),
    )
    trades = run_backtest_raw(strat, test_events, stake=25, btc_prices_df=btc_df)
    for qk, qd in quarterly_breakdown(trades).items():
        print(f"  {qk}: {qd['trades']:>4} trades  WR={qd['win_rate']:>5.1f}%  PnL={qd['pnl']:>+8.2f}")


if __name__ == "__main__":
    main()
