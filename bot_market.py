"""bot_market.py — Kalshi API layer: auth, market data, order placement, contract math.

Public interface (see __all__):
  Auth:    load_credentials, kalshi_headers
  Market:  get_btc_price, fetch_current_market, fetch_market_for_asset,
           fetch_orderbook, parse_strike, seconds_remaining, seconds_elapsed
  Orders:  place_order, _verify_order_fill, _portfolio_has_position,
           calculate_contracts, implied_prob
"""
import asyncio
import logging
import math
import os
import re
import sys
import time
from base64 import b64encode
from datetime import datetime, timezone

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import bot_state
from asset_manager import (
    ASSET_CONFIG,
    get_price as _am_get_price,
)
from bot_infra import (
    read_config, write_config,
    _maybe_fill_verification_notify, _phase_for_eth, _notify_ctx,
)
from kalshi_compat import dollars_to_cents, extract_order_counts, extract_fill_price_cents

log = logging.getLogger("bot")


def _read_price_cents(market_or_book: dict, key_base: str) -> "int | None":
    """Read a price field in cents, preferring *_dollars string over legacy int.

    key_base examples: "yes_ask", "no_ask", "yes_bid", "no_bid", "last_price".
    Tries {key_base}_dollars first (dollars_to_cents), falls back to {key_base} int.
    """
    dollars_val = market_or_book.get(f"{key_base}_dollars")
    if dollars_val is not None:
        return dollars_to_cents(dollars_val)
    int_val = market_or_book.get(key_base)
    if int_val is not None:
        try:
            v = int(round(float(int_val) * 100)) if isinstance(int_val, float) else int(int_val)
            return v if v >= 0 else None
        except (TypeError, ValueError):
            return None
    return None


__all__ = [
    "load_credentials", "kalshi_headers",
    "get_btc_price", "fetch_current_market", "fetch_market_for_asset",
    "fetch_orderbook", "parse_strike", "seconds_remaining", "seconds_elapsed",
    "place_order", "_verify_order_fill", "_portfolio_has_position",
    "calculate_contracts", "implied_prob",
]


# ---------------------------------------------------------------------------
# Kalshi API — auth, market data, price helpers (from bot_kalshi)
# ---------------------------------------------------------------------------


def load_credentials(mode: str = "paper") -> None:
    """
    Load Kalshi API credentials from environment variables based on active mode.

    paper — skips credential loading (no API calls needed)
    live  — loads KALSHI_API_KEY + KALSHI_PRIVATE_KEY, routes to live endpoint
    demo  — loads KALSHI_DEMO_API_KEY + KALSHI_DEMO_PRIVATE_KEY, routes to demo endpoint

    Credential values may be a PEM string or a path to a PEM file.
    Exits with a clear error message if required variables are missing.
    """

    if mode == "paper":
        _key = os.environ.get("KALSHI_API_KEY", "").strip()
        _pem = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
        if _key and _pem:
            bot_state.api_key = _key
            try:
                pem_bytes = open(_pem, "rb").read() if os.path.exists(_pem) else _pem.encode()
                bot_state.private_key = serialization.load_pem_private_key(pem_bytes, password=None)
                log.info("Paper mode: Kalshi credentials loaded for market data access.")
            except Exception as exc:
                log.warning(f"Paper mode: credential load failed ({exc}) — market data unavailable.")
        return

    if mode == "demo":
        bot_state.KALSHI_BASE_URL = bot_state.KALSHI_DEMO_BASE_URL
        key_id_var = "KALSHI_DEMO_API_KEY"
        pem_var    = "KALSHI_DEMO_PRIVATE_KEY"
        label      = "DEMO"
    else:  # live
        bot_state.KALSHI_BASE_URL = bot_state.KALSHI_LIVE_BASE_URL
        key_id_var = "KALSHI_API_KEY"
        pem_var    = "KALSHI_PRIVATE_KEY"
        label      = "LIVE"

    bot_state.api_key = os.environ.get(key_id_var, "").strip()
    pem_val = os.environ.get(pem_var, "").strip()

    if not bot_state.api_key or not pem_val:
        missing = key_id_var if not bot_state.api_key else pem_var
        if mode == "demo":
            log.warning(
                f"{missing} not set — DEMO mode requires demo credentials. "
                f"Falling back to paper mode. Add the env vars in Railway to enable demo."
            )
            try:
                cfg = read_config()
                cfg["mode"] = "paper"
                write_config(cfg)
            except Exception as _ce:
                log.warning(f"Could not write paper fallback to config: {_ce}")
            _key = os.environ.get("KALSHI_API_KEY", "").strip()
            _pem = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
            if _key and _pem:
                bot_state.api_key = _key
                try:
                    pem_bytes = open(_pem, "rb").read() if os.path.exists(_pem) else _pem.encode()
                    bot_state.private_key = serialization.load_pem_private_key(pem_bytes, password=None)
                    log.info("Paper fallback: loaded live credentials for market data access.")
                except Exception as exc:
                    log.warning(f"Paper fallback: live credential load failed ({exc}).")
            bot_state.KALSHI_BASE_URL = bot_state.KALSHI_LIVE_BASE_URL
            return
        else:
            print(f"ERROR: {missing} is not set (required for {label} mode).")
            sys.exit(1)

    if mode == "demo" and bot_state.KALSHI_BASE_URL != bot_state.KALSHI_DEMO_BASE_URL:
        print(f"SAFETY ERROR: demo mode must use demo URL; got {bot_state.KALSHI_BASE_URL}")
        sys.exit(1)
    if mode == "live" and bot_state.KALSHI_BASE_URL != bot_state.KALSHI_LIVE_BASE_URL:
        print(f"SAFETY ERROR: live mode must use live URL; got {bot_state.KALSHI_BASE_URL}")
        sys.exit(1)

    if os.path.exists(pem_val):
        with open(pem_val, "rb") as fh:
            pem_bytes = fh.read()
        log.info(f"Loaded {label} private key from file: {pem_val}")
    else:
        pem_bytes = pem_val.encode()
        log.info(f"Loaded {label} private key from environment variable string.")

    bot_state.private_key = serialization.load_pem_private_key(pem_bytes, password=None)

    masked = bot_state.api_key[:6] + "..." if len(bot_state.api_key) > 6 else "***"
    print(f"[{label} MODE] Base URL : {bot_state.KALSHI_BASE_URL}")
    print(f"[{label} MODE] API key  : {masked}")
    log.info(f"{label} credentials loaded successfully.")


