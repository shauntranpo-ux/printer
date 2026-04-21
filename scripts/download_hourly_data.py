"""
Download recent BTC and ETH 1-minute OHLCV from Binance US public API.
Appends new data to data/historical/{ETH,BTC}_1m_extended.parquet.
Skips already-downloaded ranges.

Usage:
    python scripts/download_hourly_data.py
"""

from __future__ import annotations
import json
import sys
import time
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd

DATA_DIR = Path("data/historical")
SYMBOLS = {
    "BTC": "BTCUSD",
    "ETH": "ETHUSD",
}
BINANCE_US_URL = "https://api.binance.us/api/v3/klines"
LIMIT = 1000  # max rows per request
INTERVAL = "1m"
TARGET_END_TS = datetime(2026, 4, 20, tzinfo=timezone.utc).timestamp()


def fetch_klines(symbol: str, start_ms: int, limit: int = LIMIT) -> list:
    """Fetch up to `limit` 1-min klines starting at start_ms (epoch ms)."""
    url = (
        f"{BINANCE_US_URL}?symbol={symbol}&interval={INTERVAL}"
        f"&limit={limit}&startTime={start_ms}"
    )
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kalshi-bot/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except Exception as e:
            print(f"  Attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to fetch {symbol} starting {start_ms}")


def klines_to_df(klines: list) -> pd.DataFrame:
    """Convert Binance klines list to DataFrame with timestamp + OHLCV."""
    rows = []
    for k in klines:
        open_time_ms = int(k[0])
        rows.append({
            "timestamp": open_time_ms / 1000.0,
            "open":   float(k[1]),
            "high":   float(k[2]),
            "low":    float(k[3]),
            "close":  float(k[4]),
            "volume": float(k[5]),
        })
    return pd.DataFrame(rows)


def download_asset(asset: str, symbol: str) -> None:
    path = DATA_DIR / f"{asset}_1m_extended.parquet"
    if not path.exists():
        print(f"  {path} not found -- skipping {asset}")
        return

    existing = pd.read_parquet(path)
    max_ts = existing["timestamp"].max()
    print(f"  {asset}: existing data ends at {datetime.fromtimestamp(max_ts, tz=timezone.utc)} ({max_ts:.0f})")

    fetch_start_ms = int((max_ts + 60) * 1000)
    fetch_end_ms = int(TARGET_END_TS * 1000)

    if fetch_start_ms >= fetch_end_ms:
        print(f"  {asset}: already up to date, nothing to download")
        return

    new_rows = []
    cursor_ms = fetch_start_ms
    request_count = 0

    while cursor_ms < fetch_end_ms:
        klines = fetch_klines(symbol, cursor_ms)
        if not klines:
            break

        chunk_df = klines_to_df(klines)
        chunk_df = chunk_df[chunk_df["timestamp"] <= TARGET_END_TS]
        if chunk_df.empty:
            break

        new_rows.append(chunk_df)
        last_ts = chunk_df["timestamp"].iloc[-1]
        cursor_ms = int((last_ts + 60) * 1000)
        request_count += 1

        if request_count % 10 == 0:
            fetched = sum(len(r) for r in new_rows)
            print(f"  {asset}: fetched {fetched:,} rows... last ts {datetime.fromtimestamp(last_ts, tz=timezone.utc)}")

        time.sleep(0.12)

        if len(klines) < LIMIT:
            break

    if not new_rows:
        print(f"  {asset}: no new rows available")
        return

    new_df = pd.concat(new_rows, ignore_index=True)
    print(f"  {asset}: downloaded {len(new_df):,} new rows from Binance US")

    combined = pd.concat([existing, new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=["timestamp"])
    combined = combined.sort_values("timestamp").reset_index(drop=True)

    combined.to_parquet(path, index=False)
    new_max = combined["timestamp"].max()
    print(f"  {asset}: saved {len(combined):,} total rows, data now ends at {datetime.fromtimestamp(new_max, tz=timezone.utc)}")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print("Downloading fresh 1-min OHLCV data from Binance US...")
    print(f"Target end: {datetime.fromtimestamp(TARGET_END_TS, tz=timezone.utc)}")
    print()

    for asset, symbol in SYMBOLS.items():
        print(f"--- {asset} ({symbol}) ---")
        download_asset(asset, symbol)
        print()

    print("Done.")


if __name__ == "__main__":
    main()
