"""
asset_manager.py - Asset configuration and Coinbase price feeds.

Provides:
  ASSET_CONFIG        - per-asset metadata (strike increment, Kalshi series, Coinbase symbol)
  coinbase_price_task() - price feed supervisor: websocket primary, REST polling fallback
  seed_price_history()  - pre-fills price deques from Coinbase Exchange candles on startup
  get_price(asset)    - latest price for an asset
  price_age_seconds   - seconds since last update

The feed is the Coinbase Exchange websocket ticker channel, decimated to at most one
deque append per second per asset so every downstream consumer (sigma estimator dt
filters, S2 tick confirmation, S1 lookback age gates) sees the same cadence as the
old 2s REST poll, just fresher and co-sampled across assets. If the socket stalls or
an asset trades thinly, the supervisor REST-polls that asset until ticks resume.
"""

import asyncio
import json
import logging
import os
import random
import time
from collections import deque

log = logging.getLogger("bot")

# Asset registry
ASSET_CONFIG = {
    "BTC": {
        "strike_increment": 1000.0,
        "kalshi_series":    ("KXBTC15M", "KXBTCD", "BTCD-B"),
    },
    "ETH": {
        "strike_increment": 25.0,
        "kalshi_series":    ("KXETH15M", "KXETHD", "ETHD-B"),
    },
    "SOL": {
        "strike_increment": 1.0,
        "kalshi_series":    ("KXSOLD", "SOLD-B", "KXSOL15M", "KXSOL15", "KXSOL", "SOL"),
    },
    "XRP": {
        "strike_increment": 0.01,
        "kalshi_series":    ("KXXRPD", "XRPD-B", "KXXRP15M", "KXXRP15", "KXXRP", "XRP"),
    },
    "DOGE": {
        "strike_increment": 0.001,
        "kalshi_series":    ("KXDOGE15M", "KXDOGED", "DOGED-B", "KXDOGE", "DOGE"),
    },
}

# Per-asset price deques
# asset -> deque[(unix_ts, price)]
_prices: dict[str, deque] = {asset: deque(maxlen=2000) for asset in ASSET_CONFIG}

# 24h change cache (updated every 5 min from Coinbase stats endpoint)
_ch24: dict[str, float | None] = {asset: None for asset in ASSET_CONFIG}
_ch24_last_fetch: float = 0.0


# Price accessors

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


def get_24h_change(asset: str) -> float | None:
    """Return 24h price change % for an asset, or None if not yet fetched."""
    return _ch24.get(asset)


# Price feeds

# Coinbase public REST URL for spot prices - no auth, works globally
_COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/{asset}-USD/spot"

# Coinbase Exchange public candles API - no auth, supports all 5 assets
_COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/{product_id}/candles"

_COINBASE_STATS_URL  = "https://api.exchange.coinbase.com/products/{product_id}/stats"
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


# Websocket feed settings
_COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
_DECIMATE_SECS   = 1.0    # at most one deque append per second per asset (matches old REST cadence)
_WS_STALE_SECS   = 10.0   # per-asset: REST-poll an asset whose last append is older than this
_WS_BACKOFF_MAX  = 30.0   # reconnect backoff ceiling

_PRODUCT_TO_ASSET = {v: k for k, v in _COINBASE_PRODUCTS.items()}


def _handle_ticker_message(data: dict, now_ts: float | None = None) -> str | None:
    """
    Apply one parsed ws ticker message to the price deques, decimated to
    _DECIMATE_SECS. Returns the asset appended, or None if ignored/deduped.
    Pure function of (message, clock) so the parsing/decimation is testable
    without a socket.
    """
    if data.get("type") != "ticker":
        return None
    asset = _PRODUCT_TO_ASSET.get(data.get("product_id"))
    if asset is None:
        return None
    try:
        price = float(data["price"])
    except (KeyError, TypeError, ValueError):
        return None
    if price <= 0:
        return None
    ts = now_ts if now_ts is not None else time.time()
    dq = _prices[asset]
    if not dq or (ts - dq[-1][0]) >= _DECIMATE_SECS:
        dq.append((ts, price))
        return asset
    return None


