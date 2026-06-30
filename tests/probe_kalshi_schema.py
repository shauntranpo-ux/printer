"""
tests/probe_kalshi_schema.py — Raw Kalshi API response shape probe.

Reads demo credentials from env; prints full JSON for each endpoint.
READ ONLY except for one IOC test order on demo (immediately cancelled
or auto-cancelled by exchange).

Run:
    python -m tests.probe_kalshi_schema 2>&1 | tee kalshi_probe_$(date +%s).log
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp
from bot_market import load_credentials, kalshi_headers
import bot_state

BASE = bot_state.KALSHI_DEMO_BASE_URL
TIMEOUT = aiohttp.ClientTimeout(total=15)


def _section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"=== {title}")
    print('='*60)


def _dump(data) -> None:
    print(json.dumps(data, indent=2, default=str))


async def probe(session: aiohttp.ClientSession) -> None:

    # ── 1. Balance ────────────────────────────────────────────────────────────
    _section("GET /portfolio/balance")
    path = "/portfolio/balance"
    async with session.get(BASE + path, headers=kalshi_headers("GET", path), timeout=TIMEOUT) as r:
        print(f"HTTP {r.status}")
        _dump(await r.json())

    # ── 2. Positions ──────────────────────────────────────────────────────────
    _section("GET /portfolio/positions?limit=5")
    path = "/portfolio/positions?limit=5"
    async with session.get(BASE + path, headers=kalshi_headers("GET", path), timeout=TIMEOUT) as r:
        print(f"HTTP {r.status}")
        data = await r.json()
        positions = data.get("market_positions") or data.get("positions") or []
        if positions:
            print("--- first position ---")
            _dump(positions[0])
            print(f"--- total in response: {len(positions)}, all keys in first: {list(positions[0].keys())} ---")
        else:
            print("(no open positions)")
            _dump(data)

    # ── 3. Orders ─────────────────────────────────────────────────────────────
    _section("GET /portfolio/orders?limit=5")
    path = "/portfolio/orders?limit=5"
    async with session.get(BASE + path, headers=kalshi_headers("GET", path), timeout=TIMEOUT) as r:
        print(f"HTTP {r.status}")
        data = await r.json()
        orders = data.get("orders") or []
        if orders:
            print("--- first order ---")
            _dump(orders[0])
            print(f"--- all keys in first: {list(orders[0].keys())} ---")
        else:
            print("(no orders)")
            _dump(data)

    # ── 4. Fills ──────────────────────────────────────────────────────────────
    _section("GET /portfolio/fills?limit=5")
    path = "/portfolio/fills?limit=5"
    async with session.get(BASE + path, headers=kalshi_headers("GET", path), timeout=TIMEOUT) as r:
        print(f"HTTP {r.status}")
        data = await r.json()
        fills = data.get("fills") or []
        if fills:
            print("--- first fill ---")
            _dump(fills[0])
            print(f"--- all keys in first: {list(fills[0].keys())} ---")
        else:
            print("(no fills in account — will check after test order below)")
            _dump(data)

    # ── 5. Markets ────────────────────────────────────────────────────────────
    _section("GET /markets?series_ticker=KXBTC15M&limit=1")
    path = "/markets?series_ticker=KXBTC15M&limit=1"
    async with session.get(BASE + path, headers=kalshi_headers("GET", path), timeout=TIMEOUT) as r:
        print(f"HTTP {r.status}")
        data = await r.json()
        markets = data.get("markets") or []
        if markets:
            print("--- first market ---")
            _dump(markets[0])
            print(f"--- all keys in first: {list(markets[0].keys())} ---")
        else:
            print("(no open KXBTC15M markets)")
            _dump(data)

    # ── 6. Test order (IOC, 1 contract YES at 1c — far OOM, should not fill) ─
    _section("POST /portfolio/orders  [IOC test — demo only]")
    order_ticker = None
    if markets:
        order_ticker = markets[0].get("ticker")

    if not order_ticker:
        print("No market ticker available — skipping test order.")
    else:
        print(f"Using ticker: {order_ticker}")
        order_path = "/portfolio/orders"
        payload = {
            "ticker":          order_ticker,
            "action":          "buy",
            "side":            "yes",
            "count":           1,
            "yes_price":       1,   # 1 cent — far OOM, IOC will cancel immediately
            "time_in_force":   "immediate_or_cancel",
        }
        print("Request payload:")
        _dump(payload)
        async with session.post(
            BASE + order_path,
            headers=kalshi_headers("POST", order_path),
            json=payload,
            timeout=TIMEOUT,
        ) as r:
            print(f"HTTP {r.status}")
            order_resp = await r.json()
            _dump(order_resp)

        # ── 7. DELETE if resting ──────────────────────────────────────────────
        order_obj = order_resp.get("order") or order_resp
        order_id = order_obj.get("order_id") or order_obj.get("id")
        status = order_obj.get("status", "")
        print(f"\norder_id={order_id!r}  status={status!r}")

        if order_id and status == "resting":
            _section(f"DELETE /portfolio/orders/{order_id}  [cleanup resting order]")
            del_path = f"/portfolio/orders/{order_id}"
            async with session.delete(
                BASE + del_path,
                headers=kalshi_headers("DELETE", del_path),
                timeout=TIMEOUT,
            ) as r:
                print(f"HTTP {r.status}")
                _dump(await r.json())
        else:
            print("Order not resting — no DELETE needed.")

        # Re-fetch fills to capture any fill from the test order
        _section("GET /portfolio/fills?limit=5  [re-check after test order]")
        path = "/portfolio/fills?limit=5"
        async with session.get(BASE + path, headers=kalshi_headers("GET", path), timeout=TIMEOUT) as r:
            print(f"HTTP {r.status}")
            data = await r.json()
            fills = data.get("fills") or []
            if fills:
                print("--- first fill (post-order) ---")
                _dump(fills[0])
                print(f"--- all keys in first: {list(fills[0].keys())} ---")
            else:
                print("(still no fills)")
                _dump(data)

    _section("PROBE COMPLETE")


async def main() -> None:
    if not os.environ.get("KALSHI_DEMO_API_KEY") or not os.environ.get("KALSHI_DEMO_PRIVATE_KEY"):
        print(
            "ERROR: KALSHI_DEMO_API_KEY and KALSHI_DEMO_PRIVATE_KEY must be set.\n"
            "  export KALSHI_DEMO_API_KEY=<uuid>\n"
            "  export KALSHI_DEMO_PRIVATE_KEY=<pem-string-or-path>"
        )
        sys.exit(1)

    load_credentials(mode="demo")
    print(f"Base URL: {BASE}")
    print(f"Bot state base URL after load: {bot_state.KALSHI_BASE_URL}")
    if bot_state.KALSHI_BASE_URL != BASE:
        print("ERROR: KALSHI_BASE_URL does not match demo URL after load — credential load failed.")
        sys.exit(1)
    if bot_state.private_key is None:
        print("ERROR: private_key is None after load_credentials — check key format.")
        sys.exit(1)
    async with aiohttp.ClientSession() as session:
        await probe(session)


if __name__ == "__main__":
    asyncio.run(main())
