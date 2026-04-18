"""
Download extended historical 1-minute OHLCV for BTC, ETH, SOL, XRP,
DOGE from Binance public REST API. Covers 2020-01-01 through the
most recent full day.

Why 2020: SOL began trading on Binance in April 2020. BTC/ETH/XRP/DOGE
have more history, but we want consistent windows across assets for
cross-asset analysis.

Binance klines endpoint: up to 1000 rows per request. We paginate
with end-time cursoring and save incrementally. Rate limit: 100ms
between requests is safe.

Output: data/historical/{ASSET}_1m_extended.parquet

Usage:
    python scripts\download_historical_extended.py
    python scripts\download_historical_extended.py --assets BTC
    python scripts\download_historical_extended.py --start 2023-01-01
    python scripts\download_historical_extended.py --resume
"""

from __future__ import annotations
import argparse
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests


BINANCE_BASE = "https://api.binance.com"
KLINES_ENDPOINT = "/api/v3/klines"
INTERVAL = "1m"
LIMIT_PER_REQUEST = 1000
SLEEP_BETWEEN = 0.12  # 120ms, comfortably under rate limit

SYMBOL_MAP = {
    "BTC":  "BTCUSDT",
    "ETH":  "ETHUSDT",
    "SOL":  "SOLUSDT",
    "XRP":  "XRPUSDT",
    "DOGE": "DOGEUSDT",
}


def fetch_kline_chunk(
    symbol: str,
    start_ms: int,
    end_ms: int,
    retries: int = 3,
) -> list:
    params = {
        "symbol": symbol,
        "interval": INTERVAL,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": LIMIT_PER_REQUEST,
    }
    for attempt in range(retries):
        try:
            r = requests.get(BINANCE_BASE + KLINES_ENDPOINT, params=params, timeout=10)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = (2 ** attempt) * 2.0
                print(f"  429 rate limit, sleeping {wait}s")
                time.sleep(wait)
                continue
            print(f"  HTTP {r.status_code}: {r.text[:200]}")
            time.sleep(1.0)
        except requests.RequestException as e:
            print(f"  request error (attempt {attempt + 1}/{retries}): {e}")
            time.sleep(1.0)
    return []


def load_existing_parquet(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
        if len(df) == 0:
            return None
        return df.sort_values("timestamp").reset_index(drop=True)
    except Exception:
        return None


def download_asset(
    asset: str,
    start_dt: datetime,
    end_dt: datetime,
    output_path: Path,
    resume: bool = False,
) -> pd.DataFrame:
    symbol = SYMBOL_MAP[asset]

    existing = load_existing_parquet(output_path) if resume else None
    if existing is not None and len(existing) > 0:
        last_ts_ms = int(existing["timestamp"].max() * 1000) + 60_000
        start_ms = max(int(start_dt.timestamp() * 1000), last_ts_ms)
        print(f"  {asset}: resuming from {datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)}")
    else:
        start_ms = int(start_dt.timestamp() * 1000)

    end_ms = int(end_dt.timestamp() * 1000)

    all_rows = []
    if existing is not None:
        for _, row in existing.iterrows():
            all_rows.append({
                "timestamp": row["timestamp"],
                "open":      row.get("open", None),
                "high":      row.get("high", None),
                "low":       row.get("low", None),
                "close":     row["close"],
                "volume":    row.get("volume", None),
            })

    current_ms = start_ms
    request_count = 0
    while current_ms < end_ms:
        chunk_end_ms = min(current_ms + LIMIT_PER_REQUEST * 60_000, end_ms)
        data = fetch_kline_chunk(symbol, current_ms, chunk_end_ms)
        request_count += 1

        if not data:
            print(f"  empty chunk at {datetime.fromtimestamp(current_ms / 1000, tz=timezone.utc)}; advancing")
            current_ms = chunk_end_ms + 1
            continue

        for row in data:
            ts_ms = row[0]
            all_rows.append({
                "timestamp": ts_ms / 1000.0,
                "open":      float(row[1]),
                "high":      float(row[2]),
                "low":       float(row[3]),
                "close":     float(row[4]),
                "volume":    float(row[5]),
            })

        last_ts_ms = data[-1][0]
        next_start = last_ts_ms + 60_000
        if next_start <= current_ms:
            current_ms = chunk_end_ms + 1
        else:
            current_ms = next_start

        if request_count % 20 == 0:
            df = pd.DataFrame(all_rows).drop_duplicates(subset=["timestamp"])
            df = df.sort_values("timestamp").reset_index(drop=True)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(output_path, index=False)
            newest = datetime.fromtimestamp(df["timestamp"].max(), tz=timezone.utc)
            print(f"  {asset}: {len(df):,} rows, newest={newest}")

        time.sleep(SLEEP_BETWEEN)

    df = pd.DataFrame(all_rows).drop_duplicates(subset=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(output_path, index=False)
    return df


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", nargs="+", default=list(SYMBOL_MAP.keys()))
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=None,
                        help="YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--output-dir", default="data/historical")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from last timestamp in existing parquet")
    args = parser.parse_args()

    start_dt = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    if args.end:
        end_dt = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)
        end_dt = (now - timedelta(days=1)).replace(hour=23, minute=59, second=0, microsecond=0)

    output_dir = Path(args.output_dir)
    for asset in args.assets:
        if asset not in SYMBOL_MAP:
            print(f"Unknown asset {asset}, skipping")
            continue
        print(f"\n== {asset} ({SYMBOL_MAP[asset]}) ==")
        path = output_dir / f"{asset}_1m_extended.parquet"
        t0 = time.time()
        df = download_asset(asset, start_dt, end_dt, path, resume=args.resume)
        print(f"  {asset}: final {len(df):,} rows, {time.time() - t0:.1f}s")
        if len(df) > 0:
            print(f"    range: {datetime.fromtimestamp(df['timestamp'].min(), tz=timezone.utc)}")
            print(f"        to {datetime.fromtimestamp(df['timestamp'].max(), tz=timezone.utc)}")


if __name__ == "__main__":
    main()
