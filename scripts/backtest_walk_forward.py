"""
Walk-forward backtest for all 5 markets.

Training/test windows:
  - Train window: 90 days
  - Test window: 30 days (subsequent)
  - Purge gap: 1 day between train and test (prevents leakage on the
    15-min window boundary)
  - Slide forward: 30 days (so each test window is fresh)

Output:
    results/wfv_report_v2.json
    results/wfv_trades_{asset}.parquet

Usage:
    python scripts\\backtest_walk_forward.py
    python scripts\\backtest_walk_forward.py --assets BTC
    python scripts\\backtest_walk_forward.py --train-days 60 --test-days 30
"""

from __future__ import annotations
import argparse
import json
import statistics
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))


def make_strategy(asset: str, calibrator=None):
    """Instantiate the evidence-based strategy for an asset."""
    from strategies.skip_layer import SkipConfig

    skip_cfg = SkipConfig(
        max_spread_cents=3.0,
        min_seconds_left=30.0,
        cold_start_samples=50,
    )
    stake = 5.0

    if asset == "BTC":
        from strategies.btc_strategy import BTCStrategy
        return BTCStrategy(
            skip_config=skip_cfg, min_ev=0.08, stake_dollars=stake,
            calibrator=calibrator, continuation_only=False,
        )
    elif asset == "ETH":
        from strategies.eth_strategy import ETHStrategy
        return ETHStrategy(
            skip_config=skip_cfg, min_ev=0.08, stake_dollars=stake,
            calibrator=calibrator,
        )
    elif asset == "SOL":
        from strategies.sol_strategy import SOLStrategy
        return SOLStrategy(
            skip_config=skip_cfg, min_ev=0.09, stake_dollars=stake,
            calibrator=calibrator,
        )
    elif asset == "XRP":
        from strategies.xrp_strategy import XRPStrategy
        return XRPStrategy(
            skip_config=skip_cfg, min_ev=0.09, stake_dollars=stake,
            calibrator=calibrator,
        )
    elif asset == "DOGE":
        from strategies.doge_strategy import DOGEStrategy
        return DOGEStrategy(
            skip_config=skip_cfg, min_ev=0.10, stake_dollars=stake,
            calibrator=calibrator,
        )
    else:
        raise ValueError(f"Unknown asset {asset}")


def load_prices(asset: str) -> pd.DataFrame:
    extended = Path("data/historical") / f"{asset}_1m_extended.parquet"
    legacy = Path("data/historical") / f"{asset}_1m_2026.parquet"
    path = extended if extended.exists() else legacy
    if not path.exists():
        raise FileNotFoundError(
            f"No historical data for {asset}. Run scripts/download_historical_extended.py"
        )
    df = pd.read_parquet(path)
    # Normalize column: legacy parquets use open_time (datetime64 UTC),
    # extended parquets use timestamp (unix seconds float).
    if "open_time" in df.columns and "timestamp" not in df.columns:
        # datetime64[us, UTC] -> unix seconds float (unambiguous path)
        df["timestamp"] = df["open_time"].values.astype("datetime64[s]").astype("int64").astype("float64")
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df[["timestamp", "close"]]


def slice_events(asset: str, df: pd.DataFrame, start_ts: float, end_ts: float, seed: int) -> list:
    from strategies.backtest.window_generator import generate_events
    return list(generate_events(df, asset, start_ts=start_ts, end_ts=end_ts, seed=seed))


def fit_calibration_from_trades(asset: str, trades: list):
    """Fit AssetCalibrator from a list of BacktestTrade records."""
    from strategies.calibration import AssetCalibrator
    if len(trades) < 50:
        return None
    cal = AssetCalibrator(asset)
    raw = [t.raw_p_model for t in trades]
    outcomes = [1 if t.outcome == "win" else 0 for t in trades]
    cal.refit(raw, outcomes)
    return cal


def compute_metrics(trades: list, total_windows: int) -> dict:
    if not trades:
        return {
            "total_trades": 0,
            "total_windows_available": total_windows,
            "trade_rate": 0.0,
            "wins": 0,
            "win_rate": 0.0,
            "total_pnl_dollars": 0.0,
            "avg_pnl_per_trade": 0.0,
            "brier_score": 0.0,
            "trade_level_sharpe": 0.0,
            "max_drawdown_dollars": 0.0,
        }
    wins = sum(1 for t in trades if t.outcome == "win")
    pnl_list = [t.pnl_dollars for t in trades]
    total_pnl = sum(pnl_list)
    avg_pnl = total_pnl / len(trades)

    brier = sum(
        (t.calibrated_p_model - (1 if t.outcome == "win" else 0)) ** 2
        for t in trades
    ) / len(trades)

    if len(pnl_list) >= 2:
        mean_p = statistics.mean(pnl_list)
        std_p = statistics.stdev(pnl_list)
        trade_sharpe = mean_p / std_p if std_p > 0 else 0.0
    else:
        trade_sharpe = 0.0

    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnl_list:
        running += p
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd

    return {
        "total_trades":             len(trades),
        "total_windows_available":  total_windows,
        "trade_rate":               len(trades) / total_windows if total_windows else 0,
        "wins":                     wins,
        "win_rate":                 wins / len(trades),
        "total_pnl_dollars":        round(total_pnl, 2),
        "avg_pnl_per_trade":        round(avg_pnl, 4),
        "brier_score":              round(brier, 4),
        "trade_level_sharpe":       round(trade_sharpe, 4),
        "max_drawdown_dollars":     round(max_dd, 2),
    }


