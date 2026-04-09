"""
discover_markets.py — One-shot diagnostic: query Kalshi to find all available
BTC-related markets so we can identify the correct series ticker for 15-min markets.

Run: python discover_markets.py
Reads KALSHI_API_KEY and KALSHI_PRIVATE_KEY from environment.
"""

import asyncio
import json
import os
import sys
import time
from base64 import b64encode
from datetime import datetime, timezone

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_PATH_PREFIX = "/trade-api/v2"

private_key = None
api_key: str = ""


def load_credentials() -> None:
    global api_key, private_key
    api_key = os.environ.get("KALSHI_API_KEY", "").strip()
    pem_val = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
    if not api_key or not pem_val:
        print("ERROR: Set KALSHI_API_KEY and KALSHI_PRIVATE_KEY env vars.")
        sys.exit(1)
    if os.path.isfile(pem_val):
        pem_bytes = open(pem_val, "rb").read()
    else:
        pem_bytes = pem_val.encode()
    private_key = serialization.load_pem_private_key(pem_bytes, password=None)
    print("Credentials loaded.")


def kalshi_headers(method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    full_path = KALSHI_PATH_PREFIX + path
    msg = (ts + method.upper() + full_path).encode()
    sig = b64encode(
        private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    ).decode()
    return {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type": "application/json",
    }


async def search_series(session, series_ticker: str) -> list:
    path = "/markets"
    params = {"series_ticker": series_ticker, "status": "open", "limit": 20}
    async with session.get(
        KALSHI_BASE_URL + path,
        headers=kalshi_headers("GET", path),
        params=params,
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        data = await resp.json()
        return data.get("markets", [])


async def search_keyword(session, keyword: str) -> list:
    """Search markets by ticker keyword (no series filter)."""
    path = "/markets"
    params = {"ticker": keyword, "status": "open", "limit": 100}
    async with session.get(
        KALSHI_BASE_URL + path,
        headers=kalshi_headers("GET", path),
        params=params,
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        data = await resp.json()
        return data.get("markets", [])


async def get_events(session, series_ticker: str) -> list:
    """Look up events (parent containers for markets) for a series."""
    path = "/events"
    params = {"series_ticker": series_ticker, "status": "open", "limit": 20}
    async with session.get(
        KALSHI_BASE_URL + path,
        headers=kalshi_headers("GET", path),
        params=params,
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        data = await resp.json()
        return data.get("events", [])


async def get_series_list(session) -> list:
    """List all available series."""
    path = "/series"
    params = {"limit": 200}
    async with session.get(
        KALSHI_BASE_URL + path,
        headers=kalshi_headers("GET", path),
        params=params,
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        if resp.status != 200:
            body = await resp.text()
            print(f"  /series returned HTTP {resp.status}: {body[:200]}")
            return []
        data = await resp.json()
        return data.get("series", [])


def print_market(m: dict, indent: str = "  "):
    now = datetime.now(timezone.utc)
    try:
        close_dt = datetime.fromisoformat(m.get("close_time", "").replace("Z", "+00:00"))
        mins_left = (close_dt - now).total_seconds() / 60
    except Exception:
        mins_left = -1
    try:
        open_dt  = datetime.fromisoformat(m.get("open_time", "").replace("Z", "+00:00"))
        close_dt2 = datetime.fromisoformat(m.get("close_time", "").replace("Z", "+00:00"))
        duration = (close_dt2 - open_dt).total_seconds() / 60
    except Exception:
        duration = -1
    print(f"{indent}ticker={m.get('ticker')} | closes_in={mins_left:.1f}m | duration={duration:.0f}m | title={m.get('title','')[:70]}")


async def main():
    load_credentials()
    now = datetime.now(timezone.utc)
    print(f"\nCurrent UTC time: {now.strftime('%Y-%m-%d %H:%M:%S')}\n")

    async with aiohttp.ClientSession() as session:

        # 1. Try known series tickers
        series_to_try = [
            "KXBTC15M", "KXBTC", "BTC15M", "BTC", "BTCUSD", "KXBTCUSD",
            "KXBTCUSD15M", "KXBTC1H", "BTCPX", "BITCOIN",
        ]
        print("=" * 70)
        print("STEP 1: Search known series tickers")
        print("=" * 70)
        for s in series_to_try:
            try:
                markets = await search_series(session, s)
                if markets:
                    print(f"\n[{s}] — {len(markets)} markets:")
                    for m in markets[:5]:
                        print_market(m)
                    if len(markets) > 5:
                        print(f"  ... and {len(markets)-5} more")
                else:
                    print(f"[{s}] — 0 markets")
            except Exception as exc:
                print(f"[{s}] — ERROR: {exc}")

        # 2. Look at events for the main series
        print("\n" + "=" * 70)
        print("STEP 2: Check /events for KXBTC and KXBTC15M")
        print("=" * 70)
        for s in ("KXBTC15M", "KXBTC", "BTC15M", "BTC"):
            try:
                events = await get_events(session, s)
                if events:
                    print(f"\n[{s}] events — {len(events)}:")
                    for e in events[:5]:
                        print(f"  event_ticker={e.get('event_ticker')} | title={e.get('title','')[:60]}")
                else:
                    print(f"[{s}] events — 0")
            except Exception as exc:
                print(f"[{s}] events ERROR: {exc}")

        # 3. Try /series endpoint to see all series
        print("\n" + "=" * 70)
        print("STEP 3: List /series — look for BTC-related entries")
        print("=" * 70)
        try:
            series_list = await get_series_list(session)
            btc_series = [s for s in series_list
                          if "btc" in s.get("ticker", "").lower()
                          or "bitcoin" in s.get("title", "").lower()
                          or "bitcoin" in s.get("ticker", "").lower()]
            print(f"Total series returned: {len(series_list)}")
            print(f"BTC-related series: {len(btc_series)}")
            for s in btc_series:
                print(f"  ticker={s.get('ticker')} | title={s.get('title', '')[:60]}")
            if not btc_series:
                print("  None found — showing first 20 series:")
                for s in series_list[:20]:
                    print(f"  ticker={s.get('ticker')} | title={s.get('title', '')[:60]}")
        except Exception as exc:
            print(f"  /series ERROR: {exc}")

        # 4. Broad open-market scan — look for any market closing within 2 hours
        print("\n" + "=" * 70)
        print("STEP 4: Broad scan — all open markets closing within 2 hours")
        print("=" * 70)
        try:
            import math
            max_ts = int(now.timestamp()) + 7200  # 2 hours from now
            path = "/markets"
            params = {
                "status": "open",
                "limit": 200,
                "max_close_ts": max_ts,
            }
            async with session.get(
                KALSHI_BASE_URL + path,
                headers=kalshi_headers("GET", path),
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                markets = data.get("markets", [])
            print(f"Markets closing within 2h: {len(markets)}")
            # Filter any that look BTC-related
            btc_markets = [m for m in markets
                           if "btc" in m.get("ticker", "").lower()
                           or "btc" in m.get("title", "").lower()
                           or "bitcoin" in m.get("title", "").lower()]
            print(f"BTC-related: {len(btc_markets)}")
            for m in btc_markets:
                print_market(m)
            if not btc_markets and markets:
                print("No BTC markets — showing all short-expiry markets:")
                for m in markets[:10]:
                    print_market(m)
        except Exception as exc:
            print(f"  Broad scan ERROR: {exc}")

        # 5. Search by min_close_ts to look for future BTC 15-min markets
        print("\n" + "=" * 70)
        print("STEP 5: Look for markets opening in the next hour")
        print("=" * 70)
        try:
            min_ts = int(now.timestamp())
            max_ts = int(now.timestamp()) + 3600
            path = "/markets"
            params = {
                "status": "open",
                "limit": 200,
                "min_close_ts": min_ts,
                "max_close_ts": max_ts,
            }
            async with session.get(
                KALSHI_BASE_URL + path,
                headers=kalshi_headers("GET", path),
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                markets = data.get("markets", [])
            print(f"Markets closing within next 1h: {len(markets)}")
            btc_markets = [m for m in markets
                           if "btc" in m.get("ticker", "").lower()
                           or "btc" in m.get("title", "").lower()
                           or "bitcoin" in m.get("title", "").lower()]
            print(f"BTC-related: {len(btc_markets)}")
            for m in btc_markets:
                print_market(m)
        except Exception as exc:
            print(f"  Step 5 ERROR: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
