"""Smoke test: hit Kalshi demo API with real credentials.

Usage:
    uv run python scripts/smoke_kalshi.py

Requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env or environment.
Fails cleanly if credentials are absent or point to dry_run mode.
"""

import asyncio
import sys


async def main() -> None:
    try:
        from kalshi_botv3.config.settings import get_settings

        settings = get_settings()
    except Exception:
        print(
            "[smoke] No credentials configured (KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH).\n"
            "        Set them in .env or environment to run against demo.",
        )
        sys.exit(0)

    if settings.mode == "dry_run":
        print("[smoke] mode=dry_run — no real credentials configured. Skipping live call.")
        sys.exit(0)

    from kalshi_botv3.kalshi.auth import KalshiSigner
    from kalshi_botv3.kalshi.client import HttpKalshiClient

    signer = KalshiSigner(settings.kalshi_private_key_path)

    async with HttpKalshiClient(signer, settings.kalshi_env) as client:
        print("[smoke] Calling get_exchange_status()...")
        status = await client.get_exchange_status()
        print(f"  trading_active={status.trading_active}  exchange_active={status.exchange_active}")

        print("\n[smoke] Calling get_markets('KXBTC15M')...")
        result = await client.get_markets("KXBTC15M", status="open", limit=5)
        print(f"  Found {len(result.markets)} open market(s)")
        for m in result.markets:
            print(f"  {m.ticker}  yes_bid={m.yes_bid}  yes_ask={m.yes_ask}  status={m.status}")

    print("\n[smoke] Done.")


if __name__ == "__main__":
    asyncio.run(main())