def walk_forward_one_asset(
    asset: str,
    train_days: int,
    test_days: int,
    purge_days: int,
    start: datetime,
    end: datetime,
    seed: int,
) -> dict:
    from strategies.backtest.runner import run_backtest

    print(f"\n== Walk-forward: {asset} ==")
    df = load_prices(asset)
    print(f"  Loaded {len(df):,} rows")

    day_sec = 86400.0
    train_sec = train_days * day_sec
    test_sec = test_days * day_sec
    purge_sec = purge_days * day_sec
    slide_sec = test_sec

    slice_start = start.timestamp()
    end_sec = end.timestamp()

    all_test_trades = []
    per_slice_metrics = []

    while slice_start + train_sec + purge_sec + test_sec <= end_sec:
        train_start = slice_start
        train_end = slice_start + train_sec
        test_start = train_end + purge_sec
        test_end = test_start + test_sec

        print(f"  train={datetime.fromtimestamp(train_start, tz=timezone.utc).date()}"
              f"..{datetime.fromtimestamp(train_end, tz=timezone.utc).date()} "
              f"test={datetime.fromtimestamp(test_start, tz=timezone.utc).date()}"
              f"..{datetime.fromtimestamp(test_end, tz=timezone.utc).date()}")

        strat_for_train = make_strategy(asset, calibrator=None)
        train_events = slice_events(asset, df, train_start, train_end, seed)
        train_trades = run_backtest(strat_for_train, train_events, stake_dollars=5.0)
        print(f"    train: {len(train_trades)} trades")

        cal = fit_calibration_from_trades(asset, train_trades)
        if cal is None:
            print(f"    insufficient train trades ({len(train_trades)}), using identity calibration")

        strat_for_test = make_strategy(asset, calibrator=cal)
        test_events = slice_events(asset, df, test_start, test_end, seed + 1)
        test_trades = run_backtest(strat_for_test, test_events, stake_dollars=5.0)
        print(f"    test:  {len(test_trades)} trades")

        n_windows = len({t.window_start_ts for t in test_trades}) or max(1, int(test_sec / 900))
        slice_metrics = compute_metrics(test_trades, total_windows=n_windows)
        slice_metrics["train_start_date"] = datetime.fromtimestamp(train_start, tz=timezone.utc).date().isoformat()
        slice_metrics["train_end_date"] = datetime.fromtimestamp(train_end, tz=timezone.utc).date().isoformat()
        slice_metrics["test_start_date"] = datetime.fromtimestamp(test_start, tz=timezone.utc).date().isoformat()
        slice_metrics["test_end_date"] = datetime.fromtimestamp(test_end, tz=timezone.utc).date().isoformat()
        per_slice_metrics.append(slice_metrics)
        all_test_trades.extend(test_trades)

        slice_start += slide_sec

    total_w = sum(m["total_windows_available"] for m in per_slice_metrics) or 1
    overall = compute_metrics(all_test_trades, total_windows=total_w)
    return {
        "asset": asset,
        "slices": per_slice_metrics,
        "overall_test": overall,
        "test_trades": [asdict(t) for t in all_test_trades],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", nargs="+", default=["BTC", "ETH", "SOL", "XRP", "DOGE"])
    parser.add_argument("--train-days", type=int, default=90)
    parser.add_argument("--test-days", type=int, default=30)
    parser.add_argument("--purge-days", type=int, default=1)
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--holdout-start", default="2025-10-01")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    if args.end:
        end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    else:
        end = datetime.fromisoformat(args.holdout_start).replace(tzinfo=timezone.utc)

    Path("results").mkdir(exist_ok=True)

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "train_days": args.train_days,
            "test_days": args.test_days,
            "purge_days": args.purge_days,
            "start": args.start,
            "end": end.isoformat(),
            "seed": args.seed,
            "stake_dollars": 5.0,
        },
        "per_asset": {},
    }

    for asset in args.assets:
        try:
            report = walk_forward_one_asset(
                asset=asset,
                train_days=args.train_days,
                test_days=args.test_days,
                purge_days=args.purge_days,
                start=start,
                end=end,
                seed=args.seed,
            )
            trades_df = pd.DataFrame(report.pop("test_trades"))
            if len(trades_df) > 0:
                trades_df.to_parquet(f"results/wfv_trades_{asset}.parquet", index=False)
            results["per_asset"][asset] = report
        except Exception as e:
            import traceback
            results["per_asset"][asset] = {
                "error": str(e),
                "traceback": traceback.format_exc(),
            }

    with open("results/wfv_report_v2.json", "w") as f:
        json.dump(results, f, indent=2, default=str)

    print("\n== WALK-FORWARD SUMMARY ==")
    for asset, rep in results["per_asset"].items():
        if "error" in rep:
            print(f"{asset}: ERROR: {rep['error']}")
            continue
        m = rep["overall_test"]
        print(f"{asset}: trades={m['total_trades']:5d} win={m['win_rate']:.3f} "
              f"pnl=${m['total_pnl_dollars']:+.2f} avg=${m['avg_pnl_per_trade']:+.4f} "
              f"sharpe={m['trade_level_sharpe']:.2f} brier={m['brier_score']:.3f}")
    print("\nFull report: results/wfv_report_v2.json")


if __name__ == "__main__":
    main()
