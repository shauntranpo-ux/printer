#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, io
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
"""
generate_bv3_table.py — Build per-asset BV3 probability tables from 1-minute CSV.

Outputs two JSON files per asset:
  bv3_tables/{SYMBOL}_bv3_full.json      — all available data (for live trading)
  bv3_tables/{SYMBOL}_bv3_pre2023.json   — pre-2023 only (for honest backtesting)

Usage:
    python generate_bv3_table.py --asset BTC
    python generate_bv3_table.py --asset BTC --csv /path/to/file.csv
    python generate_bv3_table.py --all      # process all assets with default CSV paths
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BV3_DIR  = os.path.join(BASE_DIR, "bv3_tables")
DATA_DIR = os.path.join(BASE_DIR, "data")

# Distance bucket upper bounds (as fractions, NOT percent).
# 12 bounds → 13 buckets (last bucket is open-ended: >= 0.0050 = 0.50%)
#   [0.01%, 0.02%, 0.03%, 0.05%, 0.07%, 0.10%, 0.15%, 0.20%, 0.25%, 0.30%, 0.40%, 0.50%, inf]
DIST_BOUNDS = [
    0.0001, 0.0002, 0.0003, 0.0005, 0.0007,
    0.0010, 0.0015, 0.0020, 0.0025, 0.0030,
    0.0040, 0.0050,
]

MINUTES        = list(range(1, 14))   # 1..13 minutes remaining (13 columns)
WINDOW_MINUTES = 15                    # Kalshi 15-minute window

# Strike price increments per asset (native currency)
STRIKE_INCREMENTS = {
    "BTC":  1000.0,
    "ETH":    25.0,
    "SOL":     1.0,
    "XRP":     0.01,
    "DOGE":    0.001,
}

# Default CSV search paths (new standard location first, then legacy BTC location)
def _default_csv(asset: str) -> str:
    standard = os.path.join(DATA_DIR, f"{asset}_1m.csv")
    if os.path.exists(standard):
        return standard
    if asset == "BTC":
        legacy = r"C:\Users\alxnt\Downloads\d5ae29c4-33c6-11f1-b1e7-6dda37cfa7b9\binance_api_BTCUSDT_1m.csv"
        if os.path.exists(legacy):
            return legacy
    return standard  # return standard path even if missing — caller will error clearly


def nearest_strike(price: float, increment: float) -> float:
    return round(price / increment) * increment


def dist_bucket(distance_frac: float) -> int:
    """Return 0-based row index for a given distance fraction."""
    for i, bound in enumerate(DIST_BOUNDS):
        if distance_frac < bound:
            return i
    return len(DIST_BOUNDS)  # 12 → last (open-ended) bucket


def build_table(df: pd.DataFrame, symbol: str, label: str) -> dict:
    """
    Build a BV3 probability table from a 1-minute price DataFrame.

    df must have columns: open_time (datetime64 UTC-aware), close (float).
    Returns a dict suitable for JSON serialisation.
    """
    increment = STRIKE_INCREMENTS[symbol]
    n_rows    = len(DIST_BOUNDS) + 1   # 13 distance buckets
    n_cols    = len(MINUTES)            # 13 minute columns

    wins  = [[0] * n_cols for _ in range(n_rows)]
    total = [[0] * n_cols for _ in range(n_rows)]

    df = df.sort_values("open_time").reset_index(drop=True)
    df["window_start"] = df["open_time"].dt.floor(f"{WINDOW_MINUTES}min")

    windows_seen = 0
    observations = 0

    for ws, grp in df.groupby("window_start", sort=True):
        grp = grp.reset_index(drop=True)
        if len(grp) < WINDOW_MINUTES:
            continue  # incomplete window — skip

        windows_seen += 1
        grp = grp.iloc[:WINDOW_MINUTES]

        # Outcome: close price of the LAST minute in the window
        outcome_price = float(grp["close"].iloc[-1])

        # At each minute remaining t (1..13):
        # observation is at row index = WINDOW_MINUTES - t (0-indexed from window start)
        for t in MINUTES:
            obs_idx = WINDOW_MINUTES - t
            if obs_idx < 0 or obs_idx >= len(grp):
                continue

            price  = float(grp["close"].iloc[obs_idx])
            strike = nearest_strike(price, increment)
            if strike <= 0:
                continue

            dist_frac = abs(price - strike) / strike
            above     = price > strike

            # stayed_same_side: price ended on the same side of strike as at observation
            outcome_above = outcome_price > strike
            stayed = (above == outcome_above)

            row = dist_bucket(dist_frac)
            col = t - 1  # 0-indexed

            total[row][col] += 1
            if stayed:
                wins[row][col] += 1
            observations += 1

    # Convert to probability table; fill sparse buckets with conservative defaults
    table    = []
    min_count = float("inf")
    for row in range(n_rows):
        prob_row = []
        for col in range(n_cols):
            n = total[row][col]
            if n > 0:
                prob_row.append(round(wins[row][col] / n, 4))
                if n < min_count:
                    min_count = n
            else:
                # Far-from-strike buckets rarely have observations; use 0.99 default
                # Near-strike with no data: use 0.85 (conservative)
                prob_row.append(0.99 if row >= 8 else 0.85)
        table.append(prob_row)

    if min_count == float("inf"):
        min_count = 0

    data_start = str(df["open_time"].min()) if len(df) else "N/A"
    data_end   = str(df["open_time"].max()) if len(df) else "N/A"

    return {
        "symbol":            symbol,
        "label":             label,
        "strike_increment":  increment,
        "dist_bounds":       DIST_BOUNDS,
        "minutes":           MINUTES,
        "table":             table,
        "metadata": {
            "generated_at":       datetime.now(timezone.utc).isoformat(),
            "data_start":         data_start,
            "data_end":           data_end,
            "total_windows":      windows_seen,
            "total_observations": observations,
            "min_bucket_count":   int(min_count),
        },
    }


def generate_for_asset(symbol: str, csv_path: str) -> bool:
    """
    Load CSV, build full and pre-2023 tables, write JSON, print comparison.
    Returns True on success.
    """
    if not os.path.exists(csv_path):
        print(f"[{symbol}] CSV not found: {csv_path} — skipping")
        return False

    print(f"\n{'='*60}")
    print(f"[{symbol}] Loading {csv_path} ...")

    # Parse — handle multiple timestamp formats:
    #   "time"      column with Unix seconds (legacy Binance export)
    #   "open_time" column with Unix ms integers
    #   "open_time" column with ISO strings ("2023-01-01T00:00:00+00:00")
    df = pd.read_csv(csv_path)

    # Normalise column name: "time" → "open_time"
    if "time" in df.columns and "open_time" not in df.columns:
        df = df.rename(columns={"time": "open_time"})

    if "open_time" not in df.columns:
        print(f"[{symbol}] ERROR: CSV missing 'open_time'/'time' column. Columns: {list(df.columns)}")
        return False

    # Detect timestamp format from first non-null value
    sample = str(df["open_time"].dropna().iloc[0])
    if sample.lstrip("-").isdigit():
        n_digits = len(sample.lstrip("-"))
        if n_digits <= 10:
            # Unix seconds
            df["open_time"] = pd.to_datetime(df["open_time"], unit="s", utc=True)
        else:
            # Unix milliseconds
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    else:
        # ISO string
        df["open_time"] = pd.to_datetime(df["open_time"], utc=True)

    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])

    print(f"[{symbol}] {len(df):,} rows | {df['open_time'].min()} -> {df['open_time'].max()}")

    os.makedirs(BV3_DIR, exist_ok=True)

    # Full table
    print(f"[{symbol}] Building FULL table ...")
    full_tbl  = build_table(df, symbol, "full")
    full_path = os.path.join(BV3_DIR, f"{symbol}_bv3_full.json")
    with open(full_path, "w") as f:
        json.dump(full_tbl, f, indent=2)
    print(f"[{symbol}] Wrote {full_path}  ({full_tbl['metadata']['total_windows']:,} windows)")

    # Pre-2023 table
    cutoff  = pd.Timestamp("2023-01-01", tz="UTC")
    df_pre  = df[df["open_time"] < cutoff]
    if len(df_pre) < 1000:
        print(f"[{symbol}] WARNING: only {len(df_pre)} pre-2023 rows — table may be sparse")

    print(f"[{symbol}] Building PRE-2023 table ({len(df_pre):,} rows) ...")
    pre_tbl  = build_table(df_pre, symbol, "pre2023")
    pre_path = os.path.join(BV3_DIR, f"{symbol}_bv3_pre2023.json")
    with open(pre_path, "w") as f:
        json.dump(pre_tbl, f, indent=2)
    print(f"[{symbol}] Wrote {pre_path}  ({pre_tbl['metadata']['total_windows']:,} windows)")

    # Comparison at 6 minutes remaining (col index 5)
    col6 = 5
    bucket_labels = [
        "0.00-0.01%", "0.01-0.02%", "0.02-0.03%", "0.03-0.05%",
        "0.05-0.07%", "0.07-0.10%", "0.10-0.15%", "0.15-0.20%",
        "0.20-0.25%", "0.25-0.30%", "0.30-0.40%", "0.40-0.50%", "0.50%+",
    ]
    print(f"\n[{symbol}] Table comparison (full vs pre-2023) @ 6-min remaining:")
    print(f"  {'Distance':25} {'Full':>8} {'Pre-2023':>10} {'Delta':>8}")
    for i, lbl in enumerate(bucket_labels):
        if i < len(full_tbl["table"]) and i < len(pre_tbl["table"]):
            fv    = full_tbl["table"][i][col6]
            pv    = pre_tbl["table"][i][col6]
            delta = fv - pv
            flag  = "  ← LARGE DIFF" if abs(delta) > 0.05 else ""
            print(f"  {lbl:25} {fv:>8.3f} {pv:>10.3f} {delta:>+8.3f}{flag}")

    return True


def main():
    parser = argparse.ArgumentParser(description="Generate per-asset BV3 probability tables")
    parser.add_argument("--asset", choices=list(STRIKE_INCREMENTS),
                        help="Single asset to process")
    parser.add_argument("--csv",   help="Path to 1-minute CSV (overrides default for --asset)")
    parser.add_argument("--all",   action="store_true", help="Process all assets")
    args = parser.parse_args()

    if args.all:
        for symbol in STRIKE_INCREMENTS:
            generate_for_asset(symbol, _default_csv(symbol))
    elif args.asset:
        csv_path = args.csv or _default_csv(args.asset)
        generate_for_asset(args.asset, csv_path)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
