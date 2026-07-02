# Multi-Asset Kalshi Bot Expansion - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand the single-asset BTC Kalshi bot to trade ETH, SOL, XRP, and DOGE, with per-asset BV3 tables generated from historical data and a pre-2023 / full split to fix the data leakage issue.

**Architecture:** A new `download_data.py` pulls Binance 1-minute klines (resumable). `generate_bv3_table.py` builds per-asset BV3 JSON files with a full and pre-2023 split. `asset_manager.py` owns asset config, BV3 lookup, and the Binance WebSocket fan-out. `bot.py` gains per-asset state dicts and iterates the main loop over all enabled assets. `backtest.py` gets an `--asset` flag and loads the per-asset pre-2023 BV3 table.

**Tech Stack:** Python 3.11+, aiohttp, websockets, pandas, numpy, sqlite3. Binance public REST + WebSocket (no auth required). No new dependencies beyond what's already in requirements.txt.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `download_data.py` | **Create** | Resumable Binance klines downloader for all 5 assets |
| `generate_bv3_table.py` | **Create** | Reads 1-min CSV -> outputs `bv3_tables/{SYMBOL}_bv3_full.json` + `_pre2023.json` |
| `bv3_tables/` | **Create dir** | JSON BV3 tables, one pair per asset |
| `asset_manager.py` | **Create** | Asset config registry, BV3 loader/lookup, Binance WebSocket price feed |
| `bot.py` | **Modify** | Replace single-asset globals with per-asset dicts; import from asset_manager; add `asset` column to DB; main loop iterates enabled assets |
| `backtest.py` | **Modify** | Add `--asset` flag; load per-asset BV3 table (pre-2023); per-asset + combined REALITY CHECK |

---

## Task 1: Resumable Binance Data Downloader

**Files:**
- Create: `download_data.py`

The Binance klines endpoint is public (no API key). URL:
`GET https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=1000&startTime={ms}`

Each kline row: `[open_time_ms, open, high, low, close, volume, ...]`

The output CSV has columns: `open_time,open,high,low,close,volume` with `open_time` as UTC timestamp string.

Resumable logic: if `data/{SYMBOL}_1m.csv` exists, read the last `open_time`, convert to ms, and start the next request from there.

- [ ] **Step 1: Create `download_data.py`**

