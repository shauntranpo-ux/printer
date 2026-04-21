"""
Backtest for BTC and ETH hourly Kalshi strategies.

Out-of-sample period: 2024-01-01 to 2026-04-15 (post train_end=2023-12-31).
Training/calibration period: 2023-01-01 to 2023-12-31.

Usage:
    python scripts/backtest_hourly.py
    python scripts/backtest_hourly.py --assets BTC
    python scripts/backtest_hourly.py --stake 25
"""

from __future__ import annotations
import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# -- Data loading -------------------------------------------------------------

def load_prices(asset: str) -> pd.DataFrame:
    extended = Path("data/historical") / f"{asset}_1m_extended.parquet"
    legacy   = Path("data/historical") / f"{asset}_1m_2026.parquet"
    path = extended if extended.exists() else legacy
    if not path.exists():
        raise FileNotFoundError(f"No historical data for {asset}")
    df = pd.read_parquet(path)
    if "open_time" in df.columns and "timestamp" not in df.columns:
        df["timestamp"] = (
            df["open_time"].values.astype("datetime64[s]").astype("int64").astype("float64")
        )
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df[["timestamp", "close"]]


def load_btc_prices() -> pd.DataFrame | None:
    try:
        return load_prices("BTC")
    except FileNotFoundError:
        return None


# -- Strategy factory ----------------------------------------------------------

def make_hourly_strategy(asset: str, calibrator=None, stake: float = 25.0):
    from strategies.skip_layer import SkipConfig

    skip_cfg = SkipConfig(
        max_spread_cents        = 4.0,
        min_seconds_left        = 120.0,
        min_entry_price_cents   = 35.0,
        cold_start_samples      = 60,
        # Hourly windows need a higher vol gate: at t=5min with 55min remaining
        # and a 0.5% buffer, vol_ratio = 0.002*sqrt(55)/0.005 = 2.97 which would
        # blow past a 1.8 threshold. 4.0 lets the first half of the hour trade.
        vol_ratio_threshold     = 4.0,
        vol_confirm_mult        = 1.25,
        vol_oppose_mult         = 0.70,
        mom_lock_enabled        = True,
        # Don't tighten for neutral — backtest momentum labels are always neutral
        # because _momentum_label() uses time.time() rather than event timestamps.
        mom_lock_neutral_tighten= 1.0,
        mom_accel_scale         = 3.0,
    )

    if asset == "BTC":
        from strategies.btc_hourly_strategy import BTCHourlyStrategy
        return BTCHourlyStrategy(
            skip_config=skip_cfg, min_ev=0.05, stake_dollars=stake,
            calibrator=calibrator,
        )
    elif asset == "ETH":
        from strategies.eth_hourly_strategy import ETHHourlyStrategy
        return ETHHourlyStrategy(
            skip_config=skip_cfg, min_ev=0.04, stake_dollars=stake,
            calibrator=calibrator,
        )
    raise ValueError(f"Unsupported asset: {asset}")


# -- Event generation (hourly) -------------------------------------------------

def slice_hourly_events(
    asset: str,
    df: pd.DataFrame,
    start_ts: float,
    end_ts: float,
    seed: int,
) -> list:
    from strategies.backtest.hourly_window_generator import generate_hourly_events
    return list(generate_hourly_events(df, asset, start_ts=start_ts, end_ts=end_ts, seed=seed))


# -- Backtest runner (adapted for HourlyBacktestEvent) ------------------------

