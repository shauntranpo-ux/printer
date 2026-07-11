"""
scripts/fetch_candles.py - download 1-min spot candles matching the settlement history.

The settlement parquets record WHAT each 15-min window resolved to but carry no
prices, so intra-window signals (S1 momentum continuation, S4 stall-fade, the
favorite calibration behind S2/S8) can't be tested from them alone. This script
fills that gap: public 1-minute candles from Coinbase Exchange (no API key) for
every asset with a settlement parquet, spanning the same dates plus a 90-min lead
for trailing-vol warmup, written to data/historical/{ASSET}_candles_1m.parquet for
scripts/backtest_signals.py.

RUN THIS FROM A MACHINE WITH OPEN INTERNET - a home PC works; hosted containers
usually block exchange hosts at the network layer. Safe to interrupt and re-run:
progress is saved every ~25 requests and the fetch resumes from the last saved
minute. Expect ~330 requests per asset for the 68-day span (a few minutes total).

Offline tooling only - NOT loaded at runtime.

    pip install pandas pyarrow requests
    python scripts/fetch_candles.py [data/historical]
"""
import glob
import os
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import pandas as pd
    import requests
except ImportError:
    print("pandas + requests required: pip install pandas pyarrow requests")
    sys.exit(1)

_BASE = "https://api.exchange.coinbase.com"
_PRODUCTS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD",
             "XRP": "XRP-USD", "DOGE": "DOGE-USD"}
_CHUNK_MIN = 300          # API max candles per request at granularity=60
_WARMUP_MIN = 90          # lead time so the first windows have trailing vol
_SLEEP = 0.15             # ~6 req/s, comfortably under the 10/s public limit
_RETRIES = 5


def _fetch_chunk(session, product, start, end):
    """One candles request; returns rows [(ts, open, high, low, close, volume)]."""
    url = f"{_BASE}/products/{product}/candles"
    params = {"granularity": 60, "start": start.isoformat(), "end": end.isoformat()}
    for attempt in range(_RETRIES):
        try:
            r = session.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(2.0 * (attempt + 1))
                continue
            r.raise_for_status()
            # API returns [time, low, high, open, close, volume], newest first.
            return [(int(t), o, h, lo, c, v) for t, lo, h, o, c, v in r.json()]
        except requests.RequestException as e:
            if attempt == _RETRIES - 1:
                raise
            print(f"    retry {attempt + 1}: {e}")
            time.sleep(2 ** attempt)
    return []


def _span_from_settlements(path):
    df = pd.read_parquet(path)
    start = df["window_open"].min().to_pydatetime() - timedelta(minutes=_WARMUP_MIN)
    end = df["close_time"].max().to_pydatetime() + timedelta(minutes=1)
    return start, end


def fetch_asset(session, asset, settle_path, out_path):
    start, end = _span_from_settlements(settle_path)
    rows = []
    if os.path.exists(out_path):
        prev = pd.read_parquet(out_path)
        if len(prev):
            resume = prev["ts"].max().to_pydatetime() + timedelta(minutes=1)
            if resume >= end:
                print(f"  {asset}: already complete ({len(prev)} rows)")
                return
            print(f"  {asset}: resuming from {resume:%Y-%m-%d %H:%M}")
            rows = list(prev.itertuples(index=False, name=None))
            start = resume
    total_chunks = max(1, int((end - start).total_seconds() // 60 // _CHUNK_MIN) + 1)
    product = _PRODUCTS[asset]
    cur, done = start, 0
    while cur < end:
        chunk_end = min(cur + timedelta(minutes=_CHUNK_MIN), end)
        got = _fetch_chunk(session, product, cur, chunk_end)
        rows.extend(
            (datetime.fromtimestamp(t, tz=timezone.utc), o, h, lo, c, v)
            for t, o, h, lo, c, v in got)
        cur = chunk_end
        done += 1
        if done % 25 == 0 or cur >= end:
            df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
            df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
            df.to_parquet(out_path, index=False)
            rows = list(df.itertuples(index=False, name=None))
            print(f"  {asset}: {done}/{total_chunks} chunks, {len(df)} candles saved")
        time.sleep(_SLEEP)


def main():
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "data/historical"
    settles = sorted(glob.glob(os.path.join(data_dir, "*_kalshi_settlements.parquet")))
    if not settles:
        print(f"no settlement parquets under {data_dir}")
        sys.exit(1)
    session = requests.Session()
    session.headers["User-Agent"] = "candle-fetch/1.0"
    # Fail fast with a useful message when the network path is blocked.
    try:
        session.get(f"{_BASE}/products/BTC-USD", timeout=15).raise_for_status()
    except requests.RequestException as e:
        print(f"cannot reach {_BASE}: {e}\n"
              "This host is blocked from restricted containers - run the script from a "
              "machine with open internet (home PC), then commit the candle parquets.")
        sys.exit(1)
    for path in settles:
        asset = os.path.basename(path).split("_")[0]
        if asset not in _PRODUCTS:
            print(f"  {asset}: no Coinbase product mapping, skipped")
            continue
        out = os.path.join(data_dir, f"{asset}_candles_1m.parquet")
        fetch_asset(session, asset, path, out)
    print("done - run: python scripts/backtest_signals.py")


if __name__ == "__main__":
    main()
