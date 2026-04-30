"""
Extended 15m parameter sweep: Supertrend period × multiplier × momentum_lookback.
Outputs results to backtesting/output/sweep_results.csv, then runs WFA + Monte Carlo
on the best params per asset.

Usage:
    py run_15m_sweep_extended.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backtesting"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from backtesting.data.loaders import load_bars
from backtesting.simulation.fifteen_min_backtest import (
    run_fifteen_min_backtest,
    run_monte_carlo,
    run_wfa,
)

# ── Date range ────────────────────────────────────────────────────────────────
DATE_START = "2023-01-01"
DATE_END   = "2025-04-01"

# ── Per-asset sweep grids ─────────────────────────────────────────────────────
GRIDS = {
    "BTC": {
        "period":   [5, 7, 10, 14, 20],
        "mult":     [2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5],
        "lookback": [3, 4, 5, 6],
    },
    "ETH": {
        "period":   [5, 7, 10, 14, 20],
        "mult":     [2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5],
        "lookback": [3, 4, 5, 6],
    },
    "SOL": {
        "period":   [7, 10, 14, 20],
        "mult":     [3.0, 4.0, 4.5, 5.0, 5.5, 6.0],
        "lookback": [3, 4, 5, 6],
    },
    "XRP": {
        "period":   [7, 10, 14, 20],
        "mult":     [3.0, 4.0, 4.5, 5.0, 5.5, 6.0],
        "lookback": [3, 4, 5, 6],
    },
}

MIN_TRADES = 30   # discard combos with fewer trades (too few for statistical confidence)
TOP_N      = 5    # number of top combos to run WFA on per asset

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "backtesting", "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def sharpe(pnl_arr: np.ndarray) -> float:
    std = pnl_arr.std()
    return float(pnl_arr.mean() / (std + 1e-10))


def run_sweep(asset: str, bars: pd.DataFrame) -> pd.DataFrame:
    grid = GRIDS[asset]
    total = len(grid["period"]) * len(grid["mult"]) * len(grid["lookback"])
    print(f"\n{'='*64}")
    print(f"  {asset}  —  {len(bars):,} bars  |  {total} combinations")
    print(f"{'='*64}")

    results = []
    done = 0
    for period in grid["period"]:
        for mult in grid["mult"]:
            for lookback in grid["lookback"]:
                done += 1
                try:
                    df = run_fifteen_min_backtest(
                        bars=bars,
                        asset=asset,
                        min_ev=0.0,             # EV gate disabled: testing signal quality only
                        stake_dollars=25.0,
                        entry_minute=5,
                        min_entry_price_cents=5.0,
                        max_entry_price_cents=75.0,
                        supertrend_atr_period=period,
                        supertrend_atr_multiplier=mult,
                        momentum_lookback=lookback,
                    )
                except Exception as exc:
                    print(f"  [{done}/{total}] p={period} m={mult} lb={lookback}  ERROR: {exc}")
                    continue

                n = len(df)
                if n < MIN_TRADES:
                    continue

                pnl = df["pnl"].values
                wr  = float(df["win"].mean())
                tot = float(pnl.sum())
                sp  = sharpe(pnl)

                tag = f"p={period:2d} m={mult:.1f} lb={lookback}"
                print(f"  [{done:>3}/{total}] {tag}  n={n:>5}  WR={wr:.1%}  "
                      f"total=${tot*25:>+8.2f}  sharpe={sp:+.3f}")
                results.append({
                    "asset":    asset,
                    "period":   period,
                    "mult":     mult,
                    "lookback": lookback,
                    "n_trades": n,
                    "win_rate": round(wr, 4),
                    "total_pnl_units": round(tot, 4),
                    "total_pnl_dollars": round(tot * 25, 2),
                    "avg_pnl_units": round(float(pnl.mean()), 6),
                    "sharpe":   round(sp, 4),
                })

    return pd.DataFrame(results)


def run_wfa_and_mc(asset: str, bars: pd.DataFrame, top_rows: pd.DataFrame) -> None:
    print(f"\n  {asset} — WFA + Monte Carlo on top {len(top_rows)} combos")
    print(f"  {'─'*56}")

    wfa_rows = []
    mc_rows  = []

    for _, row in top_rows.iterrows():
        params = dict(
            min_ev=0.0,
            stake_dollars=25.0,
            entry_minute=5,
            min_entry_price_cents=5.0,
            max_entry_price_cents=75.0,
            supertrend_atr_period=int(row["period"]),
            supertrend_atr_multiplier=float(row["mult"]),
            momentum_lookback=int(row["lookback"]),
        )
        label = f"p={int(row['period'])} m={row['mult']:.1f} lb={int(row['lookback'])}"

        # WFA
        wfa_df = run_wfa(bars, asset, n_folds=6, **params)
        folds_pos = int((wfa_df["total_pnl"] > 0).sum())
        avg_wr    = float(wfa_df["win_rate"].mean())
        avg_sp    = float(wfa_df["sharpe"].mean())
        wfa_consistency = folds_pos / len(wfa_df) if len(wfa_df) else 0.0

        print(f"    WFA  {label:24s}  folds_pos={folds_pos}/{len(wfa_df)}  "
              f"avg_WR={avg_wr:.1%}  avg_sharpe={avg_sp:+.3f}")

        for _, frow in wfa_df.iterrows():
            wfa_rows.append({
                "asset": asset, "period": row["period"], "mult": row["mult"],
                "lookback": row["lookback"], **frow.to_dict(),
            })

        # MC on full-period backtest
        full_df = run_fifteen_min_backtest(bars, asset, **params)
        if not full_df.empty:
            mc_df = run_monte_carlo(full_df, n_iterations=1000)
            lo = float(np.quantile(mc_df["total_pnl"], 0.025))
            hi = float(np.quantile(mc_df["total_pnl"], 0.975))
            mean_pnl = float(mc_df["total_pnl"].mean())
            positive_prob = float((mc_df["total_pnl"] > 0).mean())
            print(f"    MC   {label:24s}  mean_pnl={mean_pnl*25:+.1f}$  "
                  f"95CI=[{lo*25:+.1f}$,{hi*25:+.1f}$]  P(profit)={positive_prob:.1%}")
            for _, mrow in mc_df.iterrows():
                mc_rows.append({
                    "asset": asset, "period": row["period"], "mult": row["mult"],
                    "lookback": row["lookback"], **mrow.to_dict(),
                })

    if wfa_rows:
        pd.DataFrame(wfa_rows).to_csv(
            os.path.join(OUTPUT_DIR, f"wfa_{asset.lower()}.csv"), index=False
        )
    if mc_rows:
        pd.DataFrame(mc_rows).to_csv(
            os.path.join(OUTPUT_DIR, f"mc_{asset.lower()}.csv"), index=False
        )


def run():
    all_results = []

    for asset in ["BTC", "ETH", "SOL", "XRP"]:
        try:
            bars = load_bars(asset, start_date=DATE_START, end_date=DATE_END)
        except Exception as exc:
            print(f"\n[{asset}] SKIP — data load failed: {exc}")
            continue

        bars = bars.rename(columns={"open_time": "timestamp"}) if "open_time" in bars.columns else bars
        bars["timestamp"] = pd.to_datetime(bars["timestamp"], utc=True)

        sweep_df = run_sweep(asset, bars)
        if sweep_df.empty:
            print(f"  [{asset}] No valid combinations found.")
            continue

        sweep_df.to_csv(os.path.join(OUTPUT_DIR, f"sweep_{asset.lower()}.csv"), index=False)
        all_results.append(sweep_df)

        # Top combos: require win_rate >= 0.50, rank by sharpe
        qualified = sweep_df[sweep_df["win_rate"] >= 0.50].copy()
        if qualified.empty:
            qualified = sweep_df.copy()  # relax if nothing qualifies
        top = qualified.nlargest(TOP_N, "sharpe")

        run_wfa_and_mc(asset, bars, top)

    # ── Combined summary ──────────────────────────────────────────────────────
    if not all_results:
        print("\nNo results to summarize.")
        return

    combined = pd.concat(all_results, ignore_index=True)
    combined.to_csv(os.path.join(OUTPUT_DIR, "sweep_all.csv"), index=False)

    print(f"\n\n{'='*64}")
    print("FINAL RECOMMENDATIONS  (best Sharpe with WR >= 50%, n >= 30)")
    print(f"{'='*64}")
    for asset in ["BTC", "ETH", "SOL", "XRP"]:
        sub = combined[combined["asset"] == asset]
        if sub.empty:
            print(f"  {asset}: no data")
            continue
        q = sub[sub["win_rate"] >= 0.50]
        if q.empty:
            q = sub
        best = q.nlargest(1, "sharpe").iloc[0]
        print(
            f"  {asset:3s}  period={int(best['period']):2d}  mult={best['mult']:.1f}  "
            f"lookback={int(best['lookback'])}  "
            f"n={int(best['n_trades']):>5}  WR={best['win_rate']:.1%}  "
            f"sharpe={best['sharpe']:+.3f}  total=${best['total_pnl_dollars']:+.0f}"
        )
    print(f"\nFull results: {os.path.join(OUTPUT_DIR, 'sweep_all.csv')}\n")


if __name__ == "__main__":
    run()