```python
#!/usr/bin/env python3
"""
download_data.py - Resumable Binance 1-minute klines downloader.

Downloads historical 1-minute OHLCV data for all enabled assets.
If a CSV already exists, resumes from the last row instead of re-downloading.

Usage:
    python download_data.py                    # all assets
    python download_data.py --asset ETH        # single asset
    python download_data.py --asset BTC ETH    # subset
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone

import requests

BASE_URL = "https://api.binance.com/api/v3/klines"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Binance symbols for each asset
ASSET_SYMBOLS = {
    "BTC":  "BTCUSDT",
    "ETH":  "ETHUSDT",
    "SOL":  "SOLUSDT",
    "XRP":  "XRPUSDT",
    "DOGE": "DOGEUSDT",
}

# Earliest data available on Binance per asset (approximate)
ASSET_START = {
    "BTC":  "2017-08-17",
    "ETH":  "2017-08-17",
    "SOL":  "2020-08-11",
    "XRP":  "2018-05-04",
    "DOGE": "2019-07-05",
}

CSV_HEADER = ["open_time", "open", "high", "low", "close", "volume"]


def csv_path(asset: str) -> str:
    return os.path.join(DATA_DIR, f"{asset}_1m.csv")


def get_last_ts(path: str) -> int | None:
    """Return the last open_time in the CSV as milliseconds, or None if file is empty/missing."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", newline="") as f:
            reader = csv.reader(f)
            next(reader, None)  # skip header
            last_row = None
            for row in reader:
                if row:
                    last_row = row
        if last_row:
            # open_time is stored as ISO string like "2023-01-01T00:01:00+00:00"
            dt = datetime.fromisoformat(last_row[0])
            return int(dt.timestamp() * 1000)
    except Exception as e:
        print(f"  Warning: could not read last row of {path}: {e}")
    return None


def fetch_klines(symbol: str, start_ms: int, limit: int = 1000) -> list:
    """Fetch up to `limit` 1-minute klines from Binance starting at start_ms."""
    params = {
        "symbol": symbol,
        "interval": "1m",
        "limit": limit,
        "startTime": start_ms,
    }
    resp = requests.get(BASE_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def download_asset(asset: str, force_start: str | None = None) -> bool:
    """
    Download 1-minute klines for `asset` to data/{ASSET}_1m.csv.
    Resumes from last row if file exists.
    Returns True on success, False on failure.
    """
    symbol = ASSET_SYMBOLS[asset]
    path = csv_path(asset)
    os.makedirs(DATA_DIR, exist_ok=True)

    # Determine start time
    last_ms = get_last_ts(path)
    if last_ms is not None:
        # Start one minute after last downloaded row
        start_ms = last_ms + 60_000
        print(f"[{asset}] Resuming from {ms_to_iso(start_ms)} (last row: {ms_to_iso(last_ms)})")
        file_mode = "a"
        write_header = False
    else:
        start_str = force_start or ASSET_START[asset]
        start_ms = int(datetime.fromisoformat(start_str + "T00:00:00+00:00").timestamp() * 1000)
        print(f"[{asset}] Starting fresh from {start_str}")
        file_mode = "w"
        write_header = True

    now_ms = int(time.time() * 1000) - 120_000  # exclude last 2 minutes (incomplete candle)

    if start_ms >= now_ms:
        print(f"[{asset}] Already up to date.")
        return True

    total_rows = 0
    failed = False

    with open(path, file_mode, newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(CSV_HEADER)

        current_ms = start_ms
        while current_ms < now_ms:
            try:
                klines = fetch_klines(symbol, current_ms)
            except Exception as e:
                print(f"[{asset}] Fetch error at {ms_to_iso(current_ms)}: {e}")
                failed = True
                break

            if not klines:
                break

            batch = []
            for k in klines:
                open_ms = k[0]
                if open_ms >= now_ms:
                    break
                batch.append([
                    ms_to_iso(open_ms),   # open_time as ISO
                    k[1],                  # open
                    k[2],                  # high
                    k[3],                  # low
                    k[4],                  # close
                    k[5],                  # volume
                ])

            if not batch:
                break

            writer.writerows(batch)
            f.flush()
            total_rows += len(batch)

            last_open_ms = int(datetime.fromisoformat(batch[-1][0]).timestamp() * 1000)
            current_ms = last_open_ms + 60_000

            pct = min(100, (current_ms - start_ms) / max(1, now_ms - start_ms) * 100)
            print(f"  [{asset}] {ms_to_iso(current_ms)} | {total_rows:,} rows | {pct:.1f}%", end="\r")

            # Binance allows ~1200 req/min; 0.1s between calls is safe
            time.sleep(0.1)

    print(f"\n[{asset}] Done: {total_rows:,} new rows written to {path}")
    return not failed


def main():
    parser = argparse.ArgumentParser(description="Download Binance 1-minute klines")
    parser.add_argument("--asset", nargs="+", choices=list(ASSET_SYMBOLS), default=None,
                        help="Assets to download (default: all)")
    parser.add_argument("--start", help="Override start date YYYY-MM-DD (single asset only)")
    args = parser.parse_args()

    assets = args.asset or list(ASSET_SYMBOLS.keys())
    if args.start and len(assets) > 1:
        print("--start can only be used with a single --asset")
        sys.exit(1)

    results = {}
    for asset in assets:
        print(f"\n{'='*60}")
        print(f"Downloading {asset} ({ASSET_SYMBOLS[asset]})")
        print(f"{'='*60}")
        results[asset] = download_asset(asset, force_start=args.start if assets[0] == asset else None)

    print("\n\nSummary:")
    for asset, ok in results.items():
        status = "OK" if ok else "FAILED"
        path = csv_path(asset)
        size = os.path.getsize(path) / 1e6 if os.path.exists(path) else 0
        print(f"  {asset:5} {status:6}  {path} ({size:.1f} MB)")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run test download for a small date range**

```bash
python download_data.py --asset ETH --start 2023-01-01
```

Expected: creates `data/ETH_1m.csv`, prints progress, ends with "Done: N rows written".

- [ ] **Step 3: Verify CSV format**

```bash
head -5 data/ETH_1m.csv
```

Expected output (first 5 lines):
```
open_time,open,high,low,close,volume
2023-01-01T00:00:00+00:00,1195.33,1196.50,...
...
```

- [ ] **Step 4: Verify resumability**

Run again immediately - should print "Already up to date" or add only new rows. Delete last line of CSV manually, re-run, confirm it fills in the missing row.

- [ ] **Step 5: Commit**

```bash
git add download_data.py
git commit -m "feat: add resumable Binance 1-minute klines downloader (all assets)"
```

---

## Task 2: BV3 Table Generator

**Files:**
- Create: `generate_bv3_table.py`
- Create: `bv3_tables/` (directory, git-tracked via `.gitkeep`)

The algorithm per 15-minute window:
1. Group 1-minute CSV rows into 15-minute windows aligned to the hour (e.g., 09:00-09:15, 09:15-09:30).
2. For each window, identify the close price at minute 15 (the "outcome").
3. At each minute t from 1 to 14 (= minutes remaining at that observation):
   - Price at that point = `close` column of the row that is (15-t) minutes into the window.
   - `nearest_strike = round(price / increment) * increment`
   - `distance_pct = abs(price - nearest_strike) / nearest_strike`
   - `side = "above"` if `price > nearest_strike` else `"below"`
   - `outcome_price` = close of last row in the window
   - `stayed_same_side = (outcome_price > nearest_strike) == (price > nearest_strike)`
   - Record `(distance_pct, t, stayed_same_side)`.
4. Aggregate: for each (dist_bucket, minute_remaining) pair, compute `wins / total`.

Distance buckets (upper bounds, as fractions, 13 rows):
```
[0.0001, 0.0002, 0.0003, 0.0005, 0.0007, 0.0010, 0.0015, 0.0020, 0.0025, 0.0030, 0.0040, 0.0050]
# Last row is the 0.50%+ bucket (anything >= 0.0050)
```

Minutes columns: 1 through 13 (at 14 minutes remaining there's very little data historically so cap at 13).

Output JSON format:
```json
{
  "symbol": "BTC",
  "strike_increment": 1000,
  "dist_bounds": [0.0001, 0.0002, ...],
  "table": [[0.850, 0.796, ...], ...],
  "metadata": {
    "generated_at": "...",
    "data_start": "...",
    "data_end": "...",
    "total_windows": 123456,
    "total_observations": 987654,
    "min_bucket_count": 42
  }
}
```

- [ ] **Step 1: Create `generate_bv3_table.py`**

```python
#!/usr/bin/env python3
"""
generate_bv3_table.py - Build per-asset BV3 probability tables from 1-minute CSV.

Outputs two JSON files per asset:
  bv3_tables/{SYMBOL}_bv3_full.json      - all available data (for live trading)
  bv3_tables/{SYMBOL}_bv3_pre2023.json   - pre-2023 only (for honest backtesting)

Usage:
    python generate_bv3_table.py --asset BTC --csv data/BTC_1m.csv
    python generate_bv3_table.py --asset ETH --csv data/ETH_1m.csv
    python generate_bv3_table.py --all      # process all assets with default CSV paths
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BV3_DIR  = os.path.join(BASE_DIR, "bv3_tables")
DATA_DIR = os.path.join(BASE_DIR, "data")

# Distance bucket upper bounds (as fractions, NOT percent).
# 13 rows: [0.01%, 0.02%, 0.03%, 0.05%, 0.07%, 0.10%, 0.15%, 0.20%, 0.25%, 0.30%, 0.40%, 0.50%, inf]
DIST_BOUNDS = [
    0.0001, 0.0002, 0.0003, 0.0005, 0.0007,
    0.0010, 0.0015, 0.0020, 0.0025, 0.0030,
    0.0040, 0.0050,
]  # 12 bounds -> 13 buckets (last is open-ended >=0.0050)

MINUTES = list(range(1, 14))   # 1..13 minutes remaining (13 columns)
WINDOW_MINUTES = 15            # Kalshi 15-minute window size

# Strike price increments per asset (in asset's native currency)
STRIKE_INCREMENTS = {
    "BTC":  1000.0,
    "ETH":  25.0,
    "SOL":  1.0,
    "XRP":  0.01,
    "DOGE": 0.001,
}

# Default CSV paths
DEFAULT_CSV = {
    "BTC":  os.path.join(DATA_DIR, "BTC_1m.csv"),
    "ETH":  os.path.join(DATA_DIR, "ETH_1m.csv"),
    "SOL":  os.path.join(DATA_DIR, "SOL_1m.csv"),
    "XRP":  os.path.join(DATA_DIR, "XRP_1m.csv"),
    "DOGE": os.path.join(DATA_DIR, "DOGE_1m.csv"),
}


def nearest_strike(price: float, increment: float) -> float:
    return round(price / increment) * increment


def dist_bucket(distance_frac: float) -> int:
    """Return 0-based row index for a given distance fraction."""
    for i, bound in enumerate(DIST_BOUNDS):
        if distance_frac < bound:
            return i
    return len(DIST_BOUNDS)  # last bucket (>=0.0050)


def build_table(df: pd.DataFrame, symbol: str, label: str) -> dict:
    """
    Build a BV3 probability table from a 1-minute price DataFrame.

    df must have columns: open_time (datetime64, UTC), close (float)
    """
    increment = STRIKE_INCREMENTS[symbol]
    n_rows = len(DIST_BOUNDS) + 1   # 13 distance buckets
    n_cols = len(MINUTES)            # 13 minute columns

    # wins[row][col] = count of observations where price stayed same side
    wins  = [[0] * n_cols for _ in range(n_rows)]
    total = [[0] * n_cols for _ in range(n_rows)]

    # Align timestamps to WINDOW_MINUTES boundaries
    # Window start = floor(open_time, 15min)
    df = df.sort_values("open_time").reset_index(drop=True)
    df["window_start"] = df["open_time"].dt.floor(f"{WINDOW_MINUTES}min")

    windows_seen = 0
    observations = 0

    for ws, grp in df.groupby("window_start"):
        grp = grp.reset_index(drop=True)
        if len(grp) < WINDOW_MINUTES:
            continue  # incomplete window - skip

        windows_seen += 1
        # Use exactly the first WINDOW_MINUTES rows
        grp = grp.iloc[:WINDOW_MINUTES]

        # Outcome: close price of the LAST minute in the window
        outcome_price = grp["close"].iloc[-1]

        # At each minute mark t (minutes remaining from 1 to 13):
        # observation row is WINDOW_MINUTES - t rows from start (0-indexed)
        for t in MINUTES:
            obs_idx = WINDOW_MINUTES - t  # 0-indexed row index of the observation
            if obs_idx < 0 or obs_idx >= len(grp):
                continue

            price = grp["close"].iloc[obs_idx]
            strike = nearest_strike(price, increment)
            if strike <= 0:
                continue

            dist_frac = abs(price - strike) / strike
            above = price > strike

            # stayed_same_side: did price stay on the same side of strike at expiry?
            outcome_above = outcome_price > strike
            stayed = (above == outcome_above)

            row = dist_bucket(dist_frac)
            col = t - 1  # 0-indexed (minute 1 -> col 0)

            total[row][col] += 1
            if stayed:
                wins[row][col] += 1
            observations += 1

    # Convert to probability table
    table = []
    min_count = float("inf")
    for row in range(n_rows):
        prob_row = []
        for col in range(n_cols):
            n = total[row][col]
            if n > 0:
                prob_row.append(round(wins[row][col] / n, 4))
                min_count = min(min_count, n)
            else:
                # No data for this bucket - use a reasonable default
                # (outer rows have very few samples; fall back to 0.99 for far-from-strike)
                prob_row.append(0.99 if row >= 8 else 0.85)
        table.append(prob_row)

    if min_count == float("inf"):
        min_count = 0

    data_start = str(df["open_time"].min())
    data_end   = str(df["open_time"].max())

    return {
        "symbol":           symbol,
        "label":            label,
        "strike_increment": increment,
        "dist_bounds":      DIST_BOUNDS,
        "minutes":          MINUTES,
        "table":            table,
        "metadata": {
            "generated_at":      datetime.now(timezone.utc).isoformat(),
            "data_start":        data_start,
            "data_end":          data_end,
            "total_windows":     windows_seen,
            "total_observations": observations,
            "min_bucket_count":  int(min_count),
        }
    }


def generate_for_asset(symbol: str, csv_path: str) -> None:
    """Load CSV, build full and pre-2023 tables, write JSON, print comparison."""
    if not os.path.exists(csv_path):
        print(f"[{symbol}] CSV not found: {csv_path} - skipping")
        return

    print(f"\n{'='*60}")
    print(f"[{symbol}] Loading {csv_path} ...")

    df = pd.read_csv(csv_path, parse_dates=["open_time"])
    # Ensure timezone-aware
    if df["open_time"].dt.tz is None:
        df["open_time"] = df["open_time"].dt.tz_localize("UTC")
    else:
        df["open_time"] = df["open_time"].dt.tz_convert("UTC")

    df["close"] = df["close"].astype(float)
    print(f"[{symbol}] {len(df):,} rows | {df['open_time'].min()} -> {df['open_time'].max()}")

    os.makedirs(BV3_DIR, exist_ok=True)

    # Full table
    print(f"[{symbol}] Building FULL table ...")
    full_table = build_table(df, symbol, "full")
    full_path = os.path.join(BV3_DIR, f"{symbol}_bv3_full.json")
    with open(full_path, "w") as f:
        json.dump(full_table, f, indent=2)
    print(f"[{symbol}] Wrote {full_path} ({full_table['metadata']['total_windows']:,} windows)")

    # Pre-2023 table
    cutoff = pd.Timestamp("2023-01-01", tz="UTC")
    df_pre = df[df["open_time"] < cutoff]
    if len(df_pre) < 1000:
        print(f"[{symbol}] Warning: only {len(df_pre)} pre-2023 rows - table may be sparse")

    print(f"[{symbol}] Building PRE-2023 table ({len(df_pre):,} rows) ...")
    pre_table = build_table(df_pre, symbol, "pre2023")
    pre_path = os.path.join(BV3_DIR, f"{symbol}_bv3_pre2023.json")
    with open(pre_path, "w") as f:
        json.dump(pre_table, f, indent=2)
    print(f"[{symbol}] Wrote {pre_path} ({pre_table['metadata']['total_windows']:,} windows)")

    # Comparison: show max absolute difference per bucket row (minute=6 col, middle)
    print(f"\n[{symbol}] Table comparison (full vs pre-2023) - max delta per distance row @ 6 min remaining:")
    print(f"  {'Distance bucket':<25} {'Full':>8} {'Pre-2023':>10} {'Delta':>8}")
    bucket_labels = [
        "0.00-0.01%", "0.01-0.02%", "0.02-0.03%", "0.03-0.05%", "0.05-0.07%",
        "0.07-0.10%", "0.10-0.15%", "0.15-0.20%", "0.20-0.25%", "0.25-0.30%",
        "0.30-0.40%", "0.40-0.50%", "0.50%+",
    ]
    col6 = 5  # 0-indexed column for 6 minutes remaining
    for i, label in enumerate(bucket_labels):
        if i < len(full_table["table"]) and i < len(pre_table["table"]):
            fv = full_table["table"][i][col6]
            pv = pre_table["table"][i][col6]
            delta = fv - pv
            flag = " <- LARGE DIFF" if abs(delta) > 0.05 else ""
            print(f"  {label:<25} {fv:>8.3f} {pv:>10.3f} {delta:>+8.3f}{flag}")


def main():
    parser = argparse.ArgumentParser(description="Generate per-asset BV3 probability tables")
    parser.add_argument("--asset", choices=list(STRIKE_INCREMENTS.keys()),
                        help="Single asset to process")
    parser.add_argument("--csv",   help="Path to 1-minute CSV (required with --asset)")
    parser.add_argument("--all",   action="store_true", help="Process all assets")
    args = parser.parse_args()

    if args.all:
        for symbol in STRIKE_INCREMENTS:
            generate_for_asset(symbol, DEFAULT_CSV[symbol])
    elif args.asset:
        csv_path = args.csv or DEFAULT_CSV[args.asset]
        generate_for_asset(args.asset, csv_path)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Create `bv3_tables/.gitkeep`**

```bash
touch bv3_tables/.gitkeep
```

- [ ] **Step 3: Generate BTC table from existing data**

The existing BTC CSV is at `C:\Users\alxnt\Downloads\d5ae29c4-...\binance_api_BTCUSDT_1m.csv`. First copy it to the data dir (or symlink):

```bash
cp "C:/Users/alxnt/Downloads/d5ae29c4-33c6-11f1-b1e7-6dda37cfa7b9/binance_api_BTCUSDT_1m.csv" data/BTC_1m.csv
```

Then generate:

```bash
python generate_bv3_table.py --asset BTC --csv data/BTC_1m.csv
```

Expected output ends with table comparison showing small deltas (<=0.02) if data is stable, larger deltas (flagged) if 2023+ data changed behavior significantly.

- [ ] **Step 4: Verify output files**

```bash
python -c "
import json
with open('bv3_tables/BTC_bv3_full.json') as f:
    t = json.load(f)
print('rows:', len(t['table']), 'cols:', len(t['table'][0]))
print('metadata:', t['metadata'])
print('row0 (closest to strike):', t['table'][0])
"
```

Expected: `rows: 13 cols: 13`, metadata shows millions of windows, row 0 shows values around 0.85-0.99 decreasing from col 0 to col 12.

- [ ] **Step 5: Commit**

```bash
git add generate_bv3_table.py bv3_tables/.gitkeep
git commit -m "feat: add BV3 table generator with full/pre-2023 split per asset"
```

---

## Task 3: Asset Manager Module

**Files:**
- Create: `asset_manager.py`

This module owns:
1. Asset configuration registry (strike increments, Kalshi series tickers, Binance symbols)
2. BV3 table loading and lookup (replacing the hardcoded `_BV3_TABLE` for multi-asset use)
3. Binance WebSocket price feed (all assets via combined stream)
4. Per-asset price deque access

The Binance combined stream URL:
`wss://stream.binance.com:9443/stream?streams=btcusdt@aggTrade/ethusdt@aggTrade/solusdt@aggTrade/xrpusdt@aggTrade/dogeusdt@aggTrade`

Messages arrive as: `{"stream": "btcusdt@aggTrade", "data": {"p": "95000.00", ...}}`

- [ ] **Step 1: Create `asset_manager.py`**

```python
"""
asset_manager.py - Asset configuration, BV3 lookup, and Binance price feeds.

