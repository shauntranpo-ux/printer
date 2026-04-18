#!/usr/bin/env python3
"""
regen_bv3_clean.py — regenerate BV3 tables with a clean train/test split.

Reads data/split_config.json for train_end, then rebuilds *_bv3_full.json
for all assets using only pre-split data.

Usage:
    python scripts/regen_bv3_clean.py [--dry-run] [--asset BTC]

Flags:
    --dry-run    Print row counts and date ranges; do NOT write files.
    --asset      Process a single asset (default: all 5).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import pandas as pd

# Resolve project root regardless of where the script is invoked from
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from generate_bv3_table import (
    DIST_BOUNDS,
    MINUTES,
    STRIKE_INCREMENTS,
    build_table,
)

DATA_DIR   = os.path.join(ROOT, "data")
BV3_DIR    = os.path.join(ROOT, "bv3_tables")
SPLIT_CFG  = os.path.join(DATA_DIR, "split_config.json")
ASSETS     = ["BTC", "ETH", "SOL", "XRP", "DOGE"]


def _load_split_config() -> pd.Timestamp:
    with open(SPLIT_CFG) as f:
        cfg = json.load(f)
    train_end = cfg["train_end"]
    # inclusive — cutoff is start of the day AFTER train_end
    cutoff = pd.Timestamp(train_end, tz="UTC") + pd.Timedelta(days=1)
    print(f"[config] train_end={train_end}  cutoff={cutoff.date()} (exclusive)")
    return cutoff


def _load_csv(asset: str) -> pd.DataFrame:
    path = os.path.join(DATA_DIR, f"{asset}_1m.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Missing CSV: {path}")
    df = pd.read_csv(path)

    # Normalise column name
    if "time" in df.columns and "open_time" not in df.columns:
        df = df.rename(columns={"time": "open_time"})
    if "open_time" not in df.columns:
        raise ValueError(f"[{asset}] CSV missing 'open_time'/'time' column — found: {list(df.columns)}")

    # Detect timestamp format from first non-null value
    sample = str(df["open_time"].dropna().iloc[0])
    if sample.lstrip("-").isdigit():
        val = int(sample)
        if val > 1e12:
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        else:
            df["open_time"] = pd.to_datetime(df["open_time"], unit="s", utc=True)
    else:
        df["open_time"] = pd.to_datetime(df["open_time"], utc=True)

    return df


def process_asset(asset: str, cutoff: pd.Timestamp, dry_run: bool) -> bool:
    print(f"\n[{asset}] Loading CSV ...")
    try:
        df = _load_csv(asset)
    except FileNotFoundError as e:
        print(f"[{asset}] SKIP: {e}")
        return False

    full_rows  = len(df)
    df_train   = df[df["open_time"] < cutoff].copy()
    train_rows = len(df_train)

    print(
        f"[{asset}] {train_rows:,} train rows / {full_rows:,} total  "
        f"({train_rows/full_rows*100:.1f}%)  "
        f"{df_train['open_time'].min().date()} → {df_train['open_time'].max().date()}"
    )

    if train_rows < 1000:
        print(f"[{asset}] WARNING: only {train_rows} training rows — table will be very sparse")

    if dry_run:
        print(f"[{asset}] --dry-run: skipping table build")
        return True

    print(f"[{asset}] Building clean BV3 table ...")
    table = build_table(df_train, asset, "full_clean")

    out_path = os.path.join(BV3_DIR, f"{asset}_bv3_full.json")
    with open(out_path, "w") as f:
        json.dump(table, f, indent=2)

    meta = table["metadata"]
    print(
        f"[{asset}] Wrote {out_path}  "
        f"({meta['total_windows']:,} windows, "
        f"{meta['total_observations']:,} observations, "
        f"min_bucket={meta['min_bucket_count']})"
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print stats only, do not write")
    parser.add_argument("--asset",   help="Process one asset only (default: all)")
    args = parser.parse_args()

    cutoff = _load_split_config()

    assets = [args.asset.upper()] if args.asset else ASSETS
    unknown = [a for a in assets if a not in STRIKE_INCREMENTS]
    if unknown:
        print(f"ERROR: unknown assets {unknown}. Valid: {list(STRIKE_INCREMENTS)}")
        sys.exit(1)

    os.makedirs(BV3_DIR, exist_ok=True)

    results = {}
    for asset in assets:
        ok = process_asset(asset, cutoff, dry_run=args.dry_run)
        results[asset] = "ok" if ok else "skip"

    print("\n── Summary ─────────────────────────────────────────")
    for asset, status in results.items():
        print(f"  {asset}: {status}")
    if args.dry_run:
        print("\n[dry-run] No files written. Remove --dry-run to generate tables.")


if __name__ == "__main__":
    main()