async def _ws_price_task(valid_assets: list[str]) -> None:
    """
    Coinbase Exchange websocket ticker feed with reconnect + exponential backoff.
    Appends decimated ticks to the shared deques. Runs until cancelled.
    """
    import aiohttp as _aiohttp

    product_ids = [_COINBASE_PRODUCTS[a] for a in valid_assets if a in _COINBASE_PRODUCTS]
    if not product_ids:
        return
    backoff = 1.0
    while True:
        try:
            async with _aiohttp.ClientSession() as session:
                async with session.ws_connect(_COINBASE_WS_URL, heartbeat=15) as ws:
                    await ws.send_json({
                        "type": "subscribe",
                        "channels": [{"name": "ticker", "product_ids": product_ids}],
                    })
                    log.info("Coinbase ws feed connected for %s", valid_assets)
                    async for msg in ws:
                        if msg.type == _aiohttp.WSMsgType.TEXT:
                            try:
                                _handle_ticker_message(json.loads(msg.data))
                            except Exception:
                                continue
                            backoff = 1.0   # healthy stream: reset backoff
                        elif msg.type in (_aiohttp.WSMsgType.CLOSED, _aiohttp.WSMsgType.ERROR):
                            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Coinbase ws feed error: %s. Reconnecting in %.0fs", exc, backoff)
        await asyncio.sleep(backoff + random.uniform(0, backoff * 0.3))
        backoff = min(backoff * 2.0, _WS_BACKOFF_MAX)


async def _rest_sweep(session, assets: list[str]) -> None:
    """One REST poll of the given assets (Coinbase spot API), same decimation rule."""
    import aiohttp as _aiohttp

    for asset in assets:
        try:
            url = _COINBASE_SPOT_URL.format(asset=asset)
            async with session.get(url, timeout=_aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    price = float(data["data"]["amount"])
                    now_ts = time.time()
                    dq = _prices[asset]
                    if price > 0 and (not dq or (now_ts - dq[-1][0]) >= _DECIMATE_SECS):
                        dq.append((now_ts, price))
        except Exception as _e:
            log.debug(f"Coinbase REST fallback [{asset}] error: {_e}")


async def _maybe_refresh_24h_stats(session, assets: list[str]) -> None:
    """Refresh the 24h-change cache from the Coinbase stats endpoint every 300s."""
    import aiohttp as _aiohttp

    global _ch24_last_fetch
    if time.time() - _ch24_last_fetch < 300:
        return
    for asset in assets:
        product_id = _COINBASE_PRODUCTS.get(asset)
        if not product_id:
            continue
        try:
            async with session.get(
                _COINBASE_STATS_URL.format(product_id=product_id),
                timeout=_aiohttp.ClientTimeout(total=5),
            ) as sr:
                if sr.status == 200:
                    s = await sr.json()
                    open_24h = float(s.get("open", 0) or 0)
                    current  = get_price(asset)
                    if open_24h and current:
                        _ch24[asset] = round((current - open_24h) / open_24h * 100, 2)
        except Exception as _se:
            log.debug(f"ch24 stats [{asset}] error: {_se}")
    _ch24_last_fetch = time.time()


async def coinbase_price_task(enabled_assets: list[str]) -> None:
    """
    Price feed supervisor (public entry point; started from bot.py).

    Runs the websocket feed as the primary source and watchdogs it every 2s:
    any asset whose last append is older than _WS_STALE_SECS gets a REST poll
    (covers both socket outages and thinly-traded assets with no ticks), and a
    dead ws task is restarted. Also refreshes the 24h stats cache.
    """
    import aiohttp as _aiohttp

    valid_assets = [a for a in enabled_assets if a in ASSET_CONFIG]
    if not valid_assets:
        return

    log.info(f"Coinbase price feed starting for {valid_assets} (ws + REST fallback)")
    ws_task = asyncio.create_task(_ws_price_task(valid_assets))
    try:
        async with _aiohttp.ClientSession() as session:
            while True:
                try:
                    stale = [a for a in valid_assets
                             if (age := price_age_seconds(a)) is None or age > _WS_STALE_SECS]
                    if stale:
                        await _rest_sweep(session, stale)
                    if ws_task.done():
                        exc = None if ws_task.cancelled() else ws_task.exception()
                        log.warning("ws feed task exited (%s); restarting", exc)
                        ws_task = asyncio.create_task(_ws_price_task(valid_assets))
                    await _maybe_refresh_24h_stats(session, valid_assets)
                except Exception as exc:
                    log.warning(f"Price feed supervisor error: {exc}")
                await asyncio.sleep(2)
    finally:
        ws_task.cancel()
