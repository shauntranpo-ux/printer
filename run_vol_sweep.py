"""
Vol-ratio threshold sweep for all 15m assets.
Runs run_fifteen_min_backtest() across a grid of vol_ratio_threshold values
to find the optimal buffer-durability gate per asset.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backtesting"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings("ignore")

from backtesting.data.loaders import load_bars
from backtesting.simulation.fifteen_min_backtest import run_fifteen_min_backtest

ASSETS = ["BTC", "ETH", "SOL", "XRP"]

VOL_GRID = [0.5, 0.8, 1.0, 1.2, 1.5, 1.8, 2.0, 2.5, 3.0, 999.0]

DATE_START = "2025-01-01"
DATE_END   = "2025-06-30"

def run_sweep():
    results = []

    for asset in ASSETS:
        print(f"\n{'='*60}")
        print(f"  {asset}")
        print(f"{'='*60}")

        try:
            bars = load_bars(asset, start_date=DATE_START, end_date=DATE_END)
        except Exception as e:
            print(f"  [SKIP] Could not load bars: {e}")
            continue

        bars = bars.rename(columns={"open_time": "timestamp"}) if "open_time" in bars.columns else bars
        if "timestamp" not in bars.columns:
            print(f"  [SKIP] No timestamp column.")
            continue

        bars["timestamp"] = pd.to_datetime(bars["timestamp"], utc=True)
        print(f"  Bars: {len(bars):,}  ({bars['timestamp'].min().date()} to {bars['timestamp'].max().date()})")

        for vr in VOL_GRID:
            try:
                df = run_fifteen_min_backtest(
                    bars=bars,
                    asset=asset,
                    min_ev=0.0,
                    stake_dollars=25.0,
                    yes_ask_cents=50.0,
                    no_ask_cents=50.0,
                    min_entry_price_cents=5.0,
                    max_entry_price_cents=75.0,
                    vol_ratio_threshold=vr,
                )
            except Exception as e:
                print(f"  vr={vr:.1f}  ERROR: {e}")
                continue

            if df.empty:
                print(f"  vr={vr:<5.1f}  no trades")
                results.append({"asset": asset, "vol_ratio": vr, "n_trades": 0,
                                 "win_rate": None, "total_pnl": None, "sharpe": None})
                continue

            pnl = df["pnl"].values
            wr  = df["win"].mean()
            tot = pnl.sum()
            avg = pnl.mean()
            std = pnl.std()
            sharpe = avg / (std + 1e-10)

            print(
                f"  vr={vr:<5.1f}  trades={len(df):>5}  "
                f"WR={wr:.1%}  total=${tot*25:.0f}  "
                f"avg={avg*100:+.2f}c  sharpe={sharpe:.2f}"
            )
            results.append({
                "asset": asset, "vol_ratio": vr, "n_trades": len(df),
                "win_rate": round(wr, 4),
                "total_pnl": round(tot * 25, 2),
                "avg_pnl_cents": round(avg * 100, 3),
                "sharpe": round(sharpe, 3),
            })

    print(f"\n\n{'='*60}")
    print("SUMMARY — Best vol_ratio threshold per asset (by Sharpe)")
    print(f"{'='*60}")

    df_all = pd.DataFrame(results)
    df_all = df_all[df_all["n_trades"] > 0].copy()

    for asset in ASSETS:
        sub = df_all[df_all["asset"] == asset]
        if sub.empty:
            print(f"  {asset}: no data")
            continue
        best = sub.loc[sub["sharpe"].idxmax()]
        print(
            f"  {asset}:  best_vr={best['vol_ratio']:.1f}  "
            f"trades={int(best['n_trades'])}  WR={best['win_rate']:.1%}  "
            f"total=${best['total_pnl']:.0f}  sharpe={best['sharpe']:.2f}"
        )

    print()
    return df_all


if __name__ == "__main__":
    run_sweep()
