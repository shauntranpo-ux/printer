"""Smoke test: run ExchangeManager 90 s and print last 5 1-minute bars for BTC + ETH.

Usage:
    uv run python scripts/smoke_exchange.py
"""

import asyncio
import sys


async def main() -> None:
    from kalshi_botv3.exchange.manager import ExchangeManager
    from kalshi_botv3.utils.logging import configure_logging

    configure_logging("INFO")

    print("[smoke] Starting ExchangeManager (BTC, ETH)...")
    manager = ExchangeManager(markets=["BTC", "ETH"], backfill_minutes=90)

    await manager.start()
    print("[smoke] Manager started. Waiting 90 s for live data...")

    import contextlib

    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.sleep(90)

    print(f"\n[smoke] Healthy: {manager.is_healthy()}")

    for market in ["BTC", "ETH"]:
        df = manager.aggregator[market].get_ohlcv(n=5)
        print(f"\n--- {market} last 5 1-minute bars ---")
        if df.empty:
            print("  (no bars yet)")
        else:
            print(df.to_string())

    await manager.stop()
    print("\n[smoke] Done.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[smoke] Interrupted.")
        sys.exit(0)
