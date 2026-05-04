"""
Fetch 1-minute OHLCV bars from Coinbase Exchange public API and append
to existing data/historical/{ASSET}_1m_extended.parquet files.

No authentication required. Rate limit: 10 req/s (we sleep 0.15s).

Usage:
    python backtesting/scripts/fetch_bars_coinbase.py
    python backtesting/scripts/fetch_bars_coinbase.py --assets BTC ETH
    python backtesting/scripts/fetch_bars_coinbase.py --start 2026-04-20 --end 2026-05-04
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

BASE = "https://api.exchange.coinbase.com"
CANDLES = "/products/{product}/candles"
MAX_PER_REQUEST = 300       # Coinbase limit
GRANULARITY = 60            # 1-minute bars in seconds
SLEEP = 0.15                # seconds between requests

PRODUCTS = {
    "BTC":  "BTC-USD",
    "ETH":  "ETH-USD",
    "SOL":  "SOL-USD",
    "XRP":  "XRP-USD",
    "DOGE": "DOGE-USD",
}


def fetch_chunk(product: str, start: datetime, end: datetime) -> list[dict]:
    url = BASE + CANDLES.format(product=product)
    params = {
        "granularity": GRANULARITY,
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 200:
                data = r.json()
                # Response: [[time, low, high, open, close, volume], ...]
                return [
                    {
                        "timestamp": float(row[0]),
                        "low":       float(row[1]),
                        "high":      float(row[2]),
                        "open":      float(row[3]),
                        "close":     float(row[4]),
                        "volume":    float(row[5]),
                    }
                    for row in data
                ]
            print(f"  HTTP {r.status_code}: {r.text[:120]}")
            time.sleep(2 ** attempt)
        except requests.RequestException as exc:
            print(f"  request error ({attempt+1}/3): {exc}")
            time.sleep(2 ** attempt)
    return []


def fetch_range(asset: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    product = PRODUCTS[asset]
    rows: list[dict] = []
    cur = start_dt
    step = MAX_PER_REQUEST * GRANULARITY  # seconds per chunk

    while cur < end_dt:
        chunk_end = datetime.fromtimestamp(
            min(cur.timestamp() + step, end_dt.timestamp()), tz=timezone.utc
        )
        chunk = fetch_chunk(product, cur, chunk_end)
        if chunk:
            rows.extend(chunk)
            newest = datetime.fromtimestamp(max(r["timestamp"] for r in chunk), tz=timezone.utc)
            print(f"  {asset}: +{len(chunk)} rows, newest={newest}")
        cur = chunk_end
        time.sleep(SLEEP)

    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows).drop_duplicates(subset=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def append_to_parquet(new_df: pd.DataFrame, path: Path) -> pd.DataFrame:
    if path.exists():
        existing = pd.read_parquet(path)
        last_ts = existing["timestamp"].max()
        new_df = new_df[new_df["timestamp"] > last_ts]
        if new_df.empty:
            print(f"  No new rows beyond {last_ts} — parquet already up to date.")
            return existing
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        combined = new_df

    combined = combined.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    combined.to_parquet(path, index=False)
    return combined


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", nargs="+", default=list(PRODUCTS.keys()))
    parser.add_argument("--start", default=None, help="YYYY-MM-DD (default: day after last bar in parquet)")
    parser.add_argument("--end",   default=None, help="YYYY-MM-DD (default: today UTC)")
    parser.add_argument("--out-dir", default=str(ROOT / "data" / "historical"))
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    end_dt = (
        datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
        if args.end
        else datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    )

    for asset in args.assets:
        asset = asset.upper()
        if asset not in PRODUCTS:
            print(f"Unknown asset {asset}, skipping.")
            continue

        path = out_dir / f"{asset}_1m_extended.parquet"

        if args.start:
            start_dt = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
        elif path.exists():
            existing = pd.read_parquet(path)
            last_ts = existing["timestamp"].max()
            start_dt = datetime.fromtimestamp(last_ts + GRANULARITY, tz=timezone.utc)
            print(f"\n== {asset} ==  resuming from {start_dt}")
        else:
            print(f"\n== {asset} ==  no existing parquet, use --start to specify a start date")
            continue

        if start_dt >= end_dt:
            print(f"  {asset}: already up to date (last={start_dt}, end={end_dt})")
            continue

        print(f"\n== {asset} ({PRODUCTS[asset]}) ==  {start_dt} -> {end_dt}")
        new_df = fetch_range(asset, start_dt, end_dt)
        if new_df.empty:
            print(f"  {asset}: no data returned.")
            continue

        combined = append_to_parquet(new_df, path)
        newest = datetime.fromtimestamp(combined["timestamp"].max(), tz=timezone.utc)
        print(f"  {asset}: {len(combined):,} total rows, newest={newest}")


if __name__ == "__main__":
    main()
