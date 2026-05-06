"""bot_trade.py — S1 trade execution, settlement, orphan recovery."""
import asyncio
import json
import logging
import math
import time
from datetime import datetime, timezone, timedelta

import aiohttp

import bot_state
from bot_config import get_asset_config
from bot_orders import place_order, calculate_contracts
from bot_db import db_write_trade, db_update_trade
from bot_notify import send_telegram
from bot_kalshi import kalshi_headers

log = logging.getLogger("bot")


async def _execute_s1_trade(
    session: "aiohttp.ClientSession",
    brain_s1: dict,
    ticker: str,
    btc_price: float,
    strike: float,
    yes_ask: float,
    no_ask: float,
    elapsed_seconds: float,
    secs_left: float,
    asset: str,
    config: dict,
    mode: str,
    ob: dict,
    market: "dict | None" = None,
) -> None:
    """Place a real S1 order alongside S2 and track it in _s1_pending_trades."""
    if brain_s1.get("action") != "trade":
        return
    if ticker in bot_state._s1_pending_trades:
        return  # already have an open S1 trade on this ticker

    side = brain_s1.get("side", "yes")
    _s1_allowed = get_asset_config(config, asset, "allowed_sides", config.get("allowed_sides"))
    if _s1_allowed and side not in _s1_allowed:
        return
    entry_price_cents = yes_ask if side == "yes" else no_ask
    avail_liquidity = ob["yes_liquidity"] if side == "yes" else ob["no_liquidity"]
    trade_amount = float(config.get("trade_amount_dollars", 25))
    contracts, dollars_used = calculate_contracts(trade_amount, int(entry_price_cents), avail_liquidity)
    if contracts == 0 or dollars_used < trade_amount * 0.90:
        return

    result = await place_order(session, ticker, side, contracts, int(entry_price_cents), mode, market, asset=asset, secs_left=secs_left)
    if not result["fill_confirmed"]:
        log.info(f"[S1] {ticker}: order not filled -- skipping")
        return
    _fp = result.get("fill_price_cents")
    fill_price = _fp if _fp is not None else int(entry_price_cents)
    _fc = result.get("filled_contracts")
    contracts = _fc if _fc is not None else contracts

    _fee_rate = config.get("kalshi_fee_per_contract_cents", 7) / 100.0
    _entry_p  = fill_price / 100.0
    _fee      = _fee_rate * (1.0 - _entry_p)
    win_prob  = brain_s1.get("win_prob", 0.5)
    ev_val    = round((win_prob - _entry_p - _fee) * 100, 1)
    _ev_str   = f"+{ev_val}%" if ev_val >= 0 else f"{ev_val}%"

    import json as _json
    trade_data = {
        "ts":                   datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "market_id":            ticker,
        "market_title":         ticker,
        "mode":                 mode,
        "side":                 side,
        "contracts":            contracts,
        "entry_price_cents":    fill_price,
        "trade_amount_dollars": round(dollars_used, 2),
        "confidence_score":     brain_s1.get("confidence", 50),
        "model_prob":           win_prob,
        "implied_prob":         _entry_p,
        "btc_price_at_entry":   btc_price,
        "strike":               strike,
        "seconds_left_at_entry": int(secs_left),
        "fill_confirmed":       1,
        "outcome":              "pending",
        "order_id":             result.get("order_id"),
        "asset":                asset,
        "raw_p_yes":            brain_s1.get("raw_p_yes"),
        "entry_signals":        _json.dumps(brain_s1.get("signals", {})),
        "strategy_variant":     "strategy1",
        "strategy_version":     bot_state._S1_VERSION,
    }
    # Register in _s1_pending_trades BEFORE the DB write so that a transient
    # SQLite error never leaves a real filled order untracked (funds already committed).
    bot_state._s1_pending_trades[ticker] = {
        "trade_id":          None,  # filled in below after successful DB write
        "side":              side,
        "entry_price_cents": fill_price,
        "contracts":         contracts,
        "strike":            strike,
        "asset":             asset,
        "mode":              mode,
        "entry_ts":          time.time(),
        "market_close_time": (market or {}).get("close_time", ""),
    }
    trade_id = await db_write_trade(trade_data)
    if trade_id is None:
        log.critical(f"[S1] {ticker}: DB write failed — position tracked in-memory only; reconcile manually")
    else:
        bot_state._s1_pending_trades[ticker]["trade_id"] = trade_id

    mode_icon = {"paper": "[PAPER]", "demo": "[DEMO]"}.get(mode, "[LIVE]")
    _win_pct  = int(win_prob * 100)
    _payout   = round((100 - fill_price) * contracts / 100, 2)
    _cost     = round(fill_price * contracts / 100, 2)
    log.info(f"[S1] {ticker}: ORDER FILLED -- {side.upper()} {contracts}x @ {fill_price}c")
    await send_telegram(
        f"<b>🟡 [S1 Original] {asset} {mode_icon} ORDER FILLED</b>\n"
        f"<b>{side.upper()} — {'UP' if side == 'yes' else 'DOWN'}</b>  {contracts} contracts @ <b>{fill_price}c</b>\n"
        f"Cost: ${_cost:.2f}  |  Max payout: ${_payout:.2f}\n"
        f"Win prob: {_win_pct}%  |  EV: {_ev_str}\n"
        f"Strike: ${strike:,.0f}  |  {asset}: ${btc_price:,.0f}\n"
        f"Expires {int(secs_left // 60)}m {int(secs_left % 60)}s"
    )


