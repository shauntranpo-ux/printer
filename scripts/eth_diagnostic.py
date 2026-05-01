"""Diagnostic: ETH without calibration, different min_ev values."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from datetime import datetime, timezone
from scripts.backtest_hourly import (
    load_prices, load_btc_prices, slice_hourly_events,
    run_hourly_backtest, compute_metrics, fit_calibration,
)

from strategies.skip_layer import SkipConfig
# ETHHourlyStrategy removed (dead code)


def ts(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


train_start = ts("2023-01-01")
train_end   = ts("2023-12-31")
test_start  = ts("2024-01-01")
test_end    = ts("2026-04-15")

eth_df = load_prices("ETH")
btc_df = load_btc_prices()

train_events = slice_hourly_events("ETH", eth_df, train_start, train_end, 42)
test_events  = slice_hourly_events("ETH", eth_df, test_start,  test_end,  42)
total_windows = len(set(e.window_start_ts for e in test_events))

skip_cfg = SkipConfig(
    max_spread_cents=4.0,
    min_seconds_left=120.0,
    min_entry_price_cents=35.0,
    cold_start_samples=60,
    vol_ratio_threshold=4.0,
    vol_confirm_mult=1.25,
    vol_oppose_mult=0.70,
    mom_lock_enabled=True,
    mom_lock_neutral_tighten=1.0,
    mom_accel_scale=3.0,
)

# Fit calibrator on train
strat_train = ETHHourlyStrategy(skip_config=skip_cfg, min_ev=0.04, stake_dollars=25)
train_trades = run_hourly_backtest(strat_train, train_events, stake=25, btc_prices_df=btc_df)
cal = fit_calibration("ETH", train_trades)
print(f"Train: {len(train_trades)} trades, WR={sum(1 for t in train_trades if t.outcome=='win')/len(train_trades)*100:.1f}%")
print()

configs = [
    ("ETH no-cal ev=0.04", 0.04, False),
    ("ETH no-cal ev=0.03", 0.03, False),
    ("ETH no-cal ev=0.02", 0.02, False),
    ("ETH cal    ev=0.04", 0.04, True),
    ("ETH cal    ev=0.03", 0.03, True),
    ("ETH cal    ev=0.02", 0.02, True),
]

print(f"{'Config':<25}  {'Trades':>7}  {'Rate':>6}  {'WR%':>6}  {'PnL($)':>9}  {'Ann/yr':>10}")
print("-" * 75)
for name, min_ev, use_cal in configs:
    calibrator = cal if use_cal else None
    strat = ETHHourlyStrategy(
        skip_config=skip_cfg, min_ev=min_ev, stake_dollars=25, calibrator=calibrator
    )
    trades = run_hourly_backtest(strat, test_events, stake=25, btc_prices_df=btc_df)
    m = compute_metrics(trades, total_windows)
    ann = m["total_pnl"] * 12 / 27
    print(
        f"{name:<25}  {m['total_trades']:>7}  {m['trade_rate_pct']:>5.1f}%"
        f"  {m['win_rate_pct']:>5.1f}%  {m['total_pnl']:>+9.2f}  {ann:>+10.2f}/yr"
    )