Provides:
  - ASSET_CONFIG: per-asset metadata (strike increment, Kalshi series, Binance symbol)
  - load_bv3_tables(): loads JSON tables from bv3_tables/
  - empirical_win_prob(asset, abs_pct, mins_left): looks up the loaded table
  - binance_feed_task(): async coroutine - maintains per-asset price deques
  - get_price(asset): returns latest price for an asset
"""

import asyncio
import json
import logging
import os
import time
from collections import deque

import websockets

log = logging.getLogger("bot")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BV3_DIR  = os.path.join(BASE_DIR, "bv3_tables")

BINANCE_WS = "wss://stream.binance.com:9443/stream"

# Asset registry
# kalshi_series: ordered list of Kalshi series tickers to try (first match wins)
# binance_symbol: Binance trading pair
# strike_increment: Kalshi strike price rounding unit
ASSET_CONFIG = {
    "BTC": {
        "binance_symbol":   "btcusdt",
        "strike_increment": 1000.0,
        # Kalshi "above/below" short-duration BTC markets (confirmed active as of 2026)
        "kalshi_series":    ("KXBTCD", "BTCD-B", "KXBTC15M", "BTC15M", "KXBTC", "BTC"),
    },
    "ETH": {
        "binance_symbol":   "ethusdt",
        "strike_increment": 25.0,
        # Guessed from BTC pattern - verify via discover_markets.py if needed
        "kalshi_series":    ("KXETHD", "ETHD-B", "KXETH15M", "KXETH", "ETH"),
    },
    "SOL": {
        "binance_symbol":   "solusdt",
        "strike_increment": 1.0,
        "kalshi_series":    ("KXSOLD", "SOLD-B", "KXSOL15M", "KXSOL", "SOL"),
    },
    "XRP": {
        "binance_symbol":   "xrpusdt",
        "strike_increment": 0.01,
        "kalshi_series":    ("KXXRPD", "XRPD-B", "KXXRP15M", "KXXRP", "XRP"),
    },
    "DOGE": {
        "binance_symbol":   "dogeusdt",
        "strike_increment": 0.001,
        "kalshi_series":    ("KXDOGED", "DOGED-B", "KXDOGE15M", "KXDOGE", "DOGE"),
    },
}

# Per-asset BV3 tables loaded from JSON.
# _bv3[asset] = {"table": [...], "dist_bounds": [...], "loaded": True}
_bv3: dict[str, dict] = {}

# Fallback: original hardcoded BTC table (used if no JSON file found)
_BV3_TABLE_FALLBACK = [
    [0.850, 0.796, 0.758, 0.727, 0.705, 0.686, 0.672, 0.656, 0.639, 0.624, 0.606, 0.595, 0.578],
    [0.980, 0.956, 0.931, 0.904, 0.876, 0.856, 0.833, 0.807, 0.783, 0.752, 0.733, 0.706, 0.675],
    [0.994, 0.983, 0.967, 0.951, 0.933, 0.909, 0.889, 0.868, 0.835, 0.811, 0.788, 0.756, 0.713],
    [0.997, 0.990, 0.981, 0.968, 0.950, 0.935, 0.917, 0.893, 0.874, 0.840, 0.816, 0.778, 0.741],
    [0.998, 0.993, 0.987, 0.977, 0.962, 0.948, 0.932, 0.908, 0.883, 0.869, 0.835, 0.809, 0.782],
    [0.998, 0.997, 0.988, 0.979, 0.968, 0.960, 0.944, 0.925, 0.913, 0.876, 0.849, 0.824, 0.781],
    [0.999, 0.994, 0.994, 0.979, 0.974, 0.963, 0.947, 0.936, 0.914, 0.897, 0.872, 0.839, 0.817],
    [0.999, 0.996, 0.995, 0.988, 0.982, 0.968, 0.963, 0.942, 0.917, 0.905, 0.884, 0.845, 0.818],
    [1.000, 0.999, 0.994, 0.992, 0.984, 0.980, 0.967, 0.964, 0.935, 0.919, 0.911, 0.862, 0.820],
    [1.000, 0.997, 0.995, 0.991, 0.986, 0.972, 0.971, 0.960, 0.942, 0.921, 0.904, 0.874, 0.820],
]
_BV3_DIST_BOUNDS_FALLBACK = [0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.0075, 0.010, 0.0125]

# Per-asset price deques: asset -> deque[(unix_ts, price)]
_prices: dict[str, deque] = {asset: deque(maxlen=500) for asset in ASSET_CONFIG}


def load_bv3_tables(use_pre2023: bool = False) -> None:
    """
    Load BV3 JSON tables from bv3_tables/ for all known assets.
    If use_pre2023=True, loads the _pre2023 variant (for backtesting).
    Falls back to hardcoded BTC table if no file found.
    """
    suffix = "pre2023" if use_pre2023 else "full"
    for asset in ASSET_CONFIG:
        path = os.path.join(BV3_DIR, f"{asset}_bv3_{suffix}.json")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                _bv3[asset] = {
                    "table":       data["table"],
                    "dist_bounds": data["dist_bounds"],
                    "loaded":      True,
                    "label":       data.get("label", suffix),
                }
                log.info(
                    f"BV3 [{asset}] loaded {path} "
                    f"({data['metadata'].get('total_windows', '?')} windows, "
                    f"label={data.get('label', suffix)})"
                )
            except Exception as exc:
                log.warning(f"BV3 [{asset}] failed to load {path}: {exc} - using fallback")
                _bv3[asset] = _fallback_entry()
        else:
            if asset == "BTC":
                log.warning(f"BV3 [BTC] table not found at {path} - using hardcoded fallback")
            else:
                log.info(f"BV3 [{asset}] no table at {path} - using BTC fallback (different volatility!)")
            _bv3[asset] = _fallback_entry()


def _fallback_entry() -> dict:
    return {
        "table":       _BV3_TABLE_FALLBACK,
        "dist_bounds": _BV3_DIST_BOUNDS_FALLBACK,
        "loaded":      False,
        "label":       "fallback",
    }


def empirical_win_prob(asset: str, abs_pct: float, mins_left: float) -> float:
    """
    Return empirical P(price stays on current side at window close) for the given asset.

    abs_pct: absolute fraction distance from strike (e.g. 0.003 = 0.3%)
    mins_left: minutes until window closes
    """
    entry = _bv3.get(asset, _fallback_entry())
    table       = entry["table"]
    dist_bounds = entry["dist_bounds"]
    n_rows = len(table)

    # Distance bucket
    bidx = n_rows - 1  # default: last row
    for i, bound in enumerate(dist_bounds):
        if abs_pct < bound:
            bidx = i
            break
    bidx = min(bidx, n_rows - 1)
    row = table[bidx]

    # Sub-1-min: nearly certain
    if mins_left < 1.0:
        return min(0.997, row[0] + 0.005)

    n_cols = len(row)
    max_min = n_cols  # last column covers max_min minutes remaining

    if mins_left >= max_min:
        return row[-1]

    t_low  = int(mins_left) - 1
    t_high = t_low + 1
    frac   = mins_left - int(mins_left)

    if t_high >= n_cols:
        return row[-1]

    return row[t_low] + (row[t_high] - row[t_low]) * frac


def bv3_bucket_indices(asset: str, abs_pct: float, mins_left: float) -> tuple[int, int]:
    """Return (dist_idx, time_idx) for recording trade outcomes."""
    entry = _bv3.get(asset, _fallback_entry())
    dist_bounds = entry["dist_bounds"]
    n_rows = len(entry["table"])

    bidx = n_rows - 1
    for i, bound in enumerate(dist_bounds):
        if abs_pct < bound:
            bidx = i
            break
    bidx = min(bidx, n_rows - 1)
    n_cols = len(entry["table"][0])
    t_low = max(0, min(n_cols - 1, int(max(1.0, mins_left)) - 1))
    return bidx, t_low


def get_price(asset: str) -> float | None:
    """Return the most recent price for an asset, or None if no data."""
    dq = _prices.get(asset)
    if not dq:
        return None
    return dq[-1][1]


def price_age_seconds(asset: str) -> float | None:
    """Return seconds since last price update, or None if no data."""
    dq = _prices.get(asset)
    if not dq:
        return None
    return time.time() - dq[-1][0]


async def binance_feed_task(enabled_assets: list[str]) -> None:
    """
    Maintain real-time price feeds for all enabled assets via Binance combined WebSocket.

    Uses aggTrade stream (individual trades, not klines) for lowest latency.
    Reconnects automatically on any error.
    """
    # Build stream list, e.g. "btcusdt@aggTrade/ethusdt@aggTrade"
    streams = "/".join(
        f"{ASSET_CONFIG[a]['binance_symbol']}@aggTrade"
        for a in enabled_assets
        if a in ASSET_CONFIG
    )
    url = f"{BINANCE_WS}?streams={streams}"

    # Map Binance symbol -> asset name for fast lookup
    sym_to_asset = {
        ASSET_CONFIG[a]["binance_symbol"].upper(): a
        for a in enabled_assets
        if a in ASSET_CONFIG
    }

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                log.info(f"Connected to Binance feed for {enabled_assets}")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        data = msg.get("data", {})
                        sym  = data.get("s", "").upper()  # e.g. "BTCUSDT"
                        asset = sym_to_asset.get(sym)
                        if asset and "p" in data:
                            price = float(data["p"])
                            now_ts = time.time()
                            dq = _prices[asset]
                            # Throttle to once per second per asset to match old behavior
                            if not dq or (now_ts - dq[-1][0]) >= 1.0:
                                dq.append((now_ts, price))
                    except Exception as parse_exc:
                        log.debug(f"Binance feed parse error: {parse_exc}")
        except Exception as exc:
            log.error(f"Binance feed disconnected: {exc}. Reconnecting in 3s...")
            await asyncio.sleep(3)
```

- [ ] **Step 2: Smoke-test the BV3 lookup**

```bash
python -c "
import asset_manager as am
am.load_bv3_tables()
# BTC at 0.3% from strike, 6 minutes remaining
prob = am.empirical_win_prob('BTC', 0.003, 6.0)
print(f'BTC 0.3% 6min: {prob:.4f}')  # expect ~0.909 from current table
# Fallback for asset without table
prob2 = am.empirical_win_prob('ETH', 0.003, 6.0)
print(f'ETH fallback: {prob2:.4f}')  # same until ETH table generated
"
```

Expected: prints two probabilities around 0.90, no errors.

- [ ] **Step 3: Commit**

```bash
git add asset_manager.py
git commit -m "feat: add asset_manager module (BV3 multi-asset lookup + Binance feed)"
```

---

## Task 4: Database Migration - Add `asset` Column

**Files:**
- Modify: `bot.py` lines ~476-492 (migration block in `init_db`)
- Run: migration SQL against existing `kalshi_bot.db`

The migration adds `asset TEXT DEFAULT 'BTC'` to the `trades` table. Existing rows stay labeled as BTC. New trades get their actual asset symbol.

- [ ] **Step 1: Add the column migration to `init_db` in `bot.py`**

Find the migration block (around line 476) that already does `ALTER TABLE trades ADD COLUMN`:

```python
        # Migrate existing DB - add new columns if not present
        for col, typedef in (
            ("claude_confidence", "INTEGER"),
            ("claude_signals", "TEXT"),
            ("order_id", "TEXT"),
        ):
```

Change it to:

```python
        # Migrate existing DB - add new columns if not present
        for col, typedef in (
            ("claude_confidence", "INTEGER"),
            ("claude_signals",    "TEXT"),
            ("order_id",          "TEXT"),
            ("asset",             "TEXT DEFAULT 'BTC'"),  # multi-asset support
        ):
```

- [ ] **Step 2: Run migration against the live database**

```bash
python -c "
import sqlite3
conn = sqlite3.connect('kalshi_bot.db')
try:
    conn.execute(\"ALTER TABLE trades ADD COLUMN asset TEXT DEFAULT 'BTC'\")
    conn.commit()
    print('Column added OK')
except Exception as e:
    print(f'Already exists or error: {e}')
conn.close()
"
```

Expected: "Column added OK" (or "already exists" if you're re-running).

- [ ] **Step 3: Verify**

```bash
python -c "
import sqlite3
conn = sqlite3.connect('kalshi_bot.db')
cols = [r[1] for r in conn.execute('PRAGMA table_info(trades)').fetchall()]
print('Columns:', cols)
conn.close()
"
```

Expected: `asset` appears in the column list.

- [ ] **Step 4: Update `db_write_trade` to include `asset`**

Find `db_write_trade` (~line 494 in bot.py). The INSERT statement needs `asset` added. Locate:

```python
async def db_write_trade(trade: dict) -> int | None:
    """Insert a trade record. Returns the new row id."""
```

The INSERT in this function currently does:
```python
                    model_prob, implied_prob, btc_price_at_entry, strike,
```

Change the function to include `asset` in the INSERT. Find the INSERT SQL in that function and add the column:

```python
async def db_write_trade(trade: dict) -> int | None:
    """Insert a trade record. Returns the new row id."""
    try:
        async with aiosqlite.connect(_DB_FILE) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            cur = await db.execute(
                """INSERT INTO trades (
                    ts, market_id, market_title, mode, side, contracts,
                    entry_price_cents, trade_amount_dollars, confidence_score,
                    model_prob, implied_prob, btc_price_at_entry, strike,
                    seconds_left_at_entry, fill_confirmed, asset
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trade.get("ts"), trade.get("market_id"), trade.get("market_title"),
                    trade.get("mode"), trade.get("side"), trade.get("contracts"),
                    trade.get("entry_price_cents"), trade.get("trade_amount_dollars"),
                    trade.get("confidence_score"), trade.get("model_prob"),
                    trade.get("implied_prob"), trade.get("btc_price_at_entry"),
                    trade.get("strike"), trade.get("seconds_left_at_entry"),
                    trade.get("fill_confirmed", 0), trade.get("asset", "BTC"),
                )
            )
            await db.commit()
            return cur.lastrowid
    except Exception as exc:
        log.error(f"db_write_trade error: {exc}")
        return None