async def _settle_s1_trade(
    ticker: str,
    market_result: "str | None",
    btc_price: float,
    config: dict,
    asset: str,
) -> None:
    """Settle a pending S1 real trade. market_result is 'yes', 'no', or None (price fallback)."""
    s1_pos = bot_state._s1_pending_trades.pop(ticker, None)
    if s1_pos is None:
        return

    if market_result == "yes":
        outcome = "win" if s1_pos["side"] == "yes" else "loss"
    elif market_result == "no":
        outcome = "win" if s1_pos["side"] == "no" else "loss"
    else:
        outcome = "win" if (
            (s1_pos["side"] == "yes" and btc_price > s1_pos["strike"]) or
            (s1_pos["side"] == "no"  and btc_price <= s1_pos["strike"])
        ) else "loss"

    exit_price = 100 if outcome == "win" else 0
    _entry_p   = s1_pos["entry_price_cents"] / 100.0
    _fee_rate  = config.get("kalshi_fee_per_contract_cents", 7) / 100.0
    fee  = math.ceil(_fee_rate * s1_pos["contracts"] * _entry_p * (1.0 - _entry_p) * 100) / 100
    pnl  = (exit_price - s1_pos["entry_price_cents"]) * s1_pos["contracts"] / 100 - fee
    profit_pct = (exit_price - s1_pos["entry_price_cents"]) / s1_pos["entry_price_cents"] * 100 \
                 if s1_pos["entry_price_cents"] else 0

    await db_update_trade(s1_pos["trade_id"], {
        "exit_price_cents": exit_price,
        "exit_reason":      "expiry",
        "outcome":          outcome,
        "pnl_dollars":      round(pnl, 2),
        "profit_percent":   round(profit_pct, 2),
    })
    log.info(f"[S1] {ticker}: settled -- {outcome}, P&L=${pnl:.2f}")

    pnl_str     = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    outcome_str = "✅ WIN" if outcome == "win" else "❌ LOSS"
    pct_str     = f"+{profit_pct:.0f}%" if profit_pct >= 0 else f"{profit_pct:.0f}%"
    mode_icon   = {"paper": "[PAPER]", "demo": "[DEMO]"}.get(s1_pos["mode"], "[LIVE]")
    _time_str   = datetime.now(timezone(timedelta(hours=-7))).strftime("%b %d %I:%M %p PST")
    _now        = time.time()
    _dur_secs   = int(_now - s1_pos.get("entry_ts", _now))
    _dur_str    = f"{_dur_secs // 60}m {_dur_secs % 60}s"
    await send_telegram(
        f"<b>🟡 [S1 Original] {asset} {mode_icon} {outcome_str}  {pnl_str}  ({pct_str})</b>  —  {_time_str}\n"
        f"{s1_pos['side'].upper()}  {s1_pos['contracts']} contracts  |  held {_dur_str}\n"
        f"Entry: {s1_pos['entry_price_cents']}c  ->  Expiry: {exit_price}c\n"
        f"Strike: ${s1_pos['strike']:,.0f}"
    )


async def _try_settle_orphaned_s1(
    session: "aiohttp.ClientSession",
    ticker: str,
    btc_price: float,
    config: dict,
    asset: str,
) -> None:
    """Settle an S1 trade when the market expired but S2 never locked."""
    if ticker not in bot_state._s1_pending_trades:
        return
    market_result = None
    for _attempt in range(6):
        try:
            _path = f"/markets/{ticker}"
            async with session.get(
                bot_state.KALSHI_BASE_URL + _path,
                headers=kalshi_headers("GET", _path),
                timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
            ) as _resp:
                _mdata = await _resp.json()
            market_result = (_mdata.get("market") or _mdata).get("result")
            if market_result in ("yes", "no"):
                break
        except Exception as _exc:
            log.warning(f"[S1 orphan] market result fetch error (attempt {_attempt}): {_exc}")
        await asyncio.sleep(5)
    await _settle_s1_trade(ticker, market_result, btc_price, config, asset)
