"""
Evaluate each asset strategy under specific historical regimes.

Regimes:
  2020-03-12 to 2020-04-12: COVID crash + recovery
  2021-05-19 to 2021-06-30: BTC -55% drawdown
  2022-11-06 to 2022-11-30: FTX collapse
  2024-01-10 to 2024-03-15: ETF approval rally
  2026-02-01 to 2026-04-15: recent chop

Output: results/stress_report.json

Usage:
    python scripts\\backtest_stress.py
"""

from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


REGIMES = {
    "covid_2020": ("2020-03-12", "2020-04-12"),
    "crash_2021": ("2021-05-19", "2021-06-30"),
    "ftx_2022":   ("2022-11-06", "2022-11-30"),
    "etf_2024":   ("2024-01-10", "2024-03-15"),
    "chop_2026":  ("2026-02-01", "2026-04-15"),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", nargs="+",
                        default=["BTC", "ETH", "SOL", "XRP", "DOGE"])
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    from scripts.backtest_walk_forward import (
        make_strategy, load_prices, slice_events, compute_metrics
    )
    from strategies.backtest.runner import run_backtest

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "per_regime": {},
    }

    for regime_name, (start_str, end_str) in REGIMES.items():
        print(f"\n== {regime_name} ({start_str} to {end_str}) ==")
        start = datetime.fromisoformat(start_str).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(end_str).replace(tzinfo=timezone.utc)

        regime_results = {}
        for asset in args.assets:
            try:
                df = load_prices(asset)
                if df["timestamp"].max() < start.timestamp():
                    regime_results[asset] = {"status": "no_data_for_regime"}
                    continue
                events = slice_events(asset, df,
                                      start.timestamp(), end.timestamp(), args.seed)
                strat = make_strategy(asset, calibrator=None)
                trades = run_backtest(strat, events, stake_dollars=5.0)
                m = compute_metrics(trades, total_windows=max(1, len(events)))
                regime_results[asset] = m
                print(f"  {asset}: trades={m['total_trades']:4d}  win={m['win_rate']:.3f}  "
                      f"pnl=${m['total_pnl_dollars']:+.2f}")
            except FileNotFoundError:
                regime_results[asset] = {"status": "no_data_file"}
            except Exception as e:
                regime_results[asset] = {"error": str(e)}

        results["per_regime"][regime_name] = regime_results

    Path("results").mkdir(exist_ok=True)
    with open("results/stress_report.json", "w") as f:
        json.dump(results, f, indent=2, default=str)

    print("\nFull report: results/stress_report.json")


if __name__ == "__main__":
    main()
