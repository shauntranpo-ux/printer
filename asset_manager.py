"""
asset_manager.py — Asset configuration and Coinbase price feeds.

Provides:
  ASSET_CONFIG        — per-asset metadata (strike increment, Kalshi series, Coinbase symbol)
  coinbase_price_task() — async coroutine maintaining per-asset price deques
  seed_price_history()  — pre-fills price deques from Coinbase Exchange candles on startup
  get_price(asset)    — latest price for an asset
  price_age_seconds   — seconds since last update
"""

import asyncio
import json
import logging
import os
import time
from collections import deque

log = logging.getLogger("bot")

# ── Asset registry ────────────────────────────────────────────────────────────
ASSET_CONFIG = {
    "BTC": {
        "binance_symbol":   "btcusdt",
        "strike_increment": 1000.0,
        "kalshi_series":    ("KXBTC15M", "KXBTCD", "BTCD-B"),
    },
    "ETH": {
        "binance_symbol":   "ethusdt",
        "strike_increment": 25.0,
        "kalshi_series":    ("KXETH15M", "KXETHD", "ETHD-B"),
    },
    "SOL": {
        "binance_symbol":   "solusdt",
        "strike_increment": 1.0,
        "kalshi_series":    ("KXSOLD", "SOLD-B", "KXSOL15M", "KXSOL15", "KXSOL", "SOL"),
    },
    "XRP": {
        "binance_symbol":   "xrpusdt",
        "strike_increment": 0.01,
        "kalshi_series":    ("KXXRPD", "XRPD-B", "KXXRP15M", "KXXRP15", "KXXRP", "XRP"),
    },
    "DOGE": {
        "binance_symbol":   "dogeusdt",
        "strike_increment": 0.001,
        "kalshi_series":    ("KXDOGE15M", "KXDOGED", "DOGED-B", "KXDOGE", "DOGE"),
    },
}

# ── Per-asset price deques ────────────────────────────────────────────────────
# asset → deque[(unix_ts, price)]
_prices: dict[str, deque] = {asset: deque(maxlen=2000) for asset in ASSET_CONFIG}


# ── Price accessors ───────────────────────────────────────────────────────────

def get_price(asset: str) -> float | None:
    """Return the most recent price for an asset, or None if no data."""
    dq = _prices.get(asset)
    if not dq:
        return None
    return dq[-1][1]


def price_age_seconds(asset: str) -> float | None:
    """Return seconds since last price update, or None if no data."""
    dq = _prices.get(asset)
    if not dq:
        return None
    return time.time() - dq[-1][0]


# ── Price feeds ───────────────────────────────────────────────────────────────

# Coinbase public REST URL for spot prices — no auth, works globally
_COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/{asset}-USD/spot"

# Coinbase Exchange public candles API — no auth, supports all 5 assets
_COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/{product_id}/candles"

_COINBASE_PRODUCTS = {
    "BTC":  "BTC-USD",
    "ETH":  "ETH-USD",
    "SOL":  "SOL-USD",
    "XRP":  "XRP-USD",
    "DOGE": "DOGE-USD",
}


async def seed_price_history(assets: list[str]) -> None:
    """
    Pre-fill _prices deques with 30 historical 1-minute bars so Supertrend
    has enough data immediately after a cold start / crash restart.
    Uses Coinbase Exchange public candles API (no auth, supports all 5 assets).
    """
    import aiohttp as _aiohttp

    for asset in assets:
        dq = _prices.get(asset)
        if dq is None:
            continue

        product_id = _COINBASE_PRODUCTS.get(asset)
        if not product_id:
            log.warning("[%s] seed_price_history: no Coinbase product mapping", asset)
            continue

        try:
            async with _aiohttp.ClientSession() as s:
                async with s.get(
                    _COINBASE_CANDLES_URL.format(product_id=product_id),
                    params={"granularity": "60"},
                    timeout=_aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status == 200:
                        # Returns [[time, low, high, open, close, volume], ...] descending
                        candles = await resp.json()
                        # Take last 30 bars (candles are newest-first, reverse for chronological)
                        bars = list(reversed(candles[:30]))
                        for bar in bars:
                            # bar[0]=time (unix), bar[4]=close
                            dq.append((float(bar[0]), float(bar[4])))
                        log.info(
                            "[%s] seed_price_history: %d bars from Coinbase Exchange",
                            asset, len(bars),
                        )
                    else:
                        log.warning(
                            "[%s] seed_price_history: Coinbase returned HTTP %d",
                            asset, resp.status,
                        )
        except Exception as exc:
            log.warning("[%s] seed_price_history failed: %s", asset, exc)


async def coinbase_price_task(enabled_assets: list[str]) -> None:
    """
    Real-time REST-polling price feed using Coinbase public spot API.
    Polls every 2 seconds per asset. Primary price source for all assets.
    """
    import aiohttp as _aiohttp

    valid_assets = [a for a in enabled_assets if a in ASSET_CONFIG]
    if not valid_assets:
        return

    log.info(f"Coinbase price feed starting for {valid_assets}")
    while True:
        try:
            async with _aiohttp.ClientSession() as session:
                while True:
                    for asset in valid_assets:
                        try:
                            url = _COINBASE_SPOT_URL.format(asset=asset)
                            async with session.get(
                                url, timeout=_aiohttp.ClientTimeout(total=5)
                            ) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    price = float(data["data"]["amount"])
                                    now_ts = time.time()
                                    dq = _prices[asset]
                                    if not dq or (now_ts - dq[-1][0]) >= 1.0:
                                        dq.append((now_ts, price))
                        except Exception as _e:
                            log.debug(f"Coinbase feed [{asset}] error: {_e}")
                    await asyncio.sleep(2)
        except Exception as exc:
            log.warning(f"Coinbase feed session error: {exc}. Retrying in 5s...")
            await asyncio.sleep(5)