def run_hourly_backtest(strategy, events, stake: float, btc_prices_df=None) -> list:
    """Identical logic to runner.run_backtest but accepts HourlyBacktestEvent."""
    from collections import deque
    from strategies.backtest.runner import _settle_trade
    from strategies.features import MarketFeatures
    from strategies.fees import taker_fee
    import numpy as np

    trades = []
    traded_windows: set = set()

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

        # Build price history deques
        prices_1m  = deque(maxlen=60)
        prices_5m  = deque(maxlen=300)
        prices_60m = deque(maxlen=3600)
        for ts, p in event.price_history:
            prices_60m.append((ts, p))
            if ts >= event.eval_ts - 300:
                prices_5m.append((ts, p))
            if ts >= event.eval_ts - 60:
                prices_1m.append((ts, p))

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
            asset           = event.asset,
            ticker          = f"KH-{event.asset}",
            timestamp       = event.eval_ts,
            current_price   = event.current_price,
            strike          = event.strike,
            btc_price       = event.current_price if event.asset == "BTC" else 0.0,
            seconds_left    = event.seconds_left,
            elapsed_seconds = event.elapsed_seconds,
            yes_ask         = event.orderbook.yes_ask,
            no_ask          = event.orderbook.no_ask,
            yes_bid         = event.orderbook.yes_bid,
            no_bid          = event.orderbook.no_bid,
            spread_yes      = event.orderbook.yes_ask - event.orderbook.yes_bid,
            spread_no       = event.orderbook.no_ask  - event.orderbook.no_bid,
            prices_1m       = prices_1m,
            prices_5m       = prices_5m,
            prices_60m      = prices_60m,
            btc_prices_60m  = btc_60m,
            kalshi_price_history = kalshi_hist,
            realized_vol_1min    = event.realized_vol_1min,
        )

        decision = strategy.decide(features)
        if decision.action == "skip":
            continue

        side        = decision.side
        entry_cents = event.orderbook.yes_ask if side == "yes" else event.orderbook.no_ask
        entry_price = entry_cents / 100.0
        if entry_price <= 0 or entry_price >= 1.0:
            continue

        contracts = max(1, int(stake / entry_price))
        actual_stake = contracts * entry_price
        fee = taker_fee(contracts, entry_price)

        outcome, payout, pnl = _settle_trade(
            side        = side,
            entry_cents = entry_cents,
            contracts   = contracts,
            stake       = actual_stake,
            fee         = fee,
            strike      = event.strike,
            close_price = event.close_price,
        )

        traded_windows.add(window_key)
        from strategies.backtest.runner import BacktestTrade
        trades.append(BacktestTrade(
            asset              = event.asset,
            eval_ts            = event.eval_ts,
            window_start_ts    = event.window_start_ts,
            window_close_ts    = event.window_close_ts,
            side               = side,
            entry_price_cents  = entry_cents,
            raw_p_model        = float(decision.contributing_signals.get("raw_p_yes", decision.p_model)),
            calibrated_p_model = float(decision.p_model),
            contracts          = contracts,
            stake_dollars      = actual_stake,
            fee_dollars        = fee,
            close_price        = event.close_price,
            strike             = event.strike,
            outcome            = outcome,
            payout_dollars     = payout,
            pnl_dollars        = pnl,
            ev_at_entry        = float(decision.expected_value or 0.0),
            reason             = decision.reason,
            signals_dump       = dict(decision.contributing_signals),
        ))

    return trades


# -- Metrics -------------------------------------------------------------------

def compute_metrics(trades: list, total_windows: int) -> dict:
    if not trades:
        return {"total_trades": 0, "total_windows": total_windows,
                "trade_rate_pct": 0.0, "wins": 0, "losses": 0,
                "win_rate_pct": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
                "max_drawdown": 0.0, "sharpe": 0.0, "avg_ev_at_entry": 0.0}
    wins = sum(1 for t in trades if t.outcome == "win")
    pnl_list = [t.pnl_dollars for t in trades]
    total_pnl = sum(pnl_list)

    running = peak = max_dd = 0.0
    for p in pnl_list:
        running += p
        if running > peak:
            peak = running
        max_dd = max(max_dd, peak - running)

    sharpe = 0.0
    if len(pnl_list) >= 2:
        std = statistics.stdev(pnl_list)
        if std > 0:
            sharpe = statistics.mean(pnl_list) / std

    return {
        "total_trades":    len(trades),
        "total_windows":   total_windows,
        "trade_rate_pct":  round(len(trades) / total_windows * 100, 1) if total_windows else 0,
        "wins":            wins,
        "losses":          len(trades) - wins,
        "win_rate_pct":    round(wins / len(trades) * 100, 1),
        "total_pnl":       round(total_pnl, 2),
        "avg_pnl":         round(total_pnl / len(trades), 3),
        "max_drawdown":    round(max_dd, 2),
        "sharpe":          round(sharpe, 3),
        "avg_ev_at_entry": round(sum(t.ev_at_entry for t in trades) / len(trades), 4),
    }