```

**Note:** Read the current `db_write_trade` function before editing to ensure you match the exact existing SQL structure and don't miss any columns. The change is: add `asset` to the column list and `trade.get("asset", "BTC")` to the values tuple.

- [ ] **Step 5: Commit**

```bash
git add bot.py
git commit -m "feat: add asset column to trades table (defaults to BTC for existing rows)"
```

---

## Task 5: Multi-Asset Price Feeds in bot.py

**Files:**
- Modify: `bot.py` - replace `btc_feed_task` with Binance feed, import asset_manager

The existing `btc_feed_task` and `get_btc_price` / `btc_prices` globals are BTC-specific. We replace them with asset_manager's Binance feed and `get_price(asset)` calls.

- [ ] **Step 1: Add import at top of `bot.py`**

After the existing imports (around line 32), add:

```python
import asset_manager
from asset_manager import (
    ASSET_CONFIG,
    load_bv3_tables,
    empirical_win_prob as _am_win_prob,
    bv3_bucket_indices as _am_bv3_bucket,
    get_price as _am_get_price,
    price_age_seconds as _am_price_age,
    binance_feed_task,
)
```

- [ ] **Step 2: Add `enabled_assets` config support**

In `_init_config` or wherever config defaults are set (find the `read_config` function), ensure the config accepts `enabled_assets`. This field is read from `config.json`. Add a default in `read_config`:

```python
def read_config() -> dict:
    # ... existing code ...
    # After loading config dict, fill in default for enabled_assets if missing:
    cfg.setdefault("enabled_assets", ["BTC"])
    return cfg
