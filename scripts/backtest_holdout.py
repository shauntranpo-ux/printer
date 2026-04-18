"""
Final out-of-sample holdout evaluation.

This runs ONCE. Any tuning or parameter adjustment after seeing these
numbers invalidates them.

The holdout window: 2025-10-01 through yesterday (configurable).

Usage:
    python scripts\\backtest_holdout.py --holdout-start 2025-10-01
"""

from __future__ import annotations
import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", nargs="+", default=["BTC", "ETH", "SOL", "XRP", "DOGE"])
    parser.add_argument("--holdout-start", default="2025-10-01")
    parser.add_argument("--holdout-end", default=None)
    parser.add_argument("--train-days-before-holdout", type=int, default=180)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    from scripts.backtest_walk_forward import (
        make_strategy, load_prices, slice_events,
        fit_calibration_from_trades, compute_metrics
    )
    from strategies.backtest.runner import run_backtest

    holdout_start = datetime.fromisoformat(args.holdout_start).replace(tzinfo=timezone.utc)
    if args.holdout_end:
        holdout_end = datetime.fromisoformat(args.holdout_end).replace(tzinfo=timezone.utc)
    else:
        holdout_end = datetime.now(timezone.utc)

    train_start = holdout_start - pd.Timedelta(days=args.train_days_before_holdout)
    train_end   = holdout_start - pd.Timedelta(days=1)

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "train_window_days": args.train_days_before_holdout,
            "holdout_start": args.holdout_start,
            "holdout_end": holdout_end.isoformat(),
            "seed": args.seed,
        },
        "per_asset": {},
    }

    for asset in args.assets:
        print(f"\n== {asset} ==")
        try:
            df = load_prices(asset)

            strat_train = make_strategy(asset, calibrator=None)
            train_events = slice_events(
                asset, df, train_start.timestamp(), train_end.timestamp(), args.seed
            )
            train_trades = run_backtest(strat_train, train_events, stake_dollars=5.0)
            print(f"  train trades: {len(train_trades)}")
            cal = fit_calibration_from_trades(asset, train_trades)

            if cal is None:
                print(f"  insufficient train data -- running holdout with identity calibration")

            strat_holdout = make_strategy(asset, calibrator=cal)
            holdout_events = slice_events(
                asset, df, holdout_start.timestamp(), holdout_end.timestamp(), args.seed + 1
            )
            holdout_trades = run_backtest(strat_holdout, holdout_events, stake_dollars=5.0)
            m = compute_metrics(holdout_trades, total_windows=max(1, len(holdout_events)))
            results["per_asset"][asset] = {
                "metrics":      m,
                "train_trades": len(train_trades),
                "calibrated":   cal is not None,
            }
            print(f"  holdout: {m['total_trades']} trades  win={m['win_rate']:.3f}  "
                  f"pnl=${m['total_pnl_dollars']:+.2f}  avg=${m['avg_pnl_per_trade']:+.4f}")

            Path("results").mkdir(exist_ok=True)
            trades_df = pd.DataFrame([asdict(t) for t in holdout_trades])
            if len(trades_df) > 0:
                trades_df.to_parquet(f"results/holdout_trades_{asset}.parquet", index=False)
        except Exception as e:
            import traceback
            results["per_asset"][asset] = {
                "error": str(e),
                "traceback": traceback.format_exc(),
            }

    Path("results").mkdir(exist_ok=True)
    with open("results/holdout_report.json", "w") as f:
        json.dump(results, f, indent=2, default=str)

    print("\n== HOLDOUT RESULTS (do NOT tune after this) ==")
    for asset, rep in results["per_asset"].items():
        if "error" in rep:
            print(f"{asset}: ERROR: {rep['error']}")
            continue
        m = rep["metrics"]
        print(f"{asset}: trades={m['total_trades']:4d}  win={m['win_rate']:.3f}  "
              f"pnl=${m['total_pnl_dollars']:+.2f}  avg=${m['avg_pnl_per_trade']:+.4f}  "
              f"sharpe={m['trade_level_sharpe']:.2f}  brier={m['brier_score']:.3f}  "
              f"max_dd=${m['max_drawdown_dollars']:.2f}")


if __name__ == "__main__":
    main()