def quarterly_breakdown(trades: list) -> dict:
    by_quarter: dict[str, list] = defaultdict(list)
    for t in trades:
        dt = datetime.utcfromtimestamp(t.eval_ts)
        q  = (dt.month - 1) // 3 + 1
        key = f"{dt.year}Q{q}"
        by_quarter[key].append(t)
    result = {}
    for qk, qtrades in sorted(by_quarter.items()):
        wins  = sum(1 for t in qtrades if t.outcome == "win")
        total = len(qtrades)
        pnl   = sum(t.pnl_dollars for t in qtrades)
        result[qk] = {
            "trades":   total,
            "win_rate": round(wins / total * 100, 1) if total else 0,
            "pnl":      round(pnl, 2),
        }
    return result


def fit_calibration(asset: str, trades: list):
    from strategies.calibration import AssetCalibrator
    if len(trades) < 15:
        return None
    cal = AssetCalibrator(asset)
    raw = [t.raw_p_model for t in trades]
    outcomes = [
        1 if ((t.side == "yes" and t.outcome == "win") or
              (t.side == "no"  and t.outcome == "loss")) else 0
        for t in trades
    ]
    cal.refit(raw, outcomes)
    return cal


# -- Main ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", nargs="+", default=["BTC", "ETH"])
    parser.add_argument("--train-start",  default="2023-01-01")
    parser.add_argument("--train-end",    default="2023-12-31")
    parser.add_argument("--test-start",   default="2024-01-01")
    parser.add_argument("--test-end",     default="2026-04-15")
    parser.add_argument("--stake",        type=float, default=25.0)
    parser.add_argument("--seed",         type=int,   default=42)
    args = parser.parse_args()

    def ts(s): return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()

    train_start_ts = ts(args.train_start)
    train_end_ts   = ts(args.train_end)
    test_start_ts  = ts(args.test_start)
    test_end_ts    = ts(args.test_end)

    btc_df = load_btc_prices()

    STRATEGY_SETTINGS = {
        "window_minutes":        60,
        "eval_interval_seconds": 300,
        "strike_increment_btc":  100.0,
        "strike_increment_eth":  10.0,
        "min_entry_price_cents": 35,
        "max_spread_cents":      4.0,
        "min_seconds_left":      120,
        "vol_gate_thresh":       4.00,
        "vol_confirm_mult":      1.25,
        "vol_oppose_mult":       0.70,
        "mom_lock_enabled":      True,
        "mom_lock_neutral_tighten": 1.00,
        "mom_accel_scale":       3.0,
        "btc_min_ev":            0.05,
        "eth_min_ev":            0.04,
        "stake_dollars":         args.stake,
        "btc_signals":           ["variance_ratio_regime", "30min_momentum", "us_session_premium"],
        "eth_signals":           ["btc_beta_30min", "variance_ratio_regime", "eth_btc_ratio_divergence", "us_session_premium"],
        "calibration":           "isotonic (fitted on train set)",
    }

    print("=" * 60)
    print("  BTC + ETH HOURLY KALSHI STRATEGY — BACKTEST REPORT")
    print(f"  Train: {args.train_start} -> {args.train_end}")
    print(f"  Test:  {args.test_start}  -> {args.test_end}")
    print(f"  Stake: ${args.stake:.0f} per trade")
    print("=" * 60)
    print("\nStrategy Settings:")
    for k, v in STRATEGY_SETTINGS.items():
        print(f"  {k}: {v}")

    results = {}

    for asset in args.assets:
        print(f"\n{'-'*60}")
        print(f"  {asset} HOURLY")
        print(f"{'-'*60}")

        try:
            df = load_prices(asset)
            print(f"  Data: {len(df):,} rows")
        except FileNotFoundError as e:
            print(f"  SKIP: {e}")
            continue

        # -- Train phase: fit calibration -------------------------------
        print(f"\n  [Train {args.train_start} -> {args.train_end}]")
        train_events = slice_hourly_events(asset, df, train_start_ts, train_end_ts, args.seed)
        print(f"  Generated {len(train_events):,} hourly eval points across {len(set(e.window_start_ts for e in train_events)):,} windows")

        strat_train = make_hourly_strategy(asset, calibrator=None, stake=args.stake)
        train_btc = btc_df if asset != "BTC" else None
        train_trades = run_hourly_backtest(strat_train, train_events, stake=args.stake, btc_prices_df=train_btc)
        cal = fit_calibration(asset, train_trades)
        print(f"  Train trades: {len(train_trades)} | WR: {sum(1 for t in train_trades if t.outcome=='win')/len(train_trades)*100:.1f}%" if train_trades else "  Train trades: 0")

        # -- Test phase: out-of-sample ----------------------------------
        print(f"\n  [Test {args.test_start} -> {args.test_end}]  (OUT OF SAMPLE)")
        test_events = slice_hourly_events(asset, df, test_start_ts, test_end_ts, args.seed)
        total_windows = len(set(e.window_start_ts for e in test_events))
        print(f"  Generated {len(test_events):,} hourly eval points across {total_windows:,} windows")

        strat_test = make_hourly_strategy(asset, calibrator=cal, stake=args.stake)
        test_btc = btc_df if asset != "BTC" else None
        test_trades = run_hourly_backtest(strat_test, test_events, stake=args.stake, btc_prices_df=test_btc)

        m = compute_metrics(test_trades, total_windows)
        qb = quarterly_breakdown(test_trades)

        print(f"\n  -- Results ----------------------------------------------")
        print(f"  Total trades:    {m['total_trades']} / {m['total_windows']} windows ({m['trade_rate_pct']}% trade rate)")
        print(f"  Win rate:        {m['win_rate_pct']}%  ({m['wins']} wins / {m['losses']} losses)")
        print(f"  Total PnL:       ${m['total_pnl']:+.2f}")
        print(f"  Avg PnL/trade:   ${m['avg_pnl']:+.4f}")
        print(f"  Max drawdown:    ${m['max_drawdown']:.2f}")
        print(f"  Sharpe (trades): {m['sharpe']:.3f}")
        print(f"  Avg EV at entry: {m['avg_ev_at_entry']:+.4f}")

        if qb:
            print(f"\n  -- Quarterly breakdown ----------------------------------")
            print(f"  {'Quarter':<10} {'Trades':>7} {'WR%':>7} {'PnL':>10}")
            running_pnl = 0.0
            for qk, qm in qb.items():
                running_pnl += qm["pnl"]
                print(f"  {qk:<10} {qm['trades']:>7} {qm['win_rate']:>6.1f}% ${qm['pnl']:>+8.2f}  (cum ${running_pnl:+.2f})")

        results[asset] = {"metrics": m, "quarterly": qb}

    print(f"\n{'='*60}")
    print("  COMBINED SUMMARY")
    print(f"{'='*60}")
    for asset, r in results.items():
        m = r["metrics"]
        print(f"  {asset}: {m['total_trades']} trades | WR {m['win_rate_pct']}% | PnL ${m['total_pnl']:+.2f} | DD ${m['max_drawdown']:.2f} | Sharpe {m['sharpe']:.3f}")

    print("\nStrategy Parameters:")
    for k, v in STRATEGY_SETTINGS.items():
        print(f"  {k}: {v}")

    out_path = Path("results/hourly_backtest_report.json")
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"settings": STRATEGY_SETTINGS, "results": results}, f, indent=2)
    print(f"\nFull results saved to {out_path}")


if __name__ == "__main__":
    main()
