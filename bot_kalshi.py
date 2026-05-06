"""bot_kalshi.py — RSA auth, Kalshi API calls, price helpers."""
import csv as _csv_module
import logging
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
from bot_config import read_config, write_config

log = logging.getLogger("bot")


def _simulated_amm_midpoint(btc_price: float, strike: float) -> tuple[float, float]:
    """
    Deterministic midpoint of simulate_amm_prices() — same distance bands as
    backtest.py but without random noise, so each call is reproducible.
    Used to record what the backtest *expects* vs what Kalshi actually quotes.
    """
    pct   = (btc_price - strike) / strike
    ap    = abs(pct) * 100
    above = pct > 0
    spread = 4.5  # midpoint of backtest's 3.0—6.0 spread

    if ap < 0.10:
        yes_ask = 51.5 if above else 48.5
    elif ap < 0.30:
        yes_ask = 68.5 if above else 31.5
    else:
        yes_ask = 84.5 if above else 15.5

    yes_ask = max(3.0, min(97.0, yes_ask))
    no_ask  = max(3.0, min(97.0, 100.0 + spread - yes_ask))
    return yes_ask, no_ask


def _log_price_validation(
    ts: str,
    ticker: str,
    btc_price: float,
    strike: float,
    sim_yes: float,
    sim_no: float,
    real_yes: float | None,
    real_no: float | None,
    mins_remaining: float = 0.0,
) -> None:
    """
    Append one row to price_validation_log.csv and print a running summary
    every 50 entries.  Logs null for real prices if the API call failed.

    Columns: ts, ticker, btc_price, strike, abs_pct, mins_remaining,
             sim_yes_ask, sim_no_ask, real_yes_ask, real_no_ask, price_gap_cents
    """

    gap     = (real_yes - sim_yes) if (real_yes is not None) else None
    abs_pct = round(abs((btc_price - strike) / strike) * 100, 4) if strike else 0.0

    file_exists = os.path.isfile(bot_state._PRICE_VAL_CSV)
    try:
        with open(bot_state._PRICE_VAL_CSV, "a", newline="", encoding="utf-8") as fh:
            writer = _csv_module.writer(fh)
            if not file_exists:
                writer.writerow([
                    "ts", "ticker", "btc_price", "strike",
                    "abs_pct", "mins_remaining",
                    "sim_yes_ask", "sim_no_ask",
                    "real_yes_ask", "real_no_ask",
                    "price_gap_cents",
                ])
            writer.writerow([
                ts, ticker, round(btc_price, 2), round(strike, 2),
                abs_pct, round(mins_remaining, 2),
                round(sim_yes, 1), round(sim_no, 1),
                round(real_yes, 1) if real_yes is not None else "null",
                round(real_no,  1) if real_no  is not None else "null",
                round(gap, 1) if gap is not None else "null",
            ])
    except Exception as exc:
        log.warning(f"Price validation CSV write error: {exc}")
        return

    bot_state._price_val_count += 1
    bot_state._price_val_sim_sum += sim_yes
    if real_yes is not None:
        bot_state._price_val_real_sum += real_yes
        bot_state._price_val_gap_sum  += gap
        bot_state._price_val_gap_n    += 1

    if bot_state._price_val_count % 50 == 0:
        n        = bot_state._price_val_count
        avg_sim  = bot_state._price_val_sim_sum / n
        avg_real = bot_state._price_val_real_sum / bot_state._price_val_gap_n if bot_state._price_val_gap_n > 0 else 0.0
        avg_gap  = bot_state._price_val_gap_sum  / bot_state._price_val_gap_n if bot_state._price_val_gap_n > 0 else 0.0
        verdict  = ("✓ within 3c" if abs(avg_gap) < 3
                    else "⚠ 3-7c gap — edge marginal" if abs(avg_gap) < 7
                    else "✗ >7c gap — strategy likely unprofitable")
        log.info(
            f"Price validation: {n} samples collected. "
            f"Avg price gap: {avg_gap:+.1f}c. "
            f"Simulated avg: {avg_sim:.1f}c. Real avg: {avg_real:.1f}c. "
            f"{verdict}"
        )


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
        # Paper mode simulates fills but still reads real Kalshi market data.
        # Load live credentials if present so market fetch/orderbook calls work.
        # Non-fatal if missing — bot runs fully simulated without them.
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
            # Demo creds not set in environment — degrade gracefully to paper mode
            # so the bot doesn't crash-loop. Set KALSHI_DEMO_API_KEY and
            # KALSHI_DEMO_PRIVATE_KEY in Railway to enable demo trading.
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
            # Load live creds if available so market data still works
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

    # Safety assertions — fail loudly rather than silently routing to the wrong endpoint
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
        Dict of header name → value.
    """
    ts = str(int(time.time() * 1000))
    # Kalshi signs the full URL path including the /trade-api/v2 prefix
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

    # Log all markets with their close times so we can see what's available
    now_utc = datetime.now(timezone.utc)
    for m in all_markets:
        try:
            close_dt = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            mins_left = (close_dt - now_utc).total_seconds() / 60
        except Exception:
            mins_left = -1
        log.info(f"  Market: {m.get('ticker')} | closes in {mins_left:.1f}m | {m.get('title','')[:60]}")

    # Drop obviously long-duration markets by title keyword.
    # "range" = KXBTC daily price-range markets (~1500 min). Keep "above/below" (KXBTCD).
    all_markets = [m for m in all_markets
                   if "range" not in m.get("title", "").lower()
                   and "daily" not in m.get("title", "").lower()]

    if not all_markets:
        log.warning("No valid short-duration markets after title filtering. Waiting for next window.")
        return [] if return_all else None

    # Compute duration for each market
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

    # Accept 15-min markets (reject anything > 20 min)
    short_dur = [m for m in all_markets
                 if (lambda d: d is not None and 1 <= d <= 20)(market_duration_minutes(m))]

    if short_dur:
        log.info(f"Found {len(short_dur)} short-duration market(s). "
                 + " | ".join(f"{m.get('ticker')} {market_duration_minutes(m):.0f}m" for m in short_dur[:5]))
        pool = short_dur
    else:
        # Fall back: any market closing within 60 minutes
        soon = [
            m for m in all_markets
            if (lambda c: 0 < c <= 60)(
                (datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")) - now_utc).total_seconds() / 60
                if m.get("close_time") else -1
            )
        ]
        if soon:
            log.info(f"No short-duration match by open→close — using {len(soon)} markets closing within 60 min.")
            pool = soon
        else:
            log.warning("No short-duration markets found. Waiting for next window.")
            return [] if return_all else None

    pool.sort(key=lambda m: m.get("close_time", ""))

    # Filter out markets that have already closed — sort picks earliest close_time
    # first, which can be an expired market when multiple windows are returned.
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
    # If every market has closed (rare edge case), keep pool as-is so caller sees DONE.

    # Prefer the shortest-duration markets (KXBTC15M 15-min over KXBTCD 60-min).
    # If we have any markets within 5 minutes of the minimum duration, use only those.
    # This prevents 60-min KXBTCD markets from polluting the multi-window pool when
    # a 15-min KXBTC15M market is available.
    durations = [market_duration_minutes(m) for m in pool]
    valid_durations = [d for d in durations if d is not None]
    if valid_durations:
        min_dur = min(valid_durations)
        focused = [m for m, d in zip(pool, durations) if d is not None and d <= min_dur + 5]
        if focused and len(focused) < len(pool):
            log.info(f"Focusing pool from {len(pool)} to {len(focused)} markets "
                     f"(duration ≤ {min_dur + 5:.0f}m, dropping {len(pool) - len(focused)} longer-duration markets)")
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

    # Drop daily/range markets; keep short-duration only
    all_markets = [m for m in all_markets
                   if "range" not in m.get("title", "").lower()
                   and "daily" not in m.get("title", "").lower()]
    if not all_markets:
        return None

    # Return soonest-expiring market with > 0 seconds left, < 20 min window
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
        log.warning(f"Cannot parse strike. Full market fields: { {k: market.get(k) for k in ('ticker','title','subtitle','floor_strike','cap_strike','strike_price','result','yes_sub_title','no_sub_title')} }")
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
    # Fallback: assume 15-minute window
    return max(0.0, 15 * 60 - seconds_remaining(market))


async def fetch_orderbook(
    session: aiohttp.ClientSession,
    ticker: str,
    market: dict | None = None,
) -> dict | None:
    """
    Fetch live prices for one market ticker.

    For AMM markets (KXBTC15M) the /orderbook endpoint returns empty arrays.
    We call GET /markets/{ticker} directly for fresh AMM prices — never using
    the 30-second-cached market object, which can be stale enough to produce
    completely wrong prices after a BTC move.

    Returns:
        Dict with best_yes_ask, best_no_ask, best_yes_bid (all cents), yes_liquidity,
        or None if no price data is available. best_yes_bid may be None.
    """
    def _dollars_to_cents(val) -> int | None:
        try:
            v = int(round(float(val) * 100))
            return v if v >= 0 else None
        except (TypeError, ValueError):
            return None

    # ── Step 1: try the orderbook endpoint (populated for limit-order markets)
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

    # ── Step 2: AMM fallback — fetch the individual market fresh (not cached).
    #    The market cache TTL is 30s which is too stale for AMM price fields.
    #    A direct GET /markets/{ticker} gives real-time yes_ask/no_ask dollars.
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
            # Last resort: use the cached market object if we have one
            fresh_market = market or {}

        src = fresh_market if fresh_market else (market or {})

        if best_yes_ask is None:
            best_yes_ask = _dollars_to_cents(src.get("yes_ask_dollars"))
        if best_yes_ask is None:
            no_bid = _dollars_to_cents(src.get("no_bid_dollars"))
            if no_bid is not None:
                _derived = 100 - no_bid
                if _derived > 0:
                    best_yes_ask = _derived

        if best_no_ask is None:
            best_no_ask = _dollars_to_cents(src.get("no_ask_dollars"))
        if best_no_ask is None:
            yes_bid = _dollars_to_cents(src.get("yes_bid_dollars"))
            if yes_bid is not None:
                _derived = 100 - yes_bid
                if _derived > 0:
                    best_no_ask = _derived

        if best_yes_bid is None:
            best_yes_bid = _dollars_to_cents(src.get("yes_bid_dollars"))
        if best_yes_bid is None:
            no_ask_raw = _dollars_to_cents(src.get("no_ask_dollars"))
            if no_ask_raw is not None:
                _derived = 100 - no_ask_raw
                if _derived >= 0:
                    best_yes_bid = _derived

        if best_yes_ask is not None or best_no_ask is not None:
            log.info(
                f"AMM prices for {ticker}: "
                f"yes_ask={best_yes_ask}¢  no_ask={best_no_ask}¢  yes_bid={best_yes_bid}¢"
            )

    if best_yes_ask is None or best_no_ask is None:
        # Diagnostic: dump the raw market fields so we can see what Kalshi actually returned.
        # On demo, non-BTC markets sometimes return no AMM fields at all — this log tells us
        # whether the field is missing vs. present but zero vs. present but filtered out.
        _diag_keys = ("yes_ask_dollars", "no_ask_dollars", "yes_bid_dollars", "no_bid_dollars",
                      "last_price", "status", "volume", "liquidity")
        _diag = {k: (src.get(k) if isinstance(src, dict) else None) for k in _diag_keys}
        log.warning(
            f"No price data available for {ticker} "
            f"(yes_ask={best_yes_ask} no_ask={best_no_ask}). "
            f"Raw market fields: {_diag}"
        )
        return None

    # Sanity check — reject prices outside [0, 100]. 0c is valid (sub-cent AMM settled market; EV returns -inf and skips). >100c is data corruption.
    # One side at 100c is valid at window open (e.g. yes_ask=12c, no_ask=100c).
    # Both at 100c means the market hasn't opened yet — reject.
    if not (0 <= best_yes_ask <= 100 and 0 <= best_no_ask <= 100):
        log.warning(
            f"Orderbook prices out of range for {ticker}: "
            f"yes_ask={best_yes_ask}c no_ask={best_no_ask}c — skipping"
        )
        return None
    if best_yes_ask == 100 and best_no_ask == 100:
        log.debug(f"Both sides at ceiling for {ticker} — market not ready yet")
        return None
    if best_yes_ask + best_no_ask < 100:
        log.warning(
            f"Orderbook sum below 100 for {ticker}: "
            f"yes_ask({best_yes_ask}c) + no_ask({best_no_ask}c) = {best_yes_ask+best_no_ask}c — skipping"
        )
        return None
    if best_yes_ask + best_no_ask > 150:
        log.warning(
            f"Orderbook sum very wide for {ticker}: "
            f"yes_ask({best_yes_ask}c) + no_ask({best_no_ask}c) = {best_yes_ask+best_no_ask}c — thin market, passing through"
        )

    # For AMM markets use the reported size; fall back to generous default
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
        "best_yes_bid": best_yes_bid,  # may be None
        "yes_liquidity": yes_liquidity,
        "no_liquidity":  no_liquidity,
    }
