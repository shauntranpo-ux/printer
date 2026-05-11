"""reconcile.py — Read-only Kalshi reconciliation helpers for crash recovery.

All functions are pure async, no global state, no side effects on Kalshi.
Only GET endpoints used — no order placement or cancellation.
"""
import logging
from datetime import datetime, timezone

import aiohttp

import bot_state
from bot_market import kalshi_headers
from kalshi_compat import extract_fill_price_cents

log = logging.getLogger("bot")


async def fetch_open_positions(
    session: aiohttp.ClientSession,
    mode: str,
) -> "dict | None":
    """
    Fetch all open positions from Kalshi.

    Returns {ticker: {"side": "yes"|"no", "count": int, "avg_price_cents": int}}
    Paper mode: returns {} without calling Kalshi.
    HTTP error or network exception: returns None (caller decides whether to proceed).
    """
    if mode == "paper":
        return {}
    path = "/portfolio/positions"
    try:
        async with session.get(
            bot_state.KALSHI_BASE_URL + path,
            headers=kalshi_headers("GET", path),
            timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                log.warning("fetch_open_positions: HTTP %s", resp.status)
                return None
            data = await resp.json()
        positions = data.get("market_positions") or data.get("positions") or []
        result: dict = {}
        for p in positions:
            ticker = p.get("ticker")
            held = p.get("position", 0)
            if not ticker or held == 0:
                continue
            count = abs(held)
            exposure = p.get("market_exposure") or 0
            result[ticker] = {
                "side": "yes" if held > 0 else "no",
                "count": count,
                "avg_price_cents": exposure // max(count, 1),
            }
        return result
    except Exception as exc:
        log.warning("fetch_open_positions error: %s", exc)
        return None


async def fetch_fills_for_ticker(
    session: aiohttp.ClientSession,
    ticker: str,
    since_ts_ms: int,
) -> list:
    """
    GET /portfolio/fills?ticker=X filtered to fills at or after since_ts_ms.

    Returns raw fill list. Returns [] on any error.
    NOTE: Kalshi fills schema assumed: {"side": "yes"|"no", "price": int, "count": int,
    "created_time": ISO8601}. Flag for follow-up if field names differ in production.
    """
    path = f"/portfolio/fills?ticker={ticker}&limit=100"
    try:
        async with session.get(
            bot_state.KALSHI_BASE_URL + path,
            headers=kalshi_headers("GET", path),
            timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                log.warning("fetch_fills_for_ticker %s: HTTP %s", ticker, resp.status)
                return []
            data = await resp.json()
        fills = data.get("fills") or []
        if not since_ts_ms:
            return fills
        out = []
        for f in fills:
            created = f.get("created_time", "")
            if not created:
                out.append(f)
                continue
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if int(dt.timestamp() * 1000) >= since_ts_ms:
                    out.append(f)
            except Exception:
                out.append(f)  # unparseable timestamp — include conservatively
        return out
    except Exception as exc:
        log.warning("fetch_fills_for_ticker %s: %s", ticker, exc)
        return []


async def fetch_market_resolution(
    session: aiohttp.ClientSession,
    ticker: str,
) -> str:
    """
    Fetch market status from Kalshi.
    Returns one of: "open", "resolved_yes", "resolved_no", "settled", "unknown".
    """
    path = f"/markets/{ticker}"
    try:
        async with session.get(
            bot_state.KALSHI_BASE_URL + path,
            headers=kalshi_headers("GET", path),
            timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                return "unknown"
            data = await resp.json()
        mkt = data.get("market") or data
        result = mkt.get("result")
        status = mkt.get("status", "")
        if result == "yes":
            return "resolved_yes"
        if result == "no":
            return "resolved_no"
        if status in ("", "open", "active"):
            return "open"
        if status == "settled":
            return "settled"
        return "unknown"
    except Exception as exc:
        log.warning("fetch_market_resolution %s: %s", ticker, exc)
        return "unknown"


async def classify_pending_trade(
    session: aiohttp.ClientSession,
    trade_row,
    mode: str,
) -> dict:
    """
    Decide what to do with a pending trade (row from the trades table).

    trade_row must support dict-style key access:
        market_id, side, contracts, entry_price_cents, ts, order_id, mode, asset

    Returns one of:
      {"action": "mark_filled", "exit_price_cents": int, "pnl_dollars": float,
       "outcome": "win"|"loss", "fill_confirmed": True}
      {"action": "mark_expired_unfilled", "pnl_dollars": 0.0}
      {"action": "mark_phantom", "reason": str}
      {"action": "leave_pending", "reason": str}

    Gross PnL only (no fee deduction) — consistent with how _settle_s1_orphans works
    for orphan reconcile at startup.
    """
    # Paper mode: no real money at stake, skip Kalshi entirely.
    if mode == "paper":
        return {"action": "mark_expired_unfilled", "pnl_dollars": 0.0}

    ticker = trade_row["market_id"] or ""
    side = trade_row["side"] or "yes"
    contracts = trade_row["contracts"] or 1
    ts_str = trade_row["ts"] or ""

    since_ts_ms = 0
    if ts_str:
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            since_ts_ms = int(dt.timestamp() * 1000)
        except Exception:
            pass

    # Step 1: Is the market still open?
    resolution = await fetch_market_resolution(session, ticker)
    if resolution == "open":
        return {"action": "leave_pending", "reason": "market still open"}

    # Step 2: Look for fills on this ticker since trade entry.
    fills = await fetch_fills_for_ticker(session, ticker, since_ts_ms)
    # Kalshi fills "side" field: "yes" or "no".
    matching = [f for f in fills if f.get("side") == side]

    if matching:
        _prices = [extract_fill_price_cents(f, side) for f in matching]
        _valid_prices = [p for p in _prices if p is not None]
        avg_fill = sum(_valid_prices) / len(_valid_prices) if _valid_prices else 0
        # Winning side settles at 100 cents, losing side at 0.
        settle = 100 if (
            (side == "yes" and resolution == "resolved_yes") or
            (side == "no"  and resolution == "resolved_no")
        ) else 0
        pnl = (settle - avg_fill) * contracts / 100.0
        return {
            "action":           "mark_filled",
            "exit_price_cents": settle,
            "pnl_dollars":      round(pnl, 2),
            "outcome":          "win" if pnl >= 0 else "loss",
            "fill_confirmed":   True,
        }

    # Step 3: Any open position for this ticker?
    open_pos = await fetch_open_positions(session, mode)
    if open_pos is None:
        return {"action": "leave_pending", "reason": "positions fetch failed, will retry next startup"}

    if ticker in open_pos:
        return {"action": "leave_pending", "reason": "position still open, will settle next cycle"}

    # Step 4: Market closed, no fill, no open position — order never filled.
    return {
        "action": "mark_phantom",
        "reason": (
            f"market {resolution}, no fill found, no open position — "
            "order likely never filled; entry cost not charged"
        ),
    }
