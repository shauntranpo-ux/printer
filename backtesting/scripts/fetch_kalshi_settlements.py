"""
Fetch all settled Kalshi 15-minute markets for an asset and save settlement data.

Output: data/historical/{ASSET}_kalshi_settlements.parquet
Schema:
    window_open  datetime64[ns, UTC]  — market open (close_time - 15 min)
    close_time   datetime64[ns, UTC]  — market expiry
    ticker       str
    strike       float64              — floor_strike (ATM at market open)
    result       int8                 — 1 = YES, 0 = NO

Usage:
    python backtesting/scripts/fetch_kalshi_settlements.py --asset BTC
    python backtesting/scripts/fetch_kalshi_settlements.py --asset BTC ETH SOL XRP
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from base64 import b64encode
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import pandas as pd
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE   = "https://api.elections.kalshi.com/trade-api/v2"
PREFIX = "/trade-api/v2"

# Primary series ticker for each asset's 15m markets
_SERIES: dict[str, str] = {
    "BTC":  "KXBTC15M",
    "ETH":  "KXETH15M",
    "SOL":  "KXSOL15M",
    "XRP":  "KXXRP15M",
    "DOGE": "KXDOGE15M",
}


def _build_private_key():
    api_key = os.environ.get("KALSHI_API_KEY", "").strip()
    pem_val = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
    if not api_key or not pem_val:
        sys.exit("ERROR: KALSHI_API_KEY and KALSHI_PRIVATE_KEY env vars required.")
    pem_bytes = open(pem_val, "rb").read() if os.path.exists(pem_val) else pem_val.encode()
    return api_key, serialization.load_pem_private_key(pem_bytes, password=None)


def _headers(api_key: str, private_key, method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    msg = (ts + method.upper() + PREFIX + path).encode()
    sig = b64encode(
        private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    ).decode()
    return {
        "KALSHI-ACCESS-KEY":       api_key,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
    }


def fetch_settled_markets(asset: str, api_key: str, private_key) -> list[dict]:
    series = _SERIES.get(asset.upper())
    if not series:
        print(f"[{asset}] No series ticker configured — skipping.")
        return []

    all_markets: list[dict] = []
    cursor: str | None = None
    page = 0

    while True:
        path = "/markets"
        params: dict = {"series_ticker": series, "status": "settled", "limit": 200}
        if cursor:
            params["cursor"] = cursor

        r = requests.get(
            BASE + path,
            headers=_headers(api_key, private_key, "GET", path),
            params=params,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        batch = data.get("markets", [])
        all_markets.extend(batch)
        cursor = data.get("cursor") or ""
        page += 1
        print(f"  [{asset}] page {page:3d}: +{len(batch):3d} markets  total={len(all_markets):5d}  more={bool(cursor)}")

        if not cursor or not batch:
            break

        # Polite rate-limit buffer
        time.sleep(0.1)

    return all_markets


def build_settlements_df(markets: list[dict]) -> pd.DataFrame:
    rows = []
    for m in markets:
        close_str = m.get("close_time")
        strike    = m.get("floor_strike") or m.get("strike_val") or m.get("cap_strike")
        result    = m.get("result", "")
        ticker    = m.get("ticker", "")

        if not close_str or strike is None or result not in ("yes", "no"):
            continue

        close_dt  = pd.Timestamp(close_str, tz="UTC")
        window_open = close_dt - pd.Timedelta(minutes=15)

        rows.append({
            "window_open": window_open,
            "close_time":  close_dt,
            "ticker":      ticker,
            "strike":      float(strike),
            "result":      int(result == "yes"),
        })

    if not rows:
        return pd.DataFrame(columns=["window_open", "close_time", "ticker", "strike", "result"])

    df = pd.DataFrame(rows)
    df = df.sort_values("window_open").reset_index(drop=True)
    df["result"] = df["result"].astype("int8")
    return df


def main():
    parser = argparse.ArgumentParser(description="Fetch Kalshi 15m settlement data")
    parser.add_argument("--asset", nargs="+", default=["BTC"],
                        choices=list(_SERIES.keys()), help="Asset(s) to fetch")
    parser.add_argument("--out-dir", default=str(ROOT / "data" / "historical"),
                        help="Output directory for parquet files")
    args = parser.parse_args()

    api_key, private_key = _build_private_key()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for asset in args.asset:
        asset = asset.upper()
        print(f"\n[{asset}] Fetching settled markets for {_SERIES.get(asset)} ...")
        markets = fetch_settled_markets(asset, api_key, private_key)

        if not markets:
            print(f"[{asset}] No markets found.")
            continue

        df = build_settlements_df(markets)
        out_path = out_dir / f"{asset}_kalshi_settlements.parquet"
        df.to_parquet(out_path, index=False)

        print(f"[{asset}] {len(df):,} settlements saved -> {out_path}")
        print(f"  window_open range: {df['window_open'].min()}  ->  {df['window_open'].max()}")
        yes_rate = df['result'].mean()
        print(f"  YES rate: {yes_rate:.1%}  (expected ~50%)")


if __name__ == "__main__":
    main()
