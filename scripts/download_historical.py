#!/usr/bin/env python3
"""Download 1-minute OHLCV data from Binance for all 5 assets and save as parquet.

Usage:
    python scripts/download_historical.py
    python scripts/download_historical.py --start 2026-01-01 --end 2026-04-17
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd

BASE_URL = "https://api.binance.us/api/v3/klines"
BASE_URL_FALLBACK = "https://api.binance.com/api/v3/klines"
ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE"]
SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
    "DOGE": "DOGEUSDT",
}
LIMIT = 1000
SLEEP_MS = 100
OUT_DIR = Path(__file__).parent.parent / "data" / "historical"


def fetch_klines(symbol: str, start_ms: int, end_ms: int) -> list[list]:
    params = {
        "symbol": symbol,
        "interval": "1m",
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": LIMIT,
    }
    for url in (BASE_URL, BASE_URL_FALLBACK):
        try:
            r = httpx.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError:
            continue
    raise RuntimeError(f"Both Binance endpoints failed for {symbol}")


def download_asset(asset: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    symbol = SYMBOLS[asset]
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    rows: list[dict] = []
    cursor = start_ms
    print(f"  {asset}: fetching from {start_dt.date()} to {end_dt.date()} ...", flush=True)

    while cursor < end_ms:
        chunk = fetch_klines(symbol, cursor, end_ms)
        if not chunk:
            break
        for k in chunk:
            rows.append(
                {
                    "open_time": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                }
            )
        # Next page starts after the last candle's open_time
        cursor = chunk[-1][0] + 60_000
        time.sleep(SLEEP_MS / 1000)

    df = pd.DataFrame(rows).drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    return df


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default=None, help="Defaults to yesterday")
    args = parser.parse_args()

    start_dt = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    if args.end:
        end_dt = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    else:
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = today  # up to (but not including) today's bars

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    year = start_dt.year

    for asset in ASSETS:
        out_path = OUT_DIR / f"{asset}_1m_{year}.parquet"
        df = download_asset(asset, start_dt, end_dt)
        df.to_parquet(out_path, index=False)
        print(
            f"  {asset}: {len(df):,} rows  "
            f"{df['open_time'].min().date()} to {df['open_time'].max().date()}  "
            f"-> {out_path.name}"
        )

    print("\nDone.")


if __name__ == "__main__":
    main()
