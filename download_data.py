#!/usr/bin/env python3
"""
download_data.py — Resumable Binance 1-minute klines downloader.

Downloads historical 1-minute OHLCV data for all enabled assets.
If a CSV already exists, resumes from the last row instead of re-downloading.

Usage:
    python download_data.py                    # all assets
    python download_data.py --asset ETH        # single asset
    python download_data.py --asset BTC ETH    # subset
    python download_data.py --asset BTC --start 2020-01-01  # override start
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

# Binance trading pair symbols
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
    """Return the last open_time in the CSV as milliseconds, or None if empty/missing."""
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
            # open_time stored as ISO string "2023-01-01T00:01:00+00:00"
            dt = datetime.fromisoformat(last_row[0])
            return int(dt.timestamp() * 1000)
    except Exception as e:
        print(f"  Warning: could not read last row of {path}: {e}")
    return None


def fetch_klines(symbol: str, start_ms: int, limit: int = 1000) -> list:
    """Fetch up to `limit` 1-minute klines from Binance starting at start_ms."""
    params = {
        "symbol":    symbol,
        "interval":  "1m",
        "limit":     limit,
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
    Returns True on success, False on partial failure.
    """
    symbol = ASSET_SYMBOLS[asset]
    path   = csv_path(asset)
    os.makedirs(DATA_DIR, exist_ok=True)

    # Determine start time
    last_ms = get_last_ts(path)
    if last_ms is not None and force_start is None:
        # Start one minute after last downloaded row
        start_ms   = last_ms + 60_000
        file_mode  = "a"
        write_header = False
        print(f"[{asset}] Resuming from {ms_to_iso(start_ms)} (last row: {ms_to_iso(last_ms)})")
    else:
        start_str    = force_start or ASSET_START[asset]
        start_ms     = int(datetime.fromisoformat(start_str + "T00:00:00+00:00").timestamp() * 1000)
        file_mode    = "w"
        write_header = True
        print(f"[{asset}] Starting fresh from {start_str}")

    # Leave last 2 minutes as incomplete candles
    now_ms = int(time.time() * 1000) - 120_000

    if start_ms >= now_ms:
        print(f"[{asset}] Already up to date.")
        return True

    total_rows = 0
    failed     = False

    with open(path, file_mode, newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(CSV_HEADER)

        current_ms = start_ms
        while current_ms < now_ms:
            try:
                klines = fetch_klines(symbol, current_ms)
            except Exception as e:
                print(f"\n[{asset}] Fetch error at {ms_to_iso(current_ms)}: {e}")
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
                    ms_to_iso(open_ms),  # open_time as ISO
                    k[1],                 # open
                    k[2],                 # high
                    k[3],                 # low
                    k[4],                 # close
                    k[5],                 # volume
                ])

            if not batch:
                break

            writer.writerows(batch)
            f.flush()
            total_rows += len(batch)

            last_open_ms = int(datetime.fromisoformat(batch[-1][0]).timestamp() * 1000)
            current_ms   = last_open_ms + 60_000

            pct = min(100.0, (current_ms - start_ms) / max(1, now_ms - start_ms) * 100)
            print(f"  [{asset}] {ms_to_iso(current_ms)[:10]} | {total_rows:,} rows | {pct:.1f}%", end="\r")

            # Binance allows ~1200 req/min; 0.1 s between calls is safe
            time.sleep(0.1)

    print(f"\n[{asset}] Done: {total_rows:,} new rows → {path}")
    return not failed


def main():
    parser = argparse.ArgumentParser(description="Download Binance 1-minute klines")
    parser.add_argument(
        "--asset", nargs="+", choices=list(ASSET_SYMBOLS),
        default=None, help="Assets to download (default: all)"
    )
    parser.add_argument(
        "--start", default=None,
        help="Override start date YYYY-MM-DD (single asset only)"
    )
    args = parser.parse_args()

    assets = args.asset or list(ASSET_SYMBOLS.keys())
    if args.start and len(assets) > 1:
        print("--start can only be used with a single --asset")
        sys.exit(1)

    results: dict[str, bool] = {}
    for asset in assets:
        print(f"\n{'='*60}")
        print(f"Downloading {asset} ({ASSET_SYMBOLS[asset]})")
        print(f"{'='*60}")
        try:
            results[asset] = download_asset(
                asset,
                force_start=args.start if len(assets) == 1 else None,
            )
        except Exception as e:
            print(f"[{asset}] FAILED: {e}")
            results[asset] = False

    print("\n\nSummary:")
    for asset, ok in results.items():
        status = "OK" if ok else "FAILED"
        p      = csv_path(asset)
        size   = os.path.getsize(p) / 1e6 if os.path.exists(p) else 0
        print(f"  {asset:5} {status:6}  {p}  ({size:.1f} MB)")


if __name__ == "__main__":
    main()
