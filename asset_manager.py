"""
asset_manager.py — Asset configuration and Binance price feeds.

Provides:
  ASSET_CONFIG        — per-asset metadata (strike increment, Kalshi series, Binance symbol)
  binance_feed_task() — async coroutine maintaining per-asset price deques
  coinbase_price_task() — fallback REST price feed (Coinbase)
  get_price(asset)    — latest price for an asset
  price_age_seconds   — seconds since last update
"""

import asyncio
import json
import logging
import os
import time
from collections import deque

import websockets

log = logging.getLogger("bot")

BINANCE_WS = "wss://stream.binance.com:9443/stream"

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
}

# ── Per-asset price deques ────────────────────────────────────────────────────
# asset → deque[(unix_ts, price)]
_prices: dict[str, deque] = {asset: deque(maxlen=500) for asset in ASSET_CONFIG}


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

# Consecutive Binance-451 counter — switch to Coinbase fallback after this many
_binance_451_streak: int = 0
_BINANCE_451_THRESHOLD = 5


_BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
_KRAKEN_OHLC_URL   = "https://api.kraken.com/0/public/OHLC"
_KRAKEN_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "XRP": "XRPUSD"}


async def seed_price_history(assets: list[str]) -> None:
    """
    Pre-fill _prices deques with 30 historical 1-minute bars so Supertrend
    has enough data immediately after a cold start / crash restart.
    Tries Binance REST first; falls back to Kraken public OHLC.
    """
    import aiohttp as _aiohttp

    for asset in assets:
        dq = _prices.get(asset)
        if dq is None:
            continue

        cfg = ASSET_CONFIG.get(asset, {})
        sym = cfg.get("binance_symbol", "").upper()
        seeded = False

        # ── Binance REST klines ────────────────────────────────────────────
        if sym:
            try:
                async with _aiohttp.ClientSession() as s:
                    async with s.get(
                        _BINANCE_KLINES_URL,
                        params={"symbol": sym, "interval": "1m", "limit": "30"},
                        timeout=_aiohttp.ClientTimeout(total=6),
                    ) as resp:
                        if resp.status == 200:
                            klines = await resp.json()
                            for k in klines:
                                # k[6] = close_time (ms), k[4] = close price
                                dq.append((int(k[6]) / 1000.0, float(k[4])))
                            log.info(
                                "[%s] seed_price_history: %d bars from Binance REST",
                                asset, len(klines),
                            )
                            seeded = True
            except Exception as exc:
                log.debug("[%s] Binance klines seed failed: %s", asset, exc)

        # ── Kraken OHLC fallback ───────────────────────────────────────────
        if not seeded:
            pair = _KRAKEN_PAIRS.get(asset)
            if pair:
                try:
                    async with _aiohttp.ClientSession() as s:
                        async with s.get(
                            _KRAKEN_OHLC_URL,
                            params={"pair": pair, "interval": "1"},
                            timeout=_aiohttp.ClientTimeout(total=6),
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                result = data.get("result", {})
                                bars = next(
                                    (v for k, v in result.items()
                                     if k != "last" and isinstance(v, list)),
                                    [],
                                )
                                for bar in bars[-30:]:
                                    dq.append((float(bar[0]), float(bar[4])))
                                log.info(
                                    "[%s] seed_price_history: %d bars from Kraken",
                                    asset, min(30, len(bars)),
                                )
                                seeded = True
                except Exception as exc:
                    log.debug("[%s] Kraken OHLC seed failed: %s", asset, exc)

        if not seeded:
            log.warning("[%s] seed_price_history: both REST sources failed", asset)


async def coinbase_price_task(enabled_assets: list[str]) -> None:
    """
    Fallback REST-polling price feed using Coinbase public spot API.
    Runs in parallel with binance_feed_task. Only writes a price when
    the asset has no Binance tick in the last 5 seconds.
    """
    import aiohttp as _aiohttp

    valid_assets = [a for a in enabled_assets if a in ASSET_CONFIG]
    if not valid_assets:
        return

    log.info(f"Coinbase fallback feed starting for {valid_assets}")
    while True:
        try:
            async with _aiohttp.ClientSession() as session:
                while True:
                    for asset in valid_assets:
                        try:
                            age = price_age_seconds(asset)
                            if age is not None and age < 5:
                                continue
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
                                        if age is None or age > 10:
                                            log.info(
                                                f"Coinbase fallback [{asset}] "
                                                f"${price:,.2f}"
                                            )
                        except Exception as _e:
                            log.debug(f"Coinbase fallback [{asset}] error: {_e}")
                    await asyncio.sleep(2)
        except Exception as exc:
            log.warning(f"Coinbase fallback session error: {exc}. Retrying in 5s...")
            await asyncio.sleep(5)


async def binance_feed_task(enabled_assets: list[str]) -> None:
    """
    Maintain real-time price feeds for all enabled assets via Binance combined
    WebSocket stream (aggTrade). Reconnects automatically on any error.

    On environments where Binance is geo-blocked (HTTP 451), the coinbase_price_task
    coroutine provides prices as a fallback — start both tasks in parallel.
    """
    global _binance_451_streak

    valid_assets = [a for a in enabled_assets if a in ASSET_CONFIG]
    if not valid_assets:
        log.warning("binance_feed_task: no valid assets — price feed not started")
        return

    streams = "/".join(
        f"{ASSET_CONFIG[a]['binance_symbol']}@aggTrade"
        for a in valid_assets
    )
    url = f"{BINANCE_WS}?streams={streams}"

    sym_to_asset = {
        ASSET_CONFIG[a]["binance_symbol"].upper(): a
        for a in valid_assets
    }

    while True:
        try:
            async with websockets.connect(
                url, ping_interval=20, ping_timeout=10, open_timeout=15
            ) as ws:
                _binance_451_streak = 0
                log.info(f"Binance feed connected for {valid_assets}")
                async for raw in ws:
                    try:
                        msg   = json.loads(raw)
                        data  = msg.get("data", {})
                        sym   = data.get("s", "").upper()
                        asset = sym_to_asset.get(sym)
                        if asset and "p" in data:
                            price  = float(data["p"])
                            now_ts = time.time()
                            dq     = _prices[asset]
                            if not dq or (now_ts - dq[-1][0]) >= 1.0:
                                dq.append((now_ts, price))
                    except Exception as parse_exc:
                        log.debug(f"Binance feed parse error: {parse_exc}")
        except Exception as exc:
            err_str = str(exc)
            if "451" in err_str:
                _binance_451_streak += 1
                if _binance_451_streak == 1:
                    log.warning(
                        "Binance feed geo-blocked (HTTP 451). "
                        "coinbase_price_task will supply prices — trading continues."
                    )
                elif _binance_451_streak % 20 == 0:
                    log.debug(f"Binance 451 streak: {_binance_451_streak}")
                await asyncio.sleep(10)
            else:
                log.error(f"Binance feed disconnected ({exc}). Reconnecting in 3s...")
                _binance_451_streak = 0
                await asyncio.sleep(3)
