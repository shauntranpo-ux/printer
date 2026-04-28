"""Sweep Supertrend ATR period × multiplier grid across all 15m assets."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backtesting"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import pandas as pd
import warnings
warnings.filterwarnings("ignore")

from backtesting.data.loaders import load_bars
from backtesting.simulation.fifteen_min_backtest import run_fifteen_min_backtest

ASSETS = ["BTC", "ETH", "SOL", "XRP"]
DATE_START, DATE_END = "2025-01-01", "2025-06-30"
PERIOD_GRID     = [5, 7, 10, 14, 20]
MULTIPLIER_GRID = [2.0, 2.5, 3.0, 3.5, 4.0]

def run():
    results = []
    for asset in ASSETS:
        print(f"\n{'='*60}\n  {asset}\n{'='*60}")
        try:
            bars = load_bars(asset, start_date=DATE_START, end_date=DATE_END)
        except Exception as e:
            print(f"  [SKIP] {e}"); continue
        bars = bars.rename(columns={"open_time": "timestamp"}) if "open_time" in bars.columns else bars
        bars["timestamp"] = pd.to_datetime(bars["timestamp"], utc=True)
        print(f"  Bars: {len(bars):,}")

        for period in PERIOD_GRID:
            for mult in MULTIPLIER_GRID:
                try:
                    df = run_fifteen_min_backtest(
                        bars=bars, asset=asset, min_ev=0.0,
                        stake_dollars=25.0, entry_minute=5,
                        min_entry_price_cents=5.0, max_entry_price_cents=75.0,
                        supertrend_atr_period=period,
                        supertrend_atr_multiplier=mult,
                    )
                except Exception as e:
                    print(f"  p={period:2d} m={mult:.1f}  ERROR: {e}"); continue

                if df.empty:
                    print(f"  p={period:2d} m={mult:.1f}  no trades")
                    results.append({"asset": asset, "period": period, "mult": mult,
                                     "n_trades": 0, "win_rate": None, "total_pnl": None, "sharpe": None})
                    continue

                pnl = df["pnl"].values
                wr, tot, avg = df["win"].mean(), pnl.sum(), pnl.mean()
                sharpe = avg / (pnl.std() + 1e-10)
                print(f"  p={period:2d} m={mult:.1f}  trades={len(df):>5}  "
                      f"WR={wr:.1%}  total=${tot*25:.0f}  avg={avg*100:+.2f}c  sharpe={sharpe:.2f}")
                results.append({"asset": asset, "period": period, "mult": mult,
                                 "n_trades": len(df), "win_rate": round(wr, 4),
                                 "total_pnl": round(tot*25, 2), "sharpe": round(sharpe, 3)})

    print(f"\n\n{'='*60}")
    print("SUMMARY — Best Supertrend params per asset (by Sharpe)")
    print(f"{'='*60}")
    df_all = pd.DataFrame(results)
    df_all = df_all[df_all["n_trades"] > 0].copy()
    for asset in ASSETS:
        sub = df_all[df_all["asset"] == asset]
        if sub.empty: print(f"  {asset}: no data"); continue
        best = sub.loc[sub["sharpe"].idxmax()]
        print(f"  {asset}:  best period={int(best['period'])} mult={best['mult']:.1f}  "
              f"trades={int(best['n_trades'])}  WR={best['win_rate']:.1%}  "
              f"total=${best['total_pnl']:.0f}  sharpe={best['sharpe']:.2f}")
    print()

if __name__ == "__main__":
    run()