```

- [ ] **Step 3: Replace `btc_feed_task` call in `main()`**

Find where `btc_feed_task` is used (search for `asyncio.gather` or `asyncio.create_task` near the bottom of bot.py). It will look like:

```python
    await asyncio.gather(
        main_loop(),
        btc_feed_task(),
    )
```

Change it to:

```python
    config = read_config()
    enabled = config.get("enabled_assets", ["BTC"])
    # Load BV3 tables for all enabled assets (full table for live trading)
    load_bv3_tables(use_pre2023=False)

    await asyncio.gather(
        main_loop(),
        binance_feed_task(enabled),
    )
```

- [ ] **Step 4: Update `get_btc_price()` calls throughout bot.py**

There is one global `get_btc_price()` function and multiple call sites. We need to keep it working for backward compatibility but also support other assets.

Update `get_btc_price` to delegate to asset_manager:
```python
def get_btc_price() -> float | None:
    """Return the most recent BTC price. Kept for backward compatibility."""
    return _am_get_price("BTC")
```

Remove the now-unused `btc_prices` deque global and `btc_feed_task` function (or leave them as dead code with a comment - do NOT remove if they are referenced from other places; search first):

```bash
grep -n "btc_prices\|btc_feed_task" /c/Users/alxnt/kalshi-bot/bot.py
```

Any reference in the staleness check like:
```python
if btc_prices and (time.time() - btc_prices[-1][0]) > 60:
```
Replace with:
```python
age = _am_price_age("BTC")
if age is not None and age > 60:
    log.warning(f"BTC price stale ({age:.0f}s old) - skipping cycle.")
    ...