def kalshi_headers(method: str, path: str) -> dict:
    """
    Generate the three Kalshi authentication headers for one request.

    Args:
        method: HTTP method in uppercase (e.g. 'GET', 'POST').
        path:   URL path without the base URL (e.g. '/markets').

    Returns:
        Dict of header name -> value.
    """
    ts = str(int(time.time() * 1000))
    full_path = bot_state.KALSHI_PATH_PREFIX + path
    msg = (ts + method.upper() + full_path).encode()
    sig = b64encode(
        bot_state.private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
    ).decode()
    return {
        "KALSHI-ACCESS-KEY": bot_state.api_key,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type": "application/json",
    }


def get_btc_price() -> float | None:
    """Return the most recent BTC price, or None if no data received yet."""
    return _am_get_price("BTC")


async def fetch_current_market(session: aiohttp.ClientSession, return_all: bool = False) -> dict | None | list:
    """
    Fetch open BTC 15-minute market(s) from Kalshi.
    Results are cached for bot_state.MARKET_CACHE_TTL seconds to avoid hammering the API.

    Args:
        return_all: if True, return the full sorted list of valid markets.
                    if False (default), return only the soonest-expiring one.

    Returns:
        Market dict (or None) when return_all=False.
        List of market dicts (possibly empty) when return_all=True.
    """

    now = time.time()
    if bot_state._market_cache and (now - bot_state._market_cache_ts) < bot_state.MARKET_CACHE_TTL:
        return bot_state._all_markets_cache if return_all else bot_state._market_cache

    path = "/markets"
    _SERIES_SEARCH_ORDER = ("KXBTC15M", "KXBTCD", "BTCD-B")

    all_markets = []
    seen_tickers: set[str] = set()
    for series in _SERIES_SEARCH_ORDER:
        params = {"series_ticker": series, "status": "open", "limit": 20}
        try:
            async with session.get(
                bot_state.KALSHI_BASE_URL + path,
                headers=kalshi_headers("GET", path),
                params=params,
                timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.error(f"Market fetch HTTP {resp.status} (series={series}): {body[:300]}")
                    continue
                data = await resp.json()
            batch = data.get("markets", [])
            new_count = 0
            for m in batch:
                t = m.get("ticker", "")
                if t and t not in seen_tickers:
                    seen_tickers.add(t)
                    all_markets.append(m)
                    new_count += 1
            if new_count:
                log.info(f"Series {series!r} returned {new_count} new markets: "
                         + ", ".join(m.get("ticker", "?") for m in batch[:5]))
        except Exception as exc:
            log.error(f"Market fetch error (series={series}): {exc}")

    if not all_markets:
        log.warning("All series tickers returned no markets.")
        return bot_state._all_markets_cache if return_all else bot_state._market_cache

    now_utc = datetime.now(timezone.utc)
    for m in all_markets:
        try:
            close_dt = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            mins_left = (close_dt - now_utc).total_seconds() / 60
        except Exception:
            mins_left = -1
        log.info(f"  Market: {m.get('ticker')} | closes in {mins_left:.1f}m | {m.get('title','')[:60]}")

    all_markets = [m for m in all_markets
                   if "range" not in m.get("title", "").lower()
                   and "daily" not in m.get("title", "").lower()]

    if not all_markets:
        log.warning("No valid short-duration markets after title filtering. Waiting for next window.")
        return [] if return_all else None

    def market_duration_minutes(m):
        try:
            open_str  = m.get("open_time") or m.get("open_date")
            close_str = m.get("close_time")
            if not open_str or not close_str:
                return None
            open_dt  = datetime.fromisoformat(open_str.replace("Z", "+00:00"))
            close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            return (close_dt - open_dt).total_seconds() / 60
        except Exception:
            return None

    short_dur = [m for m in all_markets
                 if (lambda d: d is not None and 1 <= d <= 20)(market_duration_minutes(m))]

    if short_dur:
        log.info(f"Found {len(short_dur)} short-duration market(s). "
                 + " | ".join(f"{m.get('ticker')} {market_duration_minutes(m):.0f}m" for m in short_dur[:5]))
        pool = short_dur
    else:
        soon = [
            m for m in all_markets
            if (lambda c: 0 < c <= 60)(
                (datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")) - now_utc).total_seconds() / 60
                if m.get("close_time") else -1
            )
        ]
        if soon:
            log.info(f"No short-duration match by open->close -- using {len(soon)} markets closing within 60 min.")
            pool = soon
        else:
            log.warning("No short-duration markets found. Waiting for next window.")
            return [] if return_all else None

    pool.sort(key=lambda m: m.get("close_time", ""))

    def _is_open(m):
        ct = m.get("close_time")
        if not ct:
            return True
        try:
            return datetime.fromisoformat(ct.replace("Z", "+00:00")) > now_utc
        except Exception:
            return True

    open_pool = [m for m in pool if _is_open(m)]
    if open_pool:
        pool = open_pool

    durations = [market_duration_minutes(m) for m in pool]
    valid_durations = [d for d in durations if d is not None]
    if valid_durations:
        min_dur = min(valid_durations)
        focused = [m for m, d in zip(pool, durations) if d is not None and d <= min_dur + 5]
        if focused and len(focused) < len(pool):
            log.info(f"Focusing pool from {len(pool)} to {len(focused)} markets "
                     f"(duration <= {min_dur + 5:.0f}m, dropping {len(pool) - len(focused)} longer-duration markets)")
            pool = focused

    bot_state._market_cache    = pool[0]
    bot_state._market_cache_ts = now
    bot_state._all_markets_cache    = pool
    bot_state._all_markets_cache_ts = now
    log.info(
        f"Active market: {bot_state._market_cache.get('ticker')} | {bot_state._market_cache.get('title')} "
        f"| closes {bot_state._market_cache.get('close_time')} | ({len(pool)} window(s) total)"
    )
    return pool if return_all else bot_state._market_cache


async def fetch_market_for_asset(session: aiohttp.ClientSession, asset: str) -> dict | None:
    """
    Fetch the soonest-expiring open market for the given non-BTC asset.
    Uses the kalshi_series priority list from ASSET_CONFIG.
    Accepts windows up to 20 minutes (15-min markets only).
    Returns None if no suitable market found.
    """
    series_list = ASSET_CONFIG.get(asset, {}).get("kalshi_series", ())
    path = "/markets"
    all_markets: list[dict] = []
    seen_tickers: set[str] = set()
    for series in series_list:
        params = {"series_ticker": series, "status": "open", "limit": 20}
        try:
            async with session.get(
                bot_state.KALSHI_BASE_URL + path,
                headers=kalshi_headers("GET", path),
                params=params,
                timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
            for m in data.get("markets", []):
                t = m.get("ticker", "")
                if t and t not in seen_tickers:
                    seen_tickers.add(t)
                    all_markets.append(m)
        except Exception as exc:
            log.warning(f"fetch_market_for_asset [{asset}] series={series}: {exc}")

    if not all_markets:
        log.debug(f"fetch_market_for_asset [{asset}]: no markets found")
        return None

    all_markets = [m for m in all_markets
                   if "range" not in m.get("title", "").lower()
                   and "daily" not in m.get("title", "").lower()]
    if not all_markets:
        return None

    now_utc = datetime.now(timezone.utc)
    def secs_to_close(m: dict) -> float:
        try:
            return (datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")) - now_utc).total_seconds()
        except Exception:
            return -1.0

    valid = [m for m in all_markets if 0 < secs_to_close(m) < 20 * 60]
    if not valid:
        return None

    valid.sort(key=secs_to_close)
    chosen = valid[0]
    log.debug(f"fetch_market_for_asset [{asset}]: {chosen.get('ticker')} ({secs_to_close(chosen):.0f}s left)")
    return chosen


def parse_strike(market: dict) -> float | None:
    """
    Extract the strike price from market data.

    Priority:
      1. Structured fields: floor_strike, cap_strike, strike_price
      2. Regex search in title/subtitle for $XX,XXX or $XXX,XXX

    Returns:
        Strike as float, or None if unparseable (market will be skipped).
    """
    for field in ("floor_strike", "cap_strike", "strike_price"):
        val = market.get(field)
        if val is not None:
            try:
                strike = float(val)
                log.info(f"Strike parsed from field '{field}': {strike}")
                return strike
            except (ValueError, TypeError):
                pass

    text = (market.get("title", "") + " " + market.get("subtitle", "")
            + " " + (market.get("yes_sub_title") or ""))
    match = re.search(r"\$(\d{1,3}(?:,\d{3})*(?:\.\d+)?)", text)
    if match:
        strike = float(match.group(1).replace(",", ""))
        log.info(f"Strike parsed from title regex: {strike} (text: {text[:80]})")
        return strike

    yes_sub = market.get("yes_sub_title") or ""
    if "TBD" in yes_sub:
        log.debug(f"Cannot parse strike (TBD): {market.get('ticker')}")
    else:
        _diag = {k: market.get(k) for k in ('ticker', 'title', 'subtitle', 'floor_strike', 'cap_strike', 'strike_price', 'result', 'yes_sub_title', 'no_sub_title')}
        log.warning(f"Cannot parse strike. Full market fields: {_diag}")
    return None


def seconds_remaining(market: dict) -> float:
    """Seconds until the market closes. Returns 0 if already expired."""
    close_str = market.get("close_time", "")
    try:
        close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
        remaining = (close_dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, remaining)
    except Exception as exc:
        log.error(f"close_time parse error ({close_str!r}): {exc}")
        return 0.0


def seconds_elapsed(market: dict) -> float:
    """Estimate seconds since market open, using the market's actual duration."""
    try:
        open_str  = market.get("open_time") or market.get("open_date")
        close_str = market.get("close_time")
        if open_str and close_str:
            open_dt  = datetime.fromisoformat(open_str.replace("Z", "+00:00"))
            close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            duration_secs = (close_dt - open_dt).total_seconds()
            return max(0.0, duration_secs - seconds_remaining(market))
    except Exception:
        pass
    return max(0.0, 15 * 60 - seconds_remaining(market))


def _kalshi_obi(yes_arr: list, no_arr: list, top_n: int = 5) -> float | None:
    yes_asks = [(p, q) for p, q in yes_arr if q > 0 and p > 0]
    no_asks  = [(p, q) for p, q in no_arr  if q > 0 and p > 0]
    yes_depth = sum(q for _, q in sorted(yes_asks, key=lambda x: x[0])[:top_n])
    no_depth  = sum(q for _, q in sorted(no_asks,  key=lambda x: x[0])[:top_n])
    total = yes_depth + no_depth
    if total < 1e-9:
        return None
    return (no_depth - yes_depth) / total


async def fetch_orderbook(
    session: aiohttp.ClientSession,
    ticker: str,
    market: dict | None = None,
) -> dict | None:
    """
    Fetch live prices for one market ticker.

    For AMM markets (KXBTC15M) the /orderbook endpoint returns empty arrays.
    We call GET /markets/{ticker} directly for fresh AMM prices.

    Returns:
        Dict with best_yes_ask, best_no_ask, best_yes_bid (all cents), yes_liquidity,
        or None if no price data is available. best_yes_bid may be None.
    """
    ob_path = f"/markets/{ticker}/orderbook"
    try:
        async with session.get(
            bot_state.KALSHI_BASE_URL + ob_path,
            headers=kalshi_headers("GET", ob_path),
            timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.error(f"Orderbook fetch HTTP {resp.status} for {ticker}: {body[:200]}")
                ob_data = {}
            else:
                ob_data = await resp.json()
    except Exception as exc:
        log.error(f"Orderbook fetch error for {ticker}: {exc}")
        ob_data = {}

    ob = ob_data.get("orderbook", {})
    yes_arr = ob.get("yes", [])
    no_arr  = ob.get("no",  [])
    yes_asks = [(p, q) for p, q in yes_arr if q > 0 and p > 0]
    no_asks  = [(p, q) for p, q in no_arr  if q > 0 and p > 0]

    best_yes_ask = min(p for p, _ in yes_asks) if yes_asks else None
    best_no_ask  = min(p for p, _ in no_asks)  if no_asks  else None
    best_yes_bid = (100 - max(p for p, _ in no_asks)) if no_asks else None

    if best_yes_ask is None or best_no_ask is None:
        fresh_market: dict = {}
        mkt_path = f"/markets/{ticker}"
        try:
            async with session.get(
                bot_state.KALSHI_BASE_URL + mkt_path,
                headers=kalshi_headers("GET", mkt_path),
                timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
            ) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    fresh_market = body.get("market", {})
                else:
                    body_txt = await resp.text()
                    log.error(f"Market fetch HTTP {resp.status} for {ticker}: {body_txt[:200]}")
        except Exception as exc:
            log.error(f"Market fetch error for {ticker}: {exc}")
            fresh_market = market or {}

        src = fresh_market if fresh_market else (market or {})

        if best_yes_ask is None:
            best_yes_ask = _read_price_cents(src, "yes_ask")
        if best_yes_ask is None:
            no_bid = _read_price_cents(src, "no_bid")
            if no_bid is not None:
                _derived = 100 - no_bid
                if _derived > 0:
                    best_yes_ask = _derived

        if best_no_ask is None:
            best_no_ask = _read_price_cents(src, "no_ask")
        if best_no_ask is None:
            yes_bid = _read_price_cents(src, "yes_bid")
            if yes_bid is not None:
                _derived = 100 - yes_bid
                if _derived > 0:
                    best_no_ask = _derived

        if best_yes_bid is None:
            best_yes_bid = _read_price_cents(src, "yes_bid")
        if best_yes_bid is None:
            no_ask_raw = _read_price_cents(src, "no_ask")
            if no_ask_raw is not None:
                _derived = 100 - no_ask_raw
                if _derived >= 0:
                    best_yes_bid = _derived

        if best_yes_ask is not None or best_no_ask is not None:
            log.info(
                f"AMM prices for {ticker}: "
                f"yes_ask={best_yes_ask}c  no_ask={best_no_ask}c  yes_bid={best_yes_bid}c"
            )

    if best_yes_ask is None or best_no_ask is None:
        _diag_keys = ("yes_ask_dollars", "no_ask_dollars", "yes_bid_dollars", "no_bid_dollars",
                      "last_price", "status", "volume", "liquidity")
        _diag = {k: (src.get(k) if isinstance(src, dict) else None) for k in _diag_keys}
        log.warning(
            f"No price data available for {ticker} "
            f"(yes_ask={best_yes_ask} no_ask={best_no_ask}). "
            f"Raw market fields: {_diag}"
        )
        return None

    if not (0 <= best_yes_ask <= 100 and 0 <= best_no_ask <= 100):
        log.warning(
            f"Orderbook prices out of range for {ticker}: "
            f"yes_ask={best_yes_ask}c no_ask={best_no_ask}c -- skipping"
        )
        return None
    if best_yes_ask == 100 and best_no_ask == 100:
        log.debug(f"Both sides at ceiling for {ticker} -- market not ready yet")
        return None
    if best_yes_ask + best_no_ask < 100:
        log.warning(
            f"Orderbook sum below 100 for {ticker}: "
            f"yes_ask({best_yes_ask}c) + no_ask({best_no_ask}c) = {best_yes_ask+best_no_ask}c -- skipping"
        )
        return None
    if best_yes_ask + best_no_ask > 150:
        log.warning(
            f"Orderbook sum very wide for {ticker}: "
            f"yes_ask({best_yes_ask}c) + no_ask({best_no_ask}c) = {best_yes_ask+best_no_ask}c -- thin market, passing through"
        )

    if yes_asks:
        yes_liquidity = sum(q for p, q in yes_asks if p <= best_yes_ask)
    else:
        try:
            yes_liquidity = int(float(market.get("yes_ask_size_fp", 500))) if market else 500
        except (TypeError, ValueError):
            yes_liquidity = 500

    if no_asks:
        no_liquidity = sum(q for p, q in no_asks if p <= best_no_ask)
    else:
        try:
            no_liquidity = int(float(market.get("no_ask_size_fp", 500))) if market else 500
        except (TypeError, ValueError):
            no_liquidity = 500

    return {
        "best_yes_ask": best_yes_ask,
        "best_no_ask":  best_no_ask,
        "best_yes_bid": best_yes_bid,
        "yes_liquidity": yes_liquidity,
        "no_liquidity":  no_liquidity,
        "obi":           _kalshi_obi(yes_arr, no_arr),
    }


# ---------------------------------------------------------------------------
# Order placement and contract math (from bot_orders)
# ---------------------------------------------------------------------------

def calculate_contracts(
    trade_amount_dollars: float,
    entry_price_cents: int,
    liquidity: int,
) -> tuple[int, float]:
    """
    Fixed position sizing — always spend exactly trade_amount_dollars.

    Returns:
        (contracts, dollars_used)
    """
    if entry_price_cents <= 0:
        return 0, 0.0

    price_dollars = entry_price_cents / 100.0

    def _taker_fee(n: int) -> float:
        raw = 0.07 * n * price_dollars * (1.0 - price_dollars)
        return math.ceil(raw * 100) / 100.0

    contracts = int(trade_amount_dollars * 100 / entry_price_cents)
    contracts = min(contracts, liquidity)
    contracts = max(contracts, 0)

    while contracts > 0 and contracts * price_dollars + _taker_fee(contracts) > trade_amount_dollars:
        contracts -= 1

    dollars_used = contracts * price_dollars

    log.info(
        f"Fixed sizing: price={entry_price_cents}c "
        f"bet=${dollars_used:.2f} fee=${_taker_fee(contracts):.2f} -> {contracts} contracts"
    )
    return contracts, dollars_used


def implied_prob(contract_price_cents: float) -> float:
    """Convert contract price in cents to implied probability (0-1)."""
    return contract_price_cents / 100.0


async def _portfolio_has_position(
    session: aiohttp.ClientSession,
    ticker: str,
    side: str,
) -> bool:
    """Check whether the portfolio already holds a position for (ticker, side)."""
    if not ticker or not side:
        return False
    try:
        pos_path = f"/portfolio/positions?ticker={ticker}"
        async with session.get(
            bot_state.KALSHI_BASE_URL + pos_path,
            headers=kalshi_headers("GET", pos_path),
            timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                return False
            pos_data = await resp.json()
        positions = pos_data.get("market_positions") or pos_data.get("positions") or []
        for p in positions:
            if p.get("ticker") == ticker:
                held = p.get("position", 0)
                if side == "yes" and held > 0:
                    return True
                if side == "no" and held < 0:
                    return True
    except Exception as exc:
        log.warning(f"_portfolio_has_position error for {ticker}: {exc}")
    return False


async def _verify_order_fill(
    session: aiohttp.ClientSession,
    order_id: str,
    expected_filled: int,
    ticker: str = "",
    side: str = "",
) -> bool:
    """Confirm a fill is recorded in Kalshi by re-fetching the order.

    Conservative: missing count fields return False, not expected_filled.
    On HTTP errors, falls back to a portfolio position check (requires ticker+side).
    On network exceptions, returns False.
    """
    try:
        chk_path = f"/portfolio/orders/{order_id}"
        async with session.get(
            bot_state.KALSHI_BASE_URL + chk_path,
            headers=kalshi_headers("GET", chk_path),
            timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                log.warning(f"_verify_order_fill: GET {order_id} HTTP {resp.status} -- checking portfolio")
                return await _portfolio_has_position(session, ticker, side)
            chk = await resp.json()
        order = chk.get("order") or chk
        status = order.get("status", "")
        counts = extract_order_counts(order)
        filled = counts["filled"]
        total = counts["total"]
        remaining = counts["remaining"]
        if filled is not None:
            confirmed_filled = filled
        elif total is not None and remaining is not None:
            confirmed_filled = total - remaining
        else:
            log.warning(
                "_verify_order_fill: missing count fields",
                extra={"order_id": order_id, "keys": list(order.keys())},
            )
            return False
        log.info(
            f"_verify_order_fill: {order_id} status={status!r} "
            f"filled={confirmed_filled}/{total} (expected={expected_filled})"
        )
        return confirmed_filled > 0
    except Exception as exc:
        log.warning(f"_verify_order_fill error for {order_id}: {exc} -- returning False (conservative)")
        return False


async def place_order(
    session: aiohttp.ClientSession,
    ticker: str,
    side: str,
    contracts: int,
    entry_price_cents: int,
    mode: str,
    market: dict | None = None,
    asset: str = "BTC",
    secs_left: float = 900.0,
) -> dict:
    """
    Place a market order on Kalshi.

    Fills at best available price immediately. No price-bumping or GTC/IOC.

    In paper mode, simulates an instant fill without hitting the API.

    Returns:
        Dict with keys: fill_confirmed (bool), fill_price_cents (int|None),
        order_id (str|None).
    """
    if contracts <= 0:
        log.error(f"place_order called with contracts={contracts} -- refusing to send invalid order")
        return {"fill_confirmed": False, "fill_price_cents": None, "order_id": None}

    _original_strategy_target_c = entry_price_cents

    fresh_ob = None
    try:
        fresh_ob = await fetch_orderbook(session, ticker, market)
        if fresh_ob is not None:
            fp = fresh_ob["best_yes_ask"] if side == "yes" else fresh_ob["best_no_ask"]
            if fp is not None and fp != entry_price_cents:
                log.info(f"Price updated {entry_price_cents}c -> {fp}c for {side.upper()} on {ticker}")
                entry_price_cents = fp
    except Exception as _fe:
        log.warning(f"Fresh price fetch failed: {_fe}")

    _market_ask_at_post_c = None
    try:
        _ob = fresh_ob if isinstance(fresh_ob, dict) else {}
        _market_ask_at_post_c = _ob.get("best_yes_ask") if side == "yes" else _ob.get("best_no_ask")
    except Exception:
        _market_ask_at_post_c = None

    if mode == "paper":
        paper_fill = _market_ask_at_post_c if _market_ask_at_post_c is not None else entry_price_cents
        log.info(f"[PAPER] Simulated BUY {side} {contracts}x @ {paper_fill}c on {ticker}")
        return {
            "fill_confirmed": True,
            "fill_price_cents": paper_fill,
            "order_id": f"paper_{int(time.time() * 1000)}",
        }

    if fresh_ob is None or _market_ask_at_post_c is None:
        log.error("place_order: no orderbook ask, refusing to send blind IOC")
        return {"fill_confirmed": False, "fill_price_cents": None, "order_id": None}

    path = "/portfolio/orders"
    price_this_attempt = _market_ask_at_post_c

    if mode == "demo":
        client_order_id = f"demo_{int(time.time() * 1000)}"
        body: dict = {
            "ticker": ticker,
            "side": side,
            "count": contracts,
            "action": "buy",
            "client_order_id": client_order_id,
            "time_in_force": "immediate_or_cancel",
        }
        if side == "yes":
            body["yes_price"] = _market_ask_at_post_c
        else:
            body["no_price"] = _market_ask_at_post_c
        log.info(f"[demo] IOC {side.upper()} {contracts}x @ {_market_ask_at_post_c}c on {ticker}")
        try:
            async with session.post(
                bot_state.KALSHI_BASE_URL + path,
                headers=kalshi_headers("POST", path),
                json=body,
                timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
            ) as resp:
                data = await resp.json()
                http_status = resp.status
        except Exception as exc:
            log.error(f"[demo] Order POST failed: {exc}")
            return {"fill_confirmed": False, "fill_price_cents": None, "order_id": None}

        if http_status not in (200, 201):
            log.error(f"[demo] Order HTTP {http_status}: {data}")
            return {"fill_confirmed": False, "fill_price_cents": None, "order_id": None}

        order_id = (data.get("order") or {}).get("order_id") or data.get("order_id")
        if not order_id:
            log.error(f"[demo] No order_id in response: {data}")
            return {"fill_confirmed": False, "fill_price_cents": None, "order_id": None}

        _poll_interval = 3.0
        _poll_timeout  = 30.0
        _elapsed       = 0.0
        filled_order   = None
        while _elapsed < _poll_timeout:
            await asyncio.sleep(_poll_interval)
            _elapsed += _poll_interval
            try:
                chk_path = f"/portfolio/orders/{order_id}"
                async with session.get(
                    bot_state.KALSHI_BASE_URL + chk_path,
                    headers=kalshi_headers("GET", chk_path),
                    timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
                ) as chk_resp:
                    chk_data = await chk_resp.json()
                chk_order = chk_data.get("order") or chk_data
                status = chk_order.get("status", "")
                if status not in ("resting", "pending"):
                    filled_order = chk_order
                    break
            except Exception as pe:
                log.warning(f"[demo] Poll error: {pe}")

        if filled_order is None:
            log.info(f"[demo] No fill after {_poll_timeout:.0f}s -- cancelling {order_id}")
            try:
                del_path = f"/portfolio/orders/{order_id}"
                async with session.delete(
                    bot_state.KALSHI_BASE_URL + del_path,
                    headers=kalshi_headers("DELETE", del_path),
                    timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
                ) as del_resp:
                    await del_resp.json()
            except Exception as ce:
                log.warning(f"[demo] Cancel failed: {ce}")
            return {"fill_confirmed": False, "fill_price_cents": None, "order_id": order_id}

        status = filled_order.get("status", "")
        if status in ("cancelled", "canceled", "expired"):
            return {"fill_confirmed": False, "fill_price_cents": None, "order_id": order_id}

        _demo_counts = extract_order_counts(filled_order)
        cc = _demo_counts["filled"]
        if cc is None:
            if _demo_counts["total"] is not None and _demo_counts["remaining"] is not None:
                cc = _demo_counts["total"] - _demo_counts["remaining"]
        fp = extract_fill_price_cents(filled_order, side)
        if fp is None:
            fp = entry_price_cents
        _fill_yes_price = extract_fill_price_cents(filled_order, "yes") or entry_price_cents
        log.info(f"[demo] Order {order_id} filled={cc}x @ {fp}c status={status!r}")
        await _maybe_fill_verification_notify(
            asset, ticker, side, market, secs_left,
            _original_strategy_target_c, price_this_attempt,
            _market_ask_at_post_c, _fill_yes_price,
        )
        return {"fill_confirmed": bool(cc and cc > 0), "fill_price_cents": fp, "order_id": order_id, "filled_contracts": cc}

    # Live mode
    if secs_left < 300.0:
        _placed_mins = int(secs_left // 60)
        _placed_secs = int(secs_left % 60)
        _placed_elapsed = seconds_elapsed(market) if market else 0.0
        _placed_ctx = _notify_ctx(
            asset, ticker, (_placed_elapsed + secs_left) / 60.0,
            _phase_for_eth(asset, _placed_elapsed),
        )
    log.info(f"[live] IOC {side.upper()} {contracts}x @ {_market_ask_at_post_c}c on {ticker} ({secs_left:.0f}s left)")

    client_order_id = f"kalshi_{int(time.time() * 1000)}"
    for attempt in range(2):
        if attempt > 0:
            log.info(f"[live] Order retry {attempt}/1...")
            await asyncio.sleep(1.0)

        body: dict = {
            "ticker": ticker,
            "side": side,
            "count": contracts,
            "action": "buy",
            "client_order_id": client_order_id,
            "time_in_force": "immediate_or_cancel",
        }
        if side == "yes":
            body["yes_price"] = _market_ask_at_post_c
        else:
            body["no_price"] = _market_ask_at_post_c

        try:
            async with session.post(
                bot_state.KALSHI_BASE_URL + path,
                headers=kalshi_headers("POST", path),
                json=body,
                timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
            ) as resp:
                data = await resp.json()
                http_status = resp.status
        except Exception as exc:
            log.error(f"[live] Order POST failed (attempt {attempt}): {exc}")
            try:
                chk_path = f"/portfolio/positions?ticker={ticker}"
                async with session.get(
                    bot_state.KALSHI_BASE_URL + chk_path,
                    headers=kalshi_headers("GET", chk_path),
                    timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
                ) as chk_resp:
                    chk_data = await chk_resp.json()
                positions = chk_data.get("market_positions") or chk_data.get("positions") or []
                for p in positions:
                    if p.get("ticker") == ticker:
                        held = p.get("position", 0)
                        if (side == "yes" and held > 0) or (side == "no" and held < 0):
                            held_count = abs(held)
                            log.info(f"POST exception but portfolio shows {held_count}x -- treating as filled")
                            return {"fill_confirmed": True, "fill_price_cents": price_this_attempt, "order_id": None, "filled_contracts": held_count}
            except Exception as chk_exc:
                log.error(f"Portfolio check after POST exception failed: {chk_exc}")
            continue

        if http_status == 429:
            log.warning(f"[live] Rate-limited (429) on attempt {attempt} -- waiting 2s")
            await asyncio.sleep(2.0)
            continue

        if http_status not in (200, 201):
            log.error(f"[live] Order HTTP {http_status}: {data}")
            err_code = (data.get("error") or {}).get("code", "")
            _non_retryable = {
                "insufficient_funds", "authentication_error", "not_found", "forbidden",
                "market_not_open", "market_settled", "market_not_found",
                "order_limit_exceeded", "contract_limit_exceeded", "position_limit_exceeded",
                "invalid_count", "invalid_order", "min_contracts_not_met",
            }
            if err_code in _non_retryable:
                log.error(f"Non-retryable error ({err_code}). Stopping order attempts.")
                _failed_elapsed = seconds_elapsed(market) if market else 0.0
                _failed_ctx = _notify_ctx(
                    asset, ticker, (_failed_elapsed + secs_left) / 60.0,
                    _phase_for_eth(asset, _failed_elapsed),
                )
                break
            continue

        order_id = (data.get("order") or {}).get("order_id") or data.get("order_id")
        if not order_id:
            log.error(f"[live] No order_id in response: {data}")
            continue

        post_order  = data.get("order") or data
        post_status = post_order.get("status", "")
        log.info(f"[live] Order {order_id} POST status={post_status!r}")

        if post_status in ("resting", "pending"):
            log.warning(f"[live] Market order {order_id} returned {post_status!r} -- polling 5s")
            for _pi in range(5):
                await asyncio.sleep(1.0)
                try:
                    _op = f"/portfolio/orders/{order_id}"
                    async with session.get(
                        bot_state.KALSHI_BASE_URL + _op,
                        headers=kalshi_headers("GET", _op),
                        timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
                    ) as _r:
                        _d = await _r.json()
                    _polled = _d.get("order") or _d
                    if _polled.get("status", "") not in ("resting", "pending"):
                        post_order  = _polled
                        post_status = _polled.get("status", "")
                        log.info(f"[live] Order {order_id} status changed to {post_status!r} (poll {_pi+1}/5)")
                        break
                except Exception:
                    break
            if post_status in ("resting", "pending"):
                try:
                    del_path = f"/portfolio/orders/{order_id}"
                    async with session.delete(
                        bot_state.KALSHI_BASE_URL + del_path,
                        headers=kalshi_headers("DELETE", del_path),
                        timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
                    ) as _del_resp:
                        await _del_resp.json()
                except Exception as _de:
                    log.warning(f"[live] Cancel of resting market order {order_id} failed: {_de}")
                continue

        if post_status in ("canceled", "cancelled"):
            _can_counts = extract_order_counts(post_order)
            _filled = _can_counts["filled"]
            if _filled is None:
                _ct = _can_counts["total"]
                _cr = _can_counts["remaining"]
                _filled = (_ct - _cr) if (_ct is not None and _cr is not None) else 0
            if _filled > 0:
                _fp_canceled = extract_fill_price_cents(post_order, side) or price_this_attempt
                _fill_yes_price = extract_fill_price_cents(post_order, "yes") or price_this_attempt
                log.info(f"[live] IOC order {order_id} partial fill: {_filled}x @ {_fp_canceled}c")
                await _maybe_fill_verification_notify(
                    asset, ticker, side, market, secs_left,
                    _original_strategy_target_c, price_this_attempt,
                    _market_ask_at_post_c, _fill_yes_price,
                )
                _verified = await _verify_order_fill(session, order_id, _filled, ticker, side)
                return {"fill_confirmed": _verified, "fill_price_cents": _fp_canceled, "order_id": order_id, "filled_contracts": _filled}
            log.info(f"[live] IOC order {order_id} zero-fill (canceled)")
            break

        _live_counts = extract_order_counts(post_order)
        if _live_counts["filled"] is not None:
            filled_count = _live_counts["filled"]
        elif _live_counts["total"] is not None and _live_counts["remaining"] is not None:
            filled_count = _live_counts["total"] - _live_counts["remaining"]
        else:
            log.warning(
                f"[live] Order {order_id}: missing count fields — checking portfolio",
                extra={"order_id": order_id, "keys": list(post_order.keys())},
            )
            break

        if filled_count == 0:
            log.warning(f"[live] Order {order_id} status={post_status!r} but filled_count=0")
            continue

        fill_price = extract_fill_price_cents(post_order, side) or price_this_attempt
        _fill_yes_price = extract_fill_price_cents(post_order, "yes") or price_this_attempt
        log.info(f"[live] Order FILLED: {order_id} @ {fill_price}c x{filled_count} status={post_status!r}")
        await _maybe_fill_verification_notify(
            asset, ticker, side, market, secs_left,
            _original_strategy_target_c, price_this_attempt,
            _market_ask_at_post_c, _fill_yes_price,
        )
        _verified = await _verify_order_fill(session, order_id, filled_count, ticker, side)
        return {"fill_confirmed": _verified, "fill_price_cents": fill_price, "order_id": order_id, "filled_contracts": filled_count}

    log.warning(f"Market order not confirmed for {ticker} -- checking portfolio")
    try:
        pos_path = f"/portfolio/positions?ticker={ticker}"
        async with session.get(
            bot_state.KALSHI_BASE_URL + pos_path,
            headers=kalshi_headers("GET", pos_path),
            timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
        ) as resp:
            pos_data = await resp.json()
        positions = pos_data.get("market_positions") or pos_data.get("positions") or []
        for p in positions:
            if p.get("ticker") == ticker:
                held = p.get("position", 0)
                if side == "yes" and held > 0:
                    log.info(f"Portfolio check: found YES position {held}x on {ticker} -- order DID fill")
                    return {"fill_confirmed": True, "fill_price_cents": entry_price_cents, "order_id": None, "filled_contracts": held}
                if side == "no" and held < 0:
                    held_no = abs(held)
                    log.info(f"Portfolio check: found NO position {held_no}x on {ticker} -- order DID fill")
                    return {"fill_confirmed": True, "fill_price_cents": entry_price_cents, "order_id": None, "filled_contracts": held_no}
        log.info(f"Portfolio check: no position found for {ticker}")
    except Exception as exc:
        log.error(f"Portfolio check error: {exc}")

    log.error(f"Market order not filled for {ticker} {side}")
    _nofill_elapsed = seconds_elapsed(market) if market else 0.0
    _nofill_ctx = _notify_ctx(
        asset, ticker, (_nofill_elapsed + secs_left) / 60.0,
        _phase_for_eth(asset, _nofill_elapsed),
    )
    return {"fill_confirmed": False, "fill_price_cents": None, "order_id": None}
