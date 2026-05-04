"""Probe: enumerate settled KXBTC15M markets to find date range."""
import os, sys, time
from base64 import b64encode
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
import requests

api_key = os.environ.get("KALSHI_API_KEY", "").strip()
pem_val = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
if not api_key or not pem_val:
    sys.exit("KALSHI_API_KEY or KALSHI_PRIVATE_KEY not set")

pem_bytes = open(pem_val, "rb").read() if os.path.exists(pem_val) else pem_val.encode()
private_key = serialization.load_pem_private_key(pem_bytes, password=None)
BASE   = "https://api.elections.kalshi.com/trade-api/v2"
PREFIX = "/trade-api/v2"

def req(path, params=None):
    ts = str(int(time.time() * 1000))
    msg = (ts + "GET" + PREFIX + path).encode()
    sig = b64encode(private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )).decode()
    h = {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
    }
    r = requests.get(BASE + path, headers=h, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

all_markets = []
cursor = None
while True:
    params = {"series_ticker": "KXBTC15M", "status": "settled", "limit": 200}
    if cursor:
        params["cursor"] = cursor
    data = req("/markets", params)
    batch = data.get("markets", [])
    all_markets.extend(batch)
    cursor = data.get("cursor", "")
    print(f"page +{len(batch):3d}  total={len(all_markets):5d}  more={bool(cursor)}")
    if not cursor or not batch or len(all_markets) > 10000:
        break

dates = sorted(m.get("close_time", "") for m in all_markets)
print(f"\nTotal markets : {len(all_markets)}")
print(f"Oldest close  : {dates[0] if dates else 'none'}")
print(f"Newest close  : {dates[-1] if dates else 'none'}")
print("\nSample:")
step = max(1, len(all_markets) // 5)
for m in all_markets[::step][:6]:
    print(f"  {m.get('ticker')}  strike={m.get('floor_strike')}  result={m.get('result')}")
