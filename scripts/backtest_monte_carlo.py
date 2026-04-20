"""
Monte Carlo simulation on WFA test trades.

Resamples each asset's trade PnL sequence 10,000 times to produce:
  - 5th/50th/95th percentile cumulative PnL curves
  - Confidence intervals on win rate, Sharpe, max drawdown
  - Lucky-run test: % of random sequences that beat observed PnL

Uses results/wfv_trades_{asset}.parquet (output of backtest_walk_forward.py).

Usage:
    python scripts\\backtest_monte_carlo.py
    python scripts\\backtest_monte_carlo.py --assets ETH SOL --n 5000
"""

from __future__ import annotations
import argparse
import json
import statistics
from pathlib import Path

import numpy as np
import pandas as pd


def max_drawdown(pnl_series: np.ndarray) -> float:
    cumulative = np.cumsum(pnl_series)
    peak = np.maximum.accumulate(cumulative)
    dd = peak - cumulative
    return float(dd.max()) if len(dd) else 0.0


def trade_sharpe(pnl_series: np.ndarray) -> float:
    if len(pnl_series) < 2:
        return 0.0
    std = pnl_series.std()
    return float(pnl_series.mean() / std) if std > 0 else 0.0


def run_monte_carlo(pnl: np.ndarray, n_sims: int, rng: np.random.Generator) -> dict:
    n = len(pnl)
    # n_sims x n matrix of resampled sequences (with replacement)
    sims = rng.choice(pnl, size=(n_sims, n), replace=True)

    total_pnl   = sims.sum(axis=1)
    win_rates   = (sims > 0).mean(axis=1)
    sharpes     = np.array([trade_sharpe(row) for row in sims])
    drawdowns   = np.array([max_drawdown(row) for row in sims])

    observed_pnl = float(pnl.sum())
    pct_beat     = float((total_pnl >= observed_pnl).mean())

    def pct(arr, q):
        return float(np.percentile(arr, q))

    return {
        "n_trades":           n,
        "observed_total_pnl": round(observed_pnl, 2),
        "pnl_p05":            round(pct(total_pnl, 5),  2),
        "pnl_p50":            round(pct(total_pnl, 50), 2),
        "pnl_p95":            round(pct(total_pnl, 95), 2),
        "win_rate_p05":       round(pct(win_rates, 5),  4),
        "win_rate_p50":       round(pct(win_rates, 50), 4),
        "win_rate_p95":       round(pct(win_rates, 95), 4),
        "sharpe_p05":         round(pct(sharpes, 5),  4),
        "sharpe_p50":         round(pct(sharpes, 50), 4),
        "sharpe_p95":         round(pct(sharpes, 95), 4),
        "max_dd_p50":         round(pct(drawdowns, 50), 2),
        "max_dd_p95":         round(pct(drawdowns, 95), 2),
        "pct_sims_beat_observed": round(pct_beat, 4),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", nargs="+", default=["ETH", "SOL", "XRP", "DOGE"])
    parser.add_argument("--n", type=int, default=10_000, help="Number of simulations")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--source", choices=["wfv", "holdout"], default="wfv",
                        help="Which trade file to use")
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed)
    results = {}

    for asset in args.assets:
        path = Path(f"results/{args.source}_trades_{asset}.parquet")
        if not path.exists():
            print(f"{asset}: no trade file at {path} — skipping")
            continue

        df = pd.read_parquet(path)
        if "pnl_dollars" not in df.columns or len(df) == 0:
            print(f"{asset}: empty or missing pnl_dollars column — skipping")
            continue

        pnl = df["pnl_dollars"].values.astype(float)
        print(f"{asset}: {len(pnl)} trades, running {args.n:,} simulations...")
        mc = run_monte_carlo(pnl, args.n, rng)
        results[asset] = mc

    if not results:
        print("No results. Run backtest_walk_forward.py first.")
        return

    out_path = Path(f"results/monte_carlo_{args.source}.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\n{'Asset':<6} {'Trades':>6}  {'Obs PnL':>8}  {'P5':>7}  {'P50':>7}  {'P95':>7}  "
          f"{'WR p50':>7}  {'Sharpe p50':>10}  {'DD p95':>7}  {'Lucky%':>7}")
    print("-" * 90)
    for asset, m in results.items():
        lucky_pct = m["pct_sims_beat_observed"] * 100
        print(
            f"{asset:<6} {m['n_trades']:>6}  "
            f"${m['observed_total_pnl']:>7.2f}  "
            f"${m['pnl_p05']:>6.2f}  "
            f"${m['pnl_p50']:>6.2f}  "
            f"${m['pnl_p95']:>6.2f}  "
            f"{m['win_rate_p50']*100:>6.1f}%  "
            f"{m['sharpe_p50']:>10.3f}  "
            f"${m['max_dd_p95']:>6.2f}  "
            f"{lucky_pct:>6.1f}%"
        )

    print(f"\nFull report: {out_path}")
    print("\nLucky% = % of random reshuffles that matched or beat observed PnL.")
    print("If Lucky% > 50%, your observed result is below the median reshuffled outcome.")
    print("If Lucky% < 5%, results are hard to achieve by chance — signal is real.")


if __name__ == "__main__":
    main()