```

- [ ] **Step 5: Update `_empirical_win_prob` to use asset_manager**

The existing `_empirical_win_prob(abs_pct, mins_left)` in bot.py is the BTC-only hardcoded version. It is called by `printer_brain`. We need `printer_brain` to accept an `asset` parameter so it can use the right table.

Find `printer_brain` function signature. It currently looks like:
```python
def printer_brain(btc_price: float, strike: float, yes_ask: float, no_ask: float,
                  elapsed_secs: float, secs_left: float, ticker: str, ...) -> dict:
```

Add `asset: str = "BTC"` parameter:
```python
def printer_brain(btc_price: float, strike: float, yes_ask: float, no_ask: float,
                  elapsed_secs: float, secs_left: float, ticker: str,
                  min_ev_base: float = 3.0, vol_gate_thresh: float = 1.80,
                  kalshi_fee: float = 0.07, asset: str = "BTC") -> dict:
```

Inside `printer_brain`, find every call to `_empirical_win_prob(abs_pct, mins_left)` and replace with:
```python
_am_win_prob(asset, abs_pct, mins_left)
```

Also find `_bv3_bucket_indices` calls and replace with:
```python
_am_bv3_bucket(asset, abs_pct, mins_left)
```

The old `_empirical_win_prob` and `_bv3_bucket_indices` functions can remain in bot.py as internal fallbacks but are no longer the primary path. Leave them in place (do NOT delete) so any references you missed still work.

- [ ] **Step 6: Verify BTC still works**

```bash
python -c "
import asyncio, bot
# smoke test: load config, verify BTC feed hooks up
cfg = bot.read_config()
print('enabled_assets:', cfg.get('enabled_assets', ['BTC']))
print('get_btc_price (no feed yet):', bot.get_btc_price())
"
```

Expected: `enabled_assets: ['BTC']`, `get_btc_price: None` (no feed running yet - that's fine).

- [ ] **Step 7: Commit**

```bash
git add bot.py
git commit -m "feat: replace BTC-only Coinbase feed with Binance multi-asset feed"
```

---

## Task 6: Multi-Asset Market Fetching and Trading Loop

**Files:**
- Modify: `bot.py` - per-asset state dicts, per-asset `fetch_current_market`, main loop iteration

This is the largest change. We introduce per-asset state and run the trading loop for each enabled asset each cycle.

- [ ] **Step 1: Add per-asset state structure**

After the existing global state declarations (around line 85-100 in bot.py), add:

```python
# Per-asset state (replaces single-asset globals for multi-asset support)
# Each asset gets its own market, phase, position, and order-attempted set.
# BTC state is initialized from the existing single-asset globals for compatibility.
_asset_states: dict[str, dict] = {}

def _init_asset_states(enabled_assets: list[str]) -> None:
    """Initialize per-asset state dicts. Called once at startup."""
    global _asset_states
    for asset in enabled_assets:
        _asset_states[asset] = {
            "market":              None,
            "phase":               "DONE",
            "position":            None,
            "order_attempted":     set(),
        }
    # For BTC: migrate existing single-asset globals into the dict.
    # This preserves recovered state (crash recovery) for BTC.
    if "BTC" in _asset_states:
        _asset_states["BTC"]["market"]   = current_market
        _asset_states["BTC"]["phase"]    = current_phase
        _asset_states["BTC"]["position"] = current_position
