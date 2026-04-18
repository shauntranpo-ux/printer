"""
Section 12.5 diagnostic.

Loads results/wfv_trades_ETH.parquet (and SOL) from the Section 12
run and produces:

1. Histogram of trades per window (should be 1 if dedup works)
2. Per-side breakdown: YES vs NO trade counts and hit rates
3. Signal distribution: distribution of raw_p_model across all trades
4. Suspicious-pattern detection: near-duplicate trades (same window,
   different eval_ts)

Usage:
    python scripts\\section12_5_diagnose.py
"""

from __future__ import annotations
import json
import sys
from collections import Counter
from pathlib import Path

import pandas as pd


def diagnose_asset(asset: str):
    path = Path(f"results/wfv_trades_{asset}.parquet")
    if not path.exists():
        print(f"  {asset}: no parquet file at {path}")
        return

    df = pd.read_parquet(path)
    n = len(df)
    print(f"\n== {asset}: {n:,} trades ==")

    if n == 0:
        return

    # ── 1. Trades per window distribution ─────────────────────────────
    if "window_start_ts" in df.columns:
        per_window = df.groupby("window_start_ts").size()
        window_counts = per_window.value_counts().sort_index()
        print(f"  trades_per_window distribution:")
        for count, freq in window_counts.items():
            print(f"    {count} trades/window: {freq:,} windows")

        unique_windows = len(per_window)
        if n > unique_windows:
            print(f"  WARNING: {n - unique_windows:,} trades are DUPLICATES "
                  f"(multiple trades per window — dedup broken)")
        else:
            print(f"  OK: Every trade is a unique window")
    else:
        print("  (no window_start_ts column — can't check dedup)")

    # ── 2. Per-side breakdown ─────────────────────────────────────────
    if "side" in df.columns and "outcome" in df.columns:
        for side in ["yes", "no"]:
            side_df = df[df["side"] == side]
            if len(side_df) > 0:
                wins = (side_df["outcome"] == "win").sum()
                hit = wins / len(side_df)
                pnl = side_df["pnl_dollars"].sum() if "pnl_dollars" in df.columns else 0
                print(f"  {side.upper()} side: {len(side_df):6,} trades, "
                      f"{hit*100:5.1f}% hit, ${pnl:+,.0f} PnL")

    # ── 3. Model probability distribution ─────────────────────────────
    if "raw_p_model" in df.columns:
        p = df["raw_p_model"].dropna()
        if len(p) > 0:
            print(f"  raw_p_model: mean={p.mean():.3f} median={p.median():.3f} "
                  f"min={p.min():.3f} max={p.max():.3f}")
            bins = [0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 1.0]
            for i in range(len(bins) - 1):
                lo, hi = bins[i], bins[i + 1]
                count = ((p >= lo) & (p < hi)).sum()
                print(f"    [{lo:.1f}, {hi:.1f}): {count:6,} "
                      f"({count / len(p) * 100:4.1f}%)")

    # ── 4. Calibrated vs raw divergence ───────────────────────────────
    if "calibrated_p_model" in df.columns and "raw_p_model" in df.columns:
        diff = (df["calibrated_p_model"] - df["raw_p_model"]).abs()
        if diff.max() > 0.01:
            print(f"  calibration applied: max |cal - raw| = {diff.max():.3f}")
        else:
            print(f"  calibration: identity (no meaningful correction)")

    # ── 5. Entry price distribution ───────────────────────────────────
    if "entry_price_cents" in df.columns:
        ep = df["entry_price_cents"]
        print(f"  entry_price_cents: mean={ep.mean():.1f} "
              f"median={ep.median():.1f} (ask side)")
        cheap = (ep < 30).sum()
        mid = ((ep >= 30) & (ep < 70)).sum()
        expensive = (ep >= 70).sum()
        print(f"    <30c: {cheap:,}   30-70c: {mid:,}   70+: {expensive:,}")


def main():
    sys.path.insert(0, ".")

    for asset in ["BTC", "ETH", "SOL", "XRP", "DOGE"]:
        diagnose_asset(asset)

    print("\nDone.")


if __name__ == "__main__":
    main()
