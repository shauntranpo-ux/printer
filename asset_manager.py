"""
asset_manager.py — Asset configuration, BV3 lookup, and Binance price feeds.

Provides:
  ASSET_CONFIG        — per-asset metadata (strike increment, Kalshi series, Binance symbol)
  load_bv3_tables()   — loads JSON tables from bv3_tables/
  empirical_win_prob  — per-asset BV3 probability lookup
  bv3_bucket_indices  — (dist_idx, time_idx) for recording outcomes
  binance_feed_task() — async coroutine maintaining per-asset price deques
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BV3_DIR  = os.path.join(BASE_DIR, "bv3_tables")

BINANCE_WS = "wss://stream.binance.com:9443/stream"

# ── Asset registry ─────────────────────────────────────────────────────────────
# kalshi_series: ordered list to try (first match wins).
#   Named after the pattern observed for BTC (KXBTCD active as of 2026).
#   ETH/SOL/XRP/DOGE series names are guessed from the BTC pattern;
#   if Kalshi uses different tickers, fetch_market_for_asset() will return None
#   and the bot will silently skip that asset (logged as WARNING).
# strike_increment: rounding unit for nearest-strike computation.
ASSET_CONFIG = {
    "BTC": {
        "binance_symbol":   "btcusdt",
        "strike_increment": 1000.0,
        "kalshi_series":    ("KXBTCD", "BTCD-B"),  # hourly above/below only — no 15-min (no validated edge)
    },
    "ETH": {
        "binance_symbol":   "ethusdt",
        "strike_increment": 25.0,
        "kalshi_series":    ("KXETHD", "ETHD-B"),  # hourly above/below only — validated Mid+Dwell+Late edge
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
        "kalshi_series":    ("KXDOGED", "DOGED-B", "KXDOGE15M", "KXDOGE15", "KXDOGE", "DOGE"),
    },
}

# ── Hardcoded BTC fallback table (original, used when no JSON file is present) ─
# Rows = distance buckets, Cols = minutes remaining (1-13)
_BV3_TABLE_FALLBACK = [
    [0.850, 0.796, 0.758, 0.727, 0.705, 0.686, 0.672, 0.656, 0.639, 0.624, 0.606, 0.595, 0.578],
    [0.980, 0.956, 0.931, 0.904, 0.876, 0.856, 0.833, 0.807, 0.783, 0.752, 0.733, 0.706, 0.675],
    [0.994, 0.983, 0.967, 0.951, 0.933, 0.909, 0.889, 0.868, 0.835, 0.811, 0.788, 0.756, 0.713],
    [0.997, 0.990, 0.981, 0.968, 0.950, 0.935, 0.917, 0.893, 0.874, 0.840, 0.816, 0.778, 0.741],
    [0.998, 0.993, 0.987, 0.977, 0.962, 0.948, 0.932, 0.908, 0.883, 0.869, 0.835, 0.809, 0.782],
    [0.998, 0.997, 0.988, 0.979, 0.968, 0.960, 0.944, 0.925, 0.913, 0.876, 0.849, 0.824, 0.781],
    [0.999, 0.994, 0.994, 0.979, 0.974, 0.963, 0.947, 0.936, 0.914, 0.897, 0.872, 0.839, 0.817],
    [0.999, 0.996, 0.995, 0.988, 0.982, 0.968, 0.963, 0.942, 0.917, 0.905, 0.884, 0.845, 0.818],
    [1.000, 0.999, 0.994, 0.992, 0.984, 0.980, 0.967, 0.964, 0.935, 0.919, 0.911, 0.862, 0.820],
    [1.000, 0.997, 0.995, 0.991, 0.986, 0.972, 0.971, 0.960, 0.942, 0.921, 0.904, 0.874, 0.820],
]
_BV3_DIST_BOUNDS_FALLBACK = [0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.0075, 0.010, 0.0125]

# ── Per-asset BV3 tables (populated by load_bv3_tables) ───────────────────────
# _bv3[asset] = {"table": [...], "dist_bounds": [...], "loaded": bool, "label": str}
_bv3: dict[str, dict] = {}

# ── Per-asset price deques ─────────────────────────────────────────────────────
# asset → deque[(unix_ts, price)]
_prices: dict[str, deque] = {asset: deque(maxlen=500) for asset in ASSET_CONFIG}


# ═══════════════════════════════ BV3 loading ═══════════════════════════════════

def _fallback_entry() -> dict:
    return {
        "table":       _BV3_TABLE_FALLBACK,
        "dist_bounds": _BV3_DIST_BOUNDS_FALLBACK,
        "loaded":      False,
        "label":       "fallback",
    }


def load_bv3_tables(use_pre2023: bool = False) -> None:
    """
    Load BV3 JSON tables from bv3_tables/ for all known assets.

    use_pre2023=True  → load *_bv3_pre2023.json (for backtesting)
    use_pre2023=False → load *_bv3_full.json     (for live trading)

    Falls back to the hardcoded BTC table if no JSON file exists for an asset.
    """
    suffix = "pre2023" if use_pre2023 else "full"
    for asset in ASSET_CONFIG:
        path = os.path.join(BV3_DIR, f"{asset}_bv3_{suffix}.json")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                _bv3[asset] = {
                    "table":       data["table"],
                    "dist_bounds": data["dist_bounds"],
                    "loaded":      True,
                    "label":       data.get("label", suffix),
                }
                meta = data.get("metadata", {})
                log.info(
                    f"BV3 [{asset}] loaded ({meta.get('total_windows', '?'):,} windows, "
                    f"label={data.get('label', suffix)}, path={path})"
                )
            except Exception as exc:
                log.warning(f"BV3 [{asset}] failed to load {path}: {exc} — using fallback")
                _bv3[asset] = _fallback_entry()
        else:
            if asset == "BTC":
                log.warning(f"BV3 [BTC] not found at {path} — using hardcoded fallback")
            else:
                log.info(
                    f"BV3 [{asset}] no table at {path} — using BTC fallback "
                    f"(volatility may differ; generate with: python generate_bv3_table.py --asset {asset})"
                )
            _bv3[asset] = _fallback_entry()


# ═══════════════════════════════ BV3 lookup ════════════════════════════════════

def empirical_win_prob(asset: str, abs_pct: float, mins_left: float) -> float:
    """
    Return empirical P(price stays on current side of strike at window close).

    asset:    "BTC", "ETH", etc.
    abs_pct:  absolute fraction distance from strike (0.003 = 0.3%)
    mins_left: minutes until window closes
    """
    entry       = _bv3.get(asset) or _fallback_entry()
    table       = entry["table"]
    dist_bounds = entry["dist_bounds"]
    n_rows      = len(table)

    # Distance bucket
    bidx = n_rows - 1
    for i, bound in enumerate(dist_bounds):
        if abs_pct < bound:
            bidx = i
            break
    bidx = min(bidx, n_rows - 1)
    row  = table[bidx]

    # Sub-1-min: nearly certain (just above the 1-min value)
    if mins_left < 1.0:
        return min(0.997, row[0] + 0.005)

    n_cols = len(row)
    if mins_left >= n_cols:
        return row[-1]

    t_low  = int(mins_left) - 1
    t_high = t_low + 1
    frac   = mins_left - int(mins_left)

    if t_high >= n_cols:
        return row[-1]

    return row[t_low] + (row[t_high] - row[t_low]) * frac


def bv3_bucket_indices(asset: str, abs_pct: float, mins_left: float) -> tuple[int, int]:
    """Return (dist_idx, time_idx) for recording trade outcomes in correction table."""
    entry       = _bv3.get(asset) or _fallback_entry()
    dist_bounds = entry["dist_bounds"]
    n_rows      = len(entry["table"])
    n_cols      = len(entry["table"][0]) if entry["table"] else 13

    bidx = n_rows - 1
    for i, bound in enumerate(dist_bounds):
        if abs_pct < bound:
            bidx = i
            break
    bidx  = min(bidx, n_rows - 1)
    t_low = max(0, min(n_cols - 1, int(max(1.0, mins_left)) - 1))
    return bidx, t_low


# ═══════════════════════════════ Price feeds ═══════════════════════════════════

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


# Coinbase public REST URL for spot prices — no auth, works globally
_COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/{asset}-USD/spot"

# Consecutive Binance-451 counter — switch to Coinbase fallback after this many
_binance_451_streak: int = 0
_BINANCE_451_THRESHOLD = 5   # switch after 5 consecutive 451s


async def coinbase_price_task(enabled_assets: list[str]) -> None:
    """
    Fallback REST-polling price feed using Coinbase public spot API.
    Runs in parallel with binance_feed_task.  Only writes a price when
    the asset has no Binance tick in the last 5 seconds, so Binance data
    takes priority when it is working.
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
                            # Only fill in when Binance has been silent ≥5s
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
    WebSocket stream (aggTrade).  Reconnects automatically on any error.

    Uses a single combined connection for efficiency:
      wss://stream.binance.com:9443/stream?streams=btcusdt@aggTrade/ethusdt@aggTrade/...

    On environments where Binance is geo-blocked (HTTP 451), the coinbase_price_task
    coroutine provides prices as a fallback — start both tasks in parallel.
    """
    global _binance_451_streak

    # Build stream list
    valid_assets = [a for a in enabled_assets if a in ASSET_CONFIG]
    if not valid_assets:
        log.warning("binance_feed_task: no valid assets — price feed not started")
        return

    streams = "/".join(
        f"{ASSET_CONFIG[a]['binance_symbol']}@aggTrade"
        for a in valid_assets
    )
    url = f"{BINANCE_WS}?streams={streams}"

    # Map "BTCUSDT" → "BTC" for fast lookup
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
                        sym   = data.get("s", "").upper()   # e.g. "BTCUSDT"
                        asset = sym_to_asset.get(sym)
                        if asset and "p" in data:
                            price  = float(data["p"])
                            now_ts = time.time()
                            dq     = _prices[asset]
                            # Throttle: store at most one tick per second per asset
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
                await asyncio.sleep(10)   # back off longer on geo-block
            else:
                log.error(f"Binance feed disconnected ({exc}). Reconnecting in 3s...")
                _binance_451_streak = 0
                await asyncio.sleep(3)