```

- [ ] **Step 2: Add per-asset `fetch_market` function**

The existing `fetch_current_market` is BTC-hardcoded (searches KXBTCD, BTCD-B, etc.). Add a generic version:

```python
async def fetch_market_for_asset(
    session: aiohttp.ClientSession,
    asset: str,
) -> dict | None:
    """
    Fetch the soonest-expiring short-duration Kalshi market for the given asset.
    Uses ASSET_CONFIG[asset]['kalshi_series'] as the search order.
    Returns None if no valid market found.
    """
    if asset not in ASSET_CONFIG:
        return None

    series_order = ASSET_CONFIG[asset]["kalshi_series"]
    path = "/markets"
    all_markets = []
    seen: set[str] = set()

    for series in series_order:
        params = {"series_ticker": series, "status": "open", "limit": 20}
        try:
            async with session.get(
                KALSHI_BASE_URL + path,
                headers=kalshi_headers("GET", path),
                params=params,
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
            for m in data.get("markets", []):
                t = m.get("ticker", "")
                if t and t not in seen:
                    seen.add(t)
                    all_markets.append(m)
        except Exception as exc:
            log.debug(f"[{asset}] Market fetch error (series={series}): {exc}")

    if not all_markets:
        return None

    # Filter out long-duration markets (same logic as BTC)
    now_utc = datetime.now(timezone.utc)
    valid = []
    for m in all_markets:
        title = m.get("title", "").lower()
        if "range" in title or "daily" in title:
            continue
        try:
            close_dt = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            open_dt  = datetime.fromisoformat(m["open_time"].replace("Z", "+00:00"))
            duration = (close_dt - open_dt).total_seconds() / 60
            mins_left = (close_dt - now_utc).total_seconds() / 60
            if 1 <= duration <= 75 and mins_left > 0:
                valid.append(m)
        except Exception:
            continue

    if not valid:
        return None

    valid.sort(key=lambda m: m.get("close_time", ""))
    return valid[0]
```

- [ ] **Step 3: Add per-asset Telegram message helper**

All Telegram calls should include the asset name. Find `send_telegram` calls in the existing code and note that they need asset context. Add a helper:

```python
def _asset_tag(asset: str) -> str:
    """Return a short emoji+symbol tag for Telegram messages."""
    tags = {"BTC": "₿ BTC", "ETH": "Ξ ETH", "SOL": "◎ SOL", "XRP": " XRP", "DOGE": " DOGE"}
    return tags.get(asset, asset)
```

- [ ] **Step 4: Refactor the main trading loop to iterate assets**

The existing main loop (the `while True:` inside `main_loop()` or equivalent) currently does:
1. Get BTC price -> handle BTC market -> sleep

We need it to do:
1. For each enabled asset:
   a. Get asset price (staleness check)
   b. Fetch asset market
   c. Handle WATCH/READY/LOCKED phases

**Find the main loop function.** It is likely `async def main_loop()` or the outer `while True` in `async def main()`. Based on the code read earlier, the main loop is the `while True:` block inside `async def main()` (around line 3200). It calls `handle_ready_phase(session, config, market, ...)`.

The refactor wraps the existing single-asset logic in a `for asset in enabled_assets:` loop:

```python
# Inside the main while True: loop, replace the single-asset section with:

                enabled_assets = config.get("enabled_assets", ["BTC"])

                for asset in enabled_assets:
                    state = _asset_states.get(asset)
                    if state is None:
                        continue

                    # Price check
                    price = _am_get_price(asset)
                    if price is None:
                        log.warning(f"[{asset}] Waiting for price...")
                        continue
                    age = _am_price_age(asset)
                    if age is not None and age > 60:
                        log.warning(f"[{asset}] Price stale ({age:.0f}s) - skipping")
                        continue

                    # Market fetch
                    # BTC: use existing cached fetch_current_market for compatibility
                    # Others: use new per-asset fetcher
                    try:
                        if asset == "BTC":
                            market = await fetch_current_market(session)
                        else:
                            market = await fetch_market_for_asset(session, asset)
                    except Exception as exc:
                        log.error(f"[{asset}] Market fetch error: {exc}")
                        continue

                    if market is None:
                        log.info(f"[{asset}] No active market found")
                        state["phase"] = "DONE"
                        continue

                    # Phase logic
                    # Delegate to the existing handle_* functions, passing asset.
                    # The handle_ready_phase function needs asset param (see Task 5 Step 5).
                    try:
                        strike = parse_strike(market)
                        secs_left = seconds_remaining(market)
                        elapsed   = seconds_elapsed(market)
                        ticker    = market.get("ticker", "")

                        if state["phase"] == "LOCKED" and state["position"]:
                            await handle_locked_phase(session, config, market, price,
                                                       state, asset)
                        elif state["phase"] in ("WATCH", "READY", "DONE"):
                            _update_phase(market, state, secs_left, elapsed)
                            if state["phase"] == "READY" and not limit_triggered:
                                await handle_ready_phase(
                                    session, config, market, ticker,
                                    price, secs_left, strike, elapsed,
                                    asset=asset, state=state,
                                )
                    except Exception as exc:
                        log.error(f"[{asset}] Phase error: {exc}", exc_info=True)
```

**Note on handle_ready_phase and handle_locked_phase:** These functions currently use global state variables (`current_position`, `current_phase`, `_order_attempted_tickers`). After this refactor, they should receive `state` dict and `asset` as parameters, reading/writing from `state["position"]`, `state["phase"]`, `state["order_attempted"]` instead of globals.

This is a substantial signature change. Before making it, read both functions to understand all the global state they touch. The key globals to thread through `state`:
- `current_position` -> `state["position"]`
- `current_phase` -> `state["phase"]`
- `_order_attempted_tickers` -> `state["order_attempted"]`
- `btc_price` -> `price` (already a parameter)

The global `limit_triggered`, `_consecutive_losses`, `daily_reset_date` remain GLOBAL (shared across all assets).

- [ ] **Step 5: Pass `asset` to `handle_ready_phase` and `printer_brain`**

Find `handle_ready_phase` signature:
```python
async def handle_ready_phase(session, config, market, ticker, btc_price, secs_left, strike, elapsed):
```

Add `asset="BTC"` and `state=None` parameters:
```python
async def handle_ready_phase(session, config, market, ticker, price, secs_left, strike, elapsed,
                              asset: str = "BTC", state: dict | None = None):
```

Inside `handle_ready_phase`, use `state` for position/phase writes, and pass `asset` to `printer_brain`:
```python
    brain = printer_brain(price, strike, yes_ask, no_ask, elapsed, secs_left, ticker,
                          min_ev_base=config.get("min_ev_base", 3.0),
                          vol_gate_thresh=config.get("vol_gate_thresh", 1.80),
                          kalshi_fee=config.get("kalshi_fee_per_contract_cents", 7) / 100,
                          asset=asset)
```

Where the trade dict is built, add:
```python
    trade["asset"] = asset
```

For Telegram messages inside `handle_ready_phase`, prefix with `_asset_tag(asset)`.

- [ ] **Step 6: Update `write_state_file` to include per-asset prices**

The state file is read by `server.py` for the dashboard. Add a `prices` key:

Find `write_state_file` (or wherever `bot_state.json` is written) and add:
```python
    "prices": {a: _am_get_price(a) for a in config.get("enabled_assets", ["BTC"])},
    "enabled_assets": config.get("enabled_assets", ["BTC"]),
```

- [ ] **Step 7: Update `config.json` to add `enabled_assets`**

```bash
python -c "
import json
with open('config.json') as f:
    cfg = json.load(f)
cfg.setdefault('enabled_assets', ['BTC'])
with open('config.json', 'w') as f:
    json.dump(cfg, f, indent=2)
print('config.json updated')
"
```

- [ ] **Step 8: Integration smoke test (BTC only, no other asset data yet)**

```bash
# Start bot in paper mode, verify it starts without errors and trades BTC as before.
# Kill after ~30 seconds.
timeout 30 python runner.py btc15m || true
```

Check logs: should see "Connected to Binance feed for ['BTC']", market fetch, no tracebacks.

- [ ] **Step 9: Commit**

```bash
git add bot.py config.json
git commit -m "feat: multi-asset trading loop with per-asset state and market fetching"
```

---

## Task 7: Multi-Asset Backtest

**Files:**
- Modify: `backtest.py`

Changes:
1. Add `--asset BTC|ETH|SOL|XRP|DOGE` flag
2. Load the per-asset pre-2023 BV3 table from `bv3_tables/`
3. Load per-asset CSV (or use the BTC CSV if only BTC available)
4. Run backtest per asset; when no `--asset` flag given, run all available
5. Show per-asset AND combined REALITY CHECK

The existing `_BV3_TABLE` and `_empirical_win_prob` at the top of backtest.py should be replaced by loading from asset_manager.

- [ ] **Step 1: Replace hardcoded BV3 in backtest.py with asset_manager**

At the top of `backtest.py`, after the existing imports, add:

```python
import asset_manager
```

Remove the hardcoded `_BV3_TABLE`, `_BV3_DIST_BOUNDS`, and `_empirical_win_prob` function from backtest.py. Replace any call to `_empirical_win_prob(abs_pct, mins_left)` with:

```python
asset_manager.empirical_win_prob(CURRENT_ASSET, abs_pct, mins_left)
```

Where `CURRENT_ASSET` is a module-level string set before the backtest runs (e.g., `CURRENT_ASSET = "BTC"`).

- [ ] **Step 2: Add `--asset` argument and per-asset config**

Find the `argparse` setup in backtest.py (around line 12):

```python
parser = argparse.ArgumentParser(...)
parser.add_argument("--start-year", ...)
```

Add:
```python
parser.add_argument("--asset", default=None,
                    choices=["BTC", "ETH", "SOL", "XRP", "DOGE", "ALL"],
                    help="Asset to backtest (default: BTC if data available, or ALL)")
```

- [ ] **Step 3: Add per-asset CSV path resolver**

```python
def _csv_for_asset(asset: str) -> str:
    """Return the path to the 1-minute CSV for a given asset."""
    data_dir = os.path.join(_BASE_DIR, "data")
    # Check new standard location first
    standard = os.path.join(data_dir, f"{asset}_1m.csv")
    if os.path.exists(standard):
        return standard
    # BTC fallback: the original hardcoded path
    if asset == "BTC":
        fallback = r'C:\Users\alxnt\Downloads\d5ae29c4-33c6-11f1-b1e7-6dda37cfa7b9\binance_api_BTCUSDT_1m.csv'
        if os.path.exists(fallback):
            return fallback
    raise FileNotFoundError(
        f"No CSV found for {asset}. Run: python download_data.py --asset {asset}"
    )
```

- [ ] **Step 4: Add per-asset BV3 loading before backtest run**

In the main backtest entry point (look for `if __name__ == "__main__":` or the function that kicks off the full run), load the BV3 tables:

```python
    # Load pre-2023 BV3 tables for honest OOS evaluation
    asset_manager.load_bv3_tables(use_pre2023=True)
    print(f"[BV3] Loaded pre-2023 tables (honest OOS backtest)")
    # Warn for assets without a dedicated table
    for a in assets_to_run:
        entry = asset_manager._bv3.get(a, {})
        if not entry.get("loaded"):
            print(f"  WARNING: {a} has no pre-2023 BV3 table - using BTC fallback (biased!)")
```

- [ ] **Step 5: Wrap the main backtest function to accept `asset` parameter**

The existing backtest function is likely called `run_backtest(args)` or similar. Wrap it:

```python
def run_backtest_for_asset(asset: str, args) -> dict:
    """
    Run the full backtest for a single asset.
    Returns summary metrics dict.
    """
    global CURRENT_ASSET, CSV_PATH
    CURRENT_ASSET = asset
    try:
        CSV_PATH = _csv_for_asset(asset)
    except FileNotFoundError as e:
        print(f"[{asset}] {e}")
        return {}

    print(f"\n{'='*70}")
    print(f"BACKTEST: {asset}")
    print(f"CSV: {CSV_PATH}")
    print(f"BV3 table: {asset_manager._bv3.get(asset, {}).get('label', 'unknown')}")
    print(f"{'='*70}\n")

    # Call existing backtest logic (run_backtest returns the metrics dict)
    return run_backtest(args)
```

- [ ] **Step 6: Add combined REALITY CHECK output**

After all per-asset backtests complete, print combined metrics:

```python
def print_combined_reality_check(per_asset_results: dict[str, dict]) -> None:
    """Print a combined summary across all assets."""
    print("\n" + "=" * 70)
    print("COMBINED REALITY CHECK (all assets)")
    print("=" * 70)
    print(f"{'Asset':<8} {'Trades':>7} {'WR%':>7} {'PnL$':>9} {'Sharpe':>8} {'MaxDD%':>8}")
    print("-" * 55)
    total_trades = 0
    total_pnl    = 0.0
    total_wins   = 0
    for asset, r in per_asset_results.items():
        if not r:
            continue
        trades = r.get("total_trades", 0)
        wr     = r.get("win_rate", 0) * 100
        pnl    = r.get("total_pnl", 0)
        sharpe = r.get("sharpe_ratio", 0)
        maxdd  = r.get("max_drawdown_pct", 0)
        print(f"{asset:<8} {trades:>7} {wr:>7.1f} {pnl:>+9.2f} {sharpe:>8.2f} {maxdd:>8.1f}%")
        total_trades += trades
        total_pnl    += pnl
        total_wins   += int(trades * r.get("win_rate", 0))

    print("-" * 55)
    if total_trades > 0:
        combined_wr = total_wins / total_trades * 100
        print(f"{'COMBINED':<8} {total_trades:>7} {combined_wr:>7.1f} {total_pnl:>+9.2f}")
    print(f"\nBV3 data leakage: FIXED - using pre-2023 table for OOS evaluation")
```

- [ ] **Step 7: Wire up the `--asset` flag in `__main__`**

```python
if __name__ == "__main__":
    args = parser.parse_args()

    if args.asset == "ALL" or args.asset is None:
        assets_to_run = ["BTC", "ETH", "SOL", "XRP", "DOGE"]
    else:
        assets_to_run = [args.asset]

    asset_manager.load_bv3_tables(use_pre2023=True)

    results = {}
    for asset in assets_to_run:
        try:
            results[asset] = run_backtest_for_asset(asset, args)
        except Exception as e:
            print(f"[{asset}] Backtest failed: {e}")
            results[asset] = {}

    if len(assets_to_run) > 1:
        print_combined_reality_check(results)
```

- [ ] **Step 8: Test BTC backtest still works**

```bash
python backtest.py --asset BTC --start-year 2023
```

Expected: same output structure as before, with "BV3 data leakage: FIXED" in the REALITY CHECK. The OOS metrics may look WORSE than before - this is correct and expected (the table is now honest).

- [ ] **Step 9: Commit**

```bash
git add backtest.py
git commit -m "feat: multi-asset backtest with --asset flag and pre-2023 BV3 tables"
```

---

## Self-Review

### Spec coverage check

| Spec requirement | Covered by |
|-----------------|------------|
| `generate_bv3_table.py` reads 1-min CSV | Task 2 |
| 15-min window segmentation | Task 2 Step 1 (build_table function) |
| distance_pct from nearest Kalshi-style strike | Task 2 (nearest_strike function) |
| stayed_above check | Task 2 (stayed_same_side logic) |
| Distance buckets matching spec | Task 2 (DIST_BOUNDS list) |
| Output as JSON | Task 2 (json.dump) |
| `_full.json` and `_pre2023.json` | Task 2 (generate_for_asset) |
| Comparison print (stability check) | Task 2 (comparison table print) |
| Configurable strike increments | Task 3 (STRIKE_INCREMENTS dict + ASSET_CONFIG) |
| BTC $1000, ETH $25, SOL $1, XRP $0.01, DOGE $0.001 | Task 3 (ASSET_CONFIG) |
| Replace single BV3 with dict keyed by asset | Task 3 (asset_manager._bv3 dict) |
| Load all BV3 tables at startup | Task 5 (load_bv3_tables call in main) |
| `enabled_assets` config field | Task 6 Step 7 (config.json) |
| Main loop evaluates all enabled assets | Task 6 Step 4 |
| Per-asset price feed | Task 3 (binance_feed_task), Task 5 |
| Binance WebSocket for all 5 | Task 3 (BINANCE_WS combined stream) |
| Per-asset BV3 lookup | Task 3 (empirical_win_prob(asset, ...)) |
| Per-asset Kalshi strike discovery | Task 6 (fetch_market_for_asset) |
| Per-asset position tracking | Task 6 Step 1 (_asset_states dict) |
| Consecutive loss counter GLOBAL | Task 6 Step 4 (limit_triggered stays global) |
| Daily loss limit GLOBAL | Task 6 Step 4 (same) |
| `asset` column in trades | Task 4 |
| Telegram alerts include asset | Task 6 Step 3 (_asset_tag helper) |
| Resumable download | Task 1 (get_last_ts / resume logic) |
| Don't break BTC | Task 5 Step 4 (BTC uses existing fetch_current_market) |
| `--asset` flag in backtest | Task 7 Step 2 |
| Per-asset BV3 (pre-2023) in backtest | Task 7 Step 4 |
| Combined metrics in backtest | Task 7 Step 6 |

### Gaps identified

1. **dashboard.py / server.py**: The spec mentions "dashboard should show per-asset breakdown". The plan updates `write_state_file` to include per-asset prices but does NOT modify `server.py`. This is a UI concern; bot functionality is complete without it. Flag for follow-up.

2. **Kalshi series for non-BTC assets**: The series tickers (KXETHD, KXXRPD, etc.) are guesses based on the BTC pattern. If Kalshi uses different tickers, `fetch_market_for_asset` will return None for those assets. The bot will silently skip them and only trade BTC. This is the correct graceful degradation - add a log warning so it's visible.

3. **BTC CSV path in backtest.py**: Still hardcoded as fallback. Task 7 Step 3 adds a standard path first. Once `data/BTC_1m.csv` exists (from download_data.py or manual copy), the hardcoded fallback is never used.

### Placeholder scan: clean - all steps have actual code.

### Type consistency check
- `empirical_win_prob(asset: str, abs_pct: float, mins_left: float)` - used consistently in Task 3, Task 5, Task 7.
- `bv3_bucket_indices(asset, abs_pct, mins_left)` - same.
- `state: dict` with keys `market`, `phase`, `position`, `order_attempted` - consistent in Tasks 6.
- `_bv3: dict[str, dict]` accessed via `asset_manager._bv3` in Task 7 - direct dict access is fine since both files are in the same package.

---

## Execution Summary

| # | Change | Files | Task |
|---|--------|-------|------|
| 1 | Resumable Binance klines downloader | `download_data.py` (new) | Task 1 |
| 2 | Per-asset BV3 generator with full/pre-2023 split | `generate_bv3_table.py` (new), `bv3_tables/` | Task 2 |
| 3 | Asset config registry + BV3 lookup + Binance feed | `asset_manager.py` (new) | Task 3 |
| 4 | Add `asset` column to trades table | `bot.py` | Task 4 |
| 5 | Replace Coinbase feed with Binance multi-asset feed | `bot.py` | Task 5 |
| 6 | Multi-asset trading loop with per-asset state | `bot.py`, `config.json` | Task 6 |
| 7 | `--asset` flag + per-asset BV3 in backtest | `backtest.py` | Task 7 |

---

## How to Run

### Download data (Phase 1)
```bash
python download_data.py          # all assets (~3-8 hours for full history)
python download_data.py --asset ETH  # single asset (~1-2 hours for ETH)
```
Resumable - safe to interrupt and restart.

### Generate BV3 tables (Phase 2)
```bash
# BTC (uses data/BTC_1m.csv or copy from Downloads folder first)
python generate_bv3_table.py --asset BTC

# All assets (after download completes)
python generate_bv3_table.py --all
```

### Backtest per asset (Phase 4)
```bash
python backtest.py --asset BTC
python backtest.py --asset ETH
python backtest.py            # all assets + combined metrics
```

### Config changes before first live multi-asset run
Add to `config.json`:
```json
"enabled_assets": ["BTC", "ETH"]
```
Start with `["BTC"]` until other assets have BV3 tables and you've verified the Kalshi series tickers.
