"""bot_hourly.py — Dwell+Late hourly Kalshi market strategy (BTC/ETH only).

Dwell  (30-42 min elapsed): ETH/BTC has stayed on one side >= 80% of the window
        AND ended with a streak >= 60%. WR ~82-83%.
Late   (>= 45 min elapsed): price >= 0.3% from strike AND market entry >= 85c.
        WR ~96-97%.

One position per asset at a time. Uses asset_manager price history for dwell
features. Settlement detected on the next tick after market close.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

log = logging.getLogger("bot")

# ── Signal params ─────────────────────────────────────────────────────────────

DWELL_MIN_ELAPSED_S = 30 * 60
DWELL_MAX_ELAPSED_S = 42 * 60
DWELL_THRESHOLD     = 0.80
STREAK_THRESHOLD    = 0.60
LATE_MIN_ELAPSED_S  = 45 * 60
LATE_MIN_DIST_PCT   = 0.003
LATE_MIN_ENTRY_C    = 85.0
HOURLY_ASSETS       = ("BTC", "ETH")
HOURLY_MARKET_MAX_S = 65 * 60  # accept markets with up to 65 min remaining

# ── Per-asset state ───────────────────────────────────────────────────────────
# Schema: {asset: {"phase": "READY"|"LOCKED", "position": {...} | None}}

_hourly_state: dict[str, dict] = {
    "BTC": {"phase": "READY", "position": None},
    "ETH": {"phase": "READY", "position": None},
}


# ── Signal functions (pure, testable) ─────────────────────────────────────────

def dwell_features(prices: list[float], strike: float) -> dict | None:
    """
    Compute dwell and streak fraction from a list of 1m close prices.
    Returns None if fewer than 10 bars.
    """
    if len(prices) < 10:
        return None
    above = [p > strike for p in prices]
    current_itm = above[-1]
    dwell_frac = sum(1 for a in above if a == current_itm) / len(above)
    streak = 0
    for a in reversed(above):
        if a == current_itm:
            streak += 1
        else:
            break
    return {
        "dwell_frac":  dwell_frac,
        "streak_frac": streak / len(above),
        "is_itm":      current_itm,
    }


def dwell_signal(prices: list[float], strike: float) -> str | None:
    """Return 'yes', 'no', or None based on dwell features."""
    feats = dwell_features(prices, strike)
    if feats is None:
        return None
    if feats["dwell_frac"] >= DWELL_THRESHOLD and feats["streak_frac"] >= STREAK_THRESHOLD:
        return "yes" if feats["is_itm"] else "no"
    return None


def late_signal(current_price: float, strike: float, yes_ask_c: float, no_ask_c: float) -> tuple[str, float] | None:
    """
    Return (side, entry_cents) or None for the late-window rule.
    Fires when price is >= 0.3% from strike AND market entry >= 85c.
    """
    pct = (current_price - strike) / strike
    if pct >= LATE_MIN_DIST_PCT and yes_ask_c >= LATE_MIN_ENTRY_C:
        return "yes", yes_ask_c
    if pct <= -LATE_MIN_DIST_PCT and no_ask_c >= LATE_MIN_ENTRY_C:
        return "no", no_ask_c
    return None


# ── Market fetch helper ───────────────────────────────────────────────────────

async def fetch_hourly_market(session, asset: str) -> dict | None:
    """
    Fetch the soonest-closing open hourly market for asset (BTC or ETH).
    Returns None if none found or API error.
    """
    import aiohttp
    import bot_state
    from bot_market import kalshi_headers

    series_map = {"BTC": "KXBTCD", "ETH": "KXETHD"}
    series = series_map.get(asset)
    if series is None:
        return None

    path = "/markets"
    params = {"series_ticker": series, "status": "open", "limit": 10}
    try:
        async with session.get(
            bot_state.KALSHI_BASE_URL + path,
            headers=kalshi_headers("GET", path),
            params=params,
            timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception as exc:
        log.warning("fetch_hourly_market [%s]: %s", asset, exc)
        return None

    now = datetime.now(timezone.utc)
    markets = data.get("markets", [])
    valid = []
    for m in markets:
        try:
            ct = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            secs = (ct - now).total_seconds()
            if 0 < secs < HOURLY_MARKET_MAX_S:
                valid.append((secs, m))
        except Exception:
            continue

    if not valid:
        return None
    valid.sort(key=lambda x: x[0])
    return valid[0][1]


def elapsed_seconds(market: dict) -> float:
    """Seconds since the hourly market opened."""
    try:
        ot = datetime.fromisoformat(market["open_time"].replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ot).total_seconds()
    except Exception:
        return 0.0


def secs_remaining(market: dict) -> float:
    try:
        ct = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00"))
        return (ct - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return 0.0


# ── Position management ───────────────────────────────────────────────────────

def _lock_position(asset: str, ticker: str, side: str, entry_c: float,
                   contracts: int, trade_id: str, strike: float, source: str) -> None:
    _hourly_state[asset]["phase"] = "LOCKED"
    _hourly_state[asset]["position"] = {
        "ticker":       ticker,
        "side":         side,
        "entry_c":      entry_c,
        "contracts":    contracts,
        "trade_id":     trade_id,
        "strike":       strike,
        "source":       source,
        "entry_ts":     time.time(),
    }


def _clear_position(asset: str) -> None:
    _hourly_state[asset]["phase"] = "READY"
    _hourly_state[asset]["position"] = None


# ── Main per-asset handler ────────────────────────────────────────────────────

async def run_hourly_asset(session, asset: str, config: dict) -> None:
    """
    One iteration of the hourly market loop for one asset.
    Called every 60 seconds from the hourly background task.
    """
    from asset_manager import get_price
    from bot_market import calculate_contracts, place_order, seconds_remaining as sr
    from bot_infra import db_write_trade, db_update_trade

    state = _hourly_state[asset]
    mode  = config.get("mode", "paper")
    stake = float(config.get("trade_amount_dollars", 25))

    # ── Settle phase: check if position has expired ───────────────────────────
    if state["phase"] == "LOCKED":
        pos = state["position"]
        if pos is None:
            _clear_position(asset)
            return

        market = await fetch_hourly_market(session, asset)
        # Position settled when the market for its ticker is gone or closed
        if market is None or market.get("ticker") != pos["ticker"]:
            # Market closed — determine outcome from current price
            current_price = get_price(asset)
            if current_price is None:
                return
            strike = pos["strike"]
            won_yes = current_price > strike
            won = (pos["side"] == "yes" and won_yes) or (pos["side"] == "no" and not won_yes)
            outcome = "win" if won else "loss"
            exit_price = 100 if won else 0
            entry_frac  = pos["entry_c"] / 100.0
            fee_rate    = config.get("kalshi_fee_per_contract_cents", 7) / 100.0
            fee  = round(fee_rate * pos["contracts"] * entry_frac * (1 - entry_frac) * 100, 2)
            pnl  = (exit_price - pos["entry_c"]) * pos["contracts"] / 100 - fee
            await db_update_trade(pos["trade_id"], {
                "exit_price_cents": exit_price,
                "exit_reason":      "expiry",
                "outcome":          outcome,
                "pnl_dollars":      round(pnl, 2),
            })
            log.info("[hourly] %s %s settled: %s P&L=$%.2f", asset, pos["ticker"], outcome, pnl)
            _clear_position(asset)
        return

    # ── Ready phase: look for entry signals ───────────────────────────────────
    if not config.get("bot_enabled", False):
        return

    market = await fetch_hourly_market(session, asset)
    if market is None:
        return

    ticker   = market.get("ticker", "")
    elapsed  = elapsed_seconds(market)
    secs_rem = secs_remaining(market)
    if secs_rem < 60:
        return  # too close to close

    current_price = get_price(asset)
    if current_price is None:
        return

    from bot_market import parse_strike, fetch_orderbook
    strike = parse_strike(market)
    if strike is None:
        return

    # Get live orderbook prices
    ob = await fetch_orderbook(session, ticker)
    yes_ask_c = float(ob.get("yes_ask", 0) or 0) if ob else 0.0
    no_ask_c  = float(ob.get("no_ask",  0) or 0) if ob else 0.0

    side = None
    entry_c = None
    source  = ""

    # Late window (checked first — higher priority when elapsed >= 45 min)
    if elapsed >= LATE_MIN_ELAPSED_S and yes_ask_c > 0:
        result = late_signal(current_price, strike, yes_ask_c, no_ask_c)
        if result:
            side, entry_c = result
            source = "late"

    # Dwell window (30-42 min, ETH only based on backtest evidence)
    if side is None and DWELL_MIN_ELAPSED_S <= elapsed <= DWELL_MAX_ELAPSED_S and asset == "ETH":
        from asset_manager import _prices as _am_prices
        prices_deque = _am_prices.get(asset)
        if prices_deque:
            prices = list(prices_deque)[-int(elapsed // 60):]
            sig = dwell_signal(prices, strike)
            if sig:
                entry_c = yes_ask_c if sig == "yes" else no_ask_c
                if entry_c >= 50.0:  # minimum viability
                    side = sig
                    source = "dwell"

    if side is None or entry_c is None or entry_c <= 0:
        return

    contracts = calculate_contracts(stake, entry_c)
    if contracts < 1:
        return

    log.info("[hourly] %s %s signal=%s side=%s entry=%.0fc contracts=%d",
             asset, ticker, source, side, entry_c, contracts)

    fill = await place_order(
        session=session, ticker=ticker, side=side, contracts=contracts,
        entry_price_cents=entry_c, mode=mode, market=market,
        secs_left=secs_rem, btc_price=current_price,
        strike=strike, asset=asset,
    )

    if not fill.get("fill_confirmed"):
        log.info("[hourly] %s no fill", asset)
        return

    trade_id = await db_write_trade({
        "ts":                  datetime.now(timezone.utc).isoformat(),
        "ticker":              ticker,
        "asset":               asset,
        "side":                side,
        "entry_price_cents":   int(fill.get("fill_price_cents") or entry_c),
        "contracts":           contracts,
        "strategy_variant":    f"hourly_{source}",
        "mode":                mode,
        "outcome":             "open",
        "pnl_dollars":         0.0,
        "order_id":            fill.get("order_id", ""),
        "strike":              strike,
        "secs_left_at_entry":  secs_rem,
        "confidence_score":    None,
    })

    _lock_position(asset, ticker, side, entry_c, contracts, trade_id, strike, source)
    log.info("[hourly] %s LOCKED ticker=%s side=%s entry=%.0fc", asset, ticker, side, entry_c)
