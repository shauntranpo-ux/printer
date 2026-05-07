"""bot_risk.py — Risk management, trade execution, and preflight checks.

Public interface (see __all__):
  Risk:      check_daily_limits, midnight_reset, write_state_file, _log_entry,
             _parse_strike_from_ticker
  Trade:     _execute_s1_trade, _settle_s1_trade, _try_settle_orphaned_s1
  Preflight: verify_kalshi_connection, run_preflight_checks
"""
import asyncio
import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import aiohttp

import bot_state
from bot_infra import (
    atomic_write_json, read_config, write_config, get_asset_config,
    db_get_today_pnl, db_write_market_log, db_write_trade, db_update_trade,
    send_telegram, _phase_for_eth,
)
from bot_market import (
    kalshi_headers, seconds_remaining, seconds_elapsed,
    place_order, calculate_contracts,
)
from bot_strategy import _strategy_name_for
from asset_manager import (
    get_price           as _am_get_price,
    price_age_seconds   as _am_price_age,
    get_24h_change      as _am_get_24h_change,
)

log = logging.getLogger("bot")

_STRIKE_RE_T_SUFFIX       = re.compile(r"-T(\d+)$")
_STRIKE_RE_NUMERIC_SUFFIX = re.compile(r"-(\d+)$")

__all__ = [
    # Risk / limits
    "check_daily_limits", "midnight_reset", "write_state_file", "_log_entry",
    "_parse_strike_from_ticker",
    # Trade execution (absorbed from bot_trade)
    "_execute_s1_trade", "_settle_s1_trade", "_try_settle_orphaned_s1",
    # Preflight (absorbed from bot_preflight)
    "verify_kalshi_connection", "run_preflight_checks",
]


# ---------------------------------------------------------------------------
# Daily limits
# ---------------------------------------------------------------------------

async def check_daily_limits(config: dict) -> tuple[bool, str]:
    """
    Check daily loss limit and profit target for live/demo mode.

    live  -- DLL/profit target flips mode to 'paper' in config.json
    demo  -- DLL disables bot entirely and fires Telegram; profit target flips to paper

    Returns:
        (triggered: bool, reason: str)
    """

    mode = config.get("mode", "paper")
    if mode == "paper":
        return False, ""

    # If the mode changed since the limit was triggered (e.g., demo → live),
    # reset so the new mode starts with a fresh daily count.
    if (
        bot_state.limit_triggered
        and bot_state.pre_limit_mode
        and bot_state.pre_limit_mode != mode
    ):
        bot_state.limit_triggered = False
        bot_state.limit_reason = ""
        bot_state.pre_limit_mode = None
        log.info(f"Mode changed to '{mode}' - resetting daily limit state.")

    pnl = await db_get_today_pnl(mode)

    if pnl < 0 and abs(pnl) >= config.get("daily_loss_limit_dollars", 20):
        if not bot_state.limit_triggered:
            bot_state.limit_triggered = True
            bot_state.limit_reason = "daily loss limit reached"
            bot_state.pre_limit_mode = mode
            cfg = read_config()
            if mode == "demo":
                cfg["bot_enabled"] = False
                write_config(cfg)
                log.warning(f"Demo DLL hit (${pnl:.2f}). Bot disabled.")
                await send_telegram(
                    f"<b>[DEMO] Daily loss limit -- bot disabled</b>\n"
                    f"PnL today: <b>${pnl:.2f}</b>\n"
                    f"Bot has been disabled. Re-enable manually in config."
                )
            else:
                cfg["mode"] = "paper"
                write_config(cfg)
                log.warning(f"Daily loss limit hit (${pnl:.2f}). Switched to paper mode.")
                await send_telegram(
                    f"<b>Daily loss limit triggered</b>\n"
                    f"PnL today: <b>${pnl:.2f}</b>\n"
                    f"Switched to paper mode."
                )
        return True, bot_state.limit_reason

    if pnl > 0 and pnl >= config.get("daily_profit_target_dollars", 50):
        if not bot_state.limit_triggered:
            bot_state.limit_triggered = True
            bot_state.limit_reason = "daily profit target reached"
            bot_state.pre_limit_mode = mode
            cfg = read_config()
            cfg["mode"] = "paper"
            write_config(cfg)
            log.info(f"Daily profit target hit (${pnl:.2f}). Switched to paper mode.")
        return True, bot_state.limit_reason

    return False, ""


def midnight_reset() -> None:
    """
    Reset daily-limit state at UTC midnight.
    Restores the pre-limit mode if limits had previously triggered.
    """

    today = datetime.now(timezone.utc).date()
    if bot_state.daily_reset_date is None:
        bot_state.daily_reset_date = today
        return

    if today > bot_state.daily_reset_date:
        bot_state.daily_reset_date = today
        log.info("Midnight UTC: resetting daily limits.")
        if bot_state.limit_triggered and bot_state.pre_limit_mode:
            cfg = read_config()
            cfg["mode"] = bot_state.pre_limit_mode
            write_config(cfg)
            log.info(f"Restored mode to '{bot_state.pre_limit_mode}' after midnight reset.")
        bot_state.limit_triggered = False
        bot_state.limit_reason = ""
        bot_state.pre_limit_mode = None


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------

def _parse_strike_from_ticker(ticker):
    """Parse strike price out of a Kalshi ticker.

    Hourly tickers use `-T<strike>` suffix; 15-minute tickers use `-<strike>`.
    Returns None if no pattern matches or ticker is falsy.
    """
    if not ticker:
        return None
    m = _STRIKE_RE_T_SUFFIX.search(ticker)
    if m:
        return int(m.group(1))
    m = _STRIKE_RE_NUMERIC_SUFFIX.search(ticker)
    if m:
        return int(m.group(1))
    return None

async def write_state_file(
    config: dict,
    market: dict | None,
    phase: str,
    secs_left: float,
    btc_price: float | None,
    score: int,
    breakdown: dict,
    action: str,
    skip_reason: str,
) -> None:
    """Write a JSON snapshot of current bot state for server.py to serve."""
    state = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "market_ticker": market.get("ticker", "") if market else "",
        "market_title": market.get("title", "") if market else "",
        "phase": phase,
        "seconds_remaining": secs_left,
        "btc_price": btc_price,
        "confidence_score": score,
        "confidence_breakdown": breakdown,
        "reward_tier": bot_state._brain_cal_s2["reward_tier"],
        "brain_wr": bot_state._brain_cal_s2["overall_wr"],
        "brain_min_edge": bot_state._brain_cal_s2["min_edge_override"],
        "brain_n": bot_state._brain_cal_s2["last_count"],
        "bot_state.last_action": action,
        "bot_state.last_skip_reason": skip_reason,
        "mode": config.get("mode", "paper"),
        "today_live_pnl": await db_get_today_pnl("live"),
        "today_paper_pnl": await db_get_today_pnl("paper"),
        "today_demo_pnl": await db_get_today_pnl("demo"),
        "config": {**config,
                   "min_ev_pct": round(config.get("min_ev_base", 3.0)),
                   "vol_gate_thresh": config.get("vol_gate_thresh", 1.80)},
        "bot_state.limit_triggered": bot_state.limit_triggered,
        "bot_state.limit_reason": bot_state.limit_reason,
        "open_position": bot_state.current_position,
        "consecutive_losses": bot_state._consecutive_losses,
    }

    assets_snap: dict = {}
    for _a, _st in bot_state._asset_states.items():
        _m  = _st.get("market")
        _sl = seconds_remaining(_m) if _m else 0
        _ev = _st.get("eval", {})
        _a_phase = _st.get("phase", "DONE")
        _a_status = "TRADING" if _a_phase == "LOCKED" else (_ev.get("status") or _a_phase)
        _a_ticker = _m.get("ticker", "") if _m else ""
        try:
            _a_elapsed_sec = seconds_elapsed(_m) if _m else 0.0
        except Exception:
            _a_elapsed_sec = 0.0
        _a_duration_min = (float(_a_elapsed_sec) + float(_sl)) / 60.0 if _m else 0.0
        _a_session_type = "15m"
        _a_strategy_name = _strategy_name_for(_a, _a_duration_min)
        _a_strike = _parse_strike_from_ticker(_a_ticker)
        if _a_strike is None:
            _a_strike = _ev.get("strike")
            if _a_strike is None and _m:
                _a_strike = _m.get("strike_price")
        _a_window_phase = _phase_for_eth(_a, _a_elapsed_sec)
        assets_snap[_a] = {
            "price":        _am_get_price(_a),
            "phase":        _a_phase,
            "ticker":       _a_ticker,
            "market_title": _m.get("title",  "") if _m else "",
            "secs_left":    _sl,
            "price_age":    _am_price_age(_a),
            "strike":       _a_strike,
            "distance_pct": _ev.get("distance_pct"),
            "direction":    _ev.get("direction"),
            "yes_ask":      _ev.get("yes_ask"),
            "no_ask":       _ev.get("no_ask"),
            "ev":           _ev.get("ev"),
            "win_prob":     _ev.get("win_prob"),
            "status":       _a_status,
            "skip_reason":  _ev.get("skip_reason"),
            "vol_ratio":   _ev.get("vol_ratio"),
            "ch24":        _am_get_24h_change(_a),
            "signals":      _ev.get("signals", {}),
            "position":     _st.get("position"),
            "session_type": _a_session_type,
            "strategy_name": _a_strategy_name,
            "phase_label":  _a_window_phase,
            "window_phase": _a_window_phase,
        }
    _btc_ev = bot_state._asset_eval.get("BTC", {})
    _btc_status = "TRADING" if phase == "LOCKED" else (_btc_ev.get("status") or phase)
    _btc_ticker = market.get("ticker", "") if market else ""
    try:
        _btc_elapsed_sec = seconds_elapsed(market) if market else 0.0
    except Exception:
        _btc_elapsed_sec = 0.0
    _btc_duration_min = (float(_btc_elapsed_sec) + float(secs_left)) / 60.0 if market else 0.0
    _btc_session_type = "h1" if _btc_duration_min > 30 else "15m"
    _btc_strategy_name = _strategy_name_for("BTC", _btc_duration_min)
    _btc_strike = _parse_strike_from_ticker(_btc_ticker)
    if _btc_strike is None:
        _btc_strike = _btc_ev.get("strike")
        if _btc_strike is None and market:
            _btc_strike = market.get("strike_price")
    _btc_window_phase = _phase_for_eth("BTC", _btc_elapsed_sec)
    assets_snap["BTC"] = {
        "price":        btc_price,
        "phase":        phase,
        "ticker":       _btc_ticker,
        "market_title": market.get("title",  "") if market else "",
        "secs_left":    secs_left,
        "price_age":    _am_price_age("BTC"),
        "strike":       _btc_strike,
        "distance_pct": _btc_ev.get("distance_pct"),
        "direction":    _btc_ev.get("direction"),
        "yes_ask":      _btc_ev.get("yes_ask"),
        "no_ask":       _btc_ev.get("no_ask"),
        "ev":           _btc_ev.get("ev"),
        "win_prob":     _btc_ev.get("win_prob"),
        "status":       _btc_status,
        "skip_reason":  skip_reason or _btc_ev.get("skip_reason", ""),
        "vol_ratio":   _btc_ev.get("vol_ratio"),
        "ch24":        _am_get_24h_change("BTC"),
        "signals":      _btc_ev.get("signals", {}),
        "position":     bot_state.current_position,
        "session_type": _btc_session_type,
        "strategy_name": _btc_strategy_name,
        "phase_label":  _btc_window_phase,
        "window_phase": _btc_window_phase,
    }
    state["assets"] = assets_snap

    try:
        atomic_write_json(state, bot_state._STATE_FILE)
    except Exception as exc:
        log.error(f"State file write error: {exc}")


async def _log_entry(
    market: dict,
    phase: str,
    secs_left: float,
    btc_price: float,
    strike: float,
    contract_price: int | None,
    score: int,
    action: str,
    skip_reason: str,
    mode: str,
) -> None:
    """Write one row to market_log."""
    await db_write_market_log({
        "ts": datetime.now(timezone.utc).isoformat(),
        "market_id": market.get("ticker", ""),
        "market_title": market.get("title", ""),
        "phase": phase,
        "seconds_left": int(secs_left),
        "btc_price": btc_price,
        "strike": strike,
        "contract_price_cents": contract_price,
        "confidence_score": score,
        "action": action,
        "skip_reason": skip_reason,
        "mode": mode,
    })


# ---------------------------------------------------------------------------
# Trade execution (absorbed from bot_trade)
# ---------------------------------------------------------------------------

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
        if not brain_s1.get("price_filter_skip"):
            log.debug("[S1] %s: skip — %s", ticker, brain_s1.get("reasoning", "no_reason"))
        return
    if ticker in bot_state._s1_pending_trades:
        return

    side = brain_s1.get("side", "yes")
    _s1_allowed = get_asset_config(config, asset, "allowed_sides", config.get("allowed_sides"))
    if _s1_allowed and side not in _s1_allowed:
        return
    if side == "no":
        _min_no_ask = float(get_asset_config(config, asset, "min_no_ask_cents",
                                             config.get("min_no_ask_cents", 10.0)))
        if no_ask < _min_no_ask:
            log.info(f"[S1] {ticker}: no_ask {no_ask:.0f}c below floor {_min_no_ask:.0f}c — skip")
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
        "entry_signals":        json.dumps(brain_s1.get("signals", {})),
        "strategy_variant":     "strategy1",
        "strategy_version":     bot_state._S1_VERSION,
    }
    bot_state._s1_pending_trades[ticker] = {
        "trade_id":          None,
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
        log.critical(f"[S1] {ticker}: DB write failed -- position tracked in-memory only; reconcile manually")
    else:
        bot_state._s1_pending_trades[ticker]["trade_id"] = trade_id

    mode_icon = {"paper": "[PAPER]", "demo": "[DEMO]"}.get(mode, "[LIVE]")
    _win_pct  = int(win_prob * 100)
    _payout   = round((100 - fill_price) * contracts / 100, 2)
    _cost     = round(fill_price * contracts / 100, 2)
    log.info(f"[S1] {ticker}: ORDER FILLED -- {side.upper()} {contracts}x @ {fill_price}c")
    await send_telegram(
        f"<b>[S1 Original] {asset} {mode_icon} ORDER FILLED</b>\n"
        f"<b>{side.upper()} -- {'UP' if side == 'yes' else 'DOWN'}</b>  {contracts} contracts @ <b>{fill_price}c</b>\n"
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
    outcome_str = "WIN" if outcome == "win" else "LOSS"
    pct_str     = f"+{profit_pct:.0f}%" if profit_pct >= 0 else f"{profit_pct:.0f}%"
    mode_icon   = {"paper": "[PAPER]", "demo": "[DEMO]"}.get(s1_pos["mode"], "[LIVE]")
    _time_str   = datetime.now(timezone(timedelta(hours=-7))).strftime("%b %d %I:%M %p PST")
    _now        = time.time()
    _dur_secs   = int(_now - s1_pos.get("entry_ts", _now))
    _dur_str    = f"{_dur_secs // 60}m {_dur_secs % 60}s"
    await send_telegram(
        f"<b>[S1 Original] {asset} {mode_icon} {outcome_str}  {pnl_str}  ({pct_str})</b>  --  {_time_str}\n"
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


# ---------------------------------------------------------------------------
# Preflight checks (absorbed from bot_preflight)
# ---------------------------------------------------------------------------

async def verify_kalshi_connection(session: aiohttp.ClientSession) -> None:
    """Verify Kalshi credentials work and log all available BTC market series."""
    balance_path = "/portfolio/balance"
    try:
        async with session.get(
            bot_state.KALSHI_BASE_URL + balance_path,
            headers=kalshi_headers("GET", balance_path),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json()
            if resp.status == 401:
                log.error("KALSHI AUTH FAILED (401) -- check KALSHI_API_KEY and KALSHI_PRIVATE_KEY")
                sys.exit(1)
            if resp.status != 200:
                log.error(f"Kalshi connection check failed: HTTP {resp.status} -- {data}")
                sys.exit(1)
            balance = data.get("balance", "?")
            log.info(f"Kalshi auth OK. Account balance: {balance} cents")
    except SystemExit:
        raise
    except Exception as exc:
        log.error(f"Kalshi connection check failed: {exc}")
        sys.exit(1)

    path = "/markets"

    now_utc = datetime.now(timezone.utc)
    log.info("=== KALSHI MARKET DISCOVERY START ===")

    for series in ("KXBTCD", "BTCD-B", "KXBTC15M", "KXBTC", "BTC15M", "BTC", "BTCUSD", "KXBTCUSD", "KXBTCUSD15M"):
        try:
            async with session.get(
                bot_state.KALSHI_BASE_URL + path,
                headers=kalshi_headers("GET", path),
                params={"series_ticker": series, "status": "open", "limit": 20},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                d = await resp.json()
                markets = d.get("markets", [])
                log.info(f"  series={series!r} -> {len(markets)} markets")
                for m in markets[:5]:
                    try:
                        close_dt = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
                        mins_left = (close_dt - now_utc).total_seconds() / 60
                        open_dt   = datetime.fromisoformat(m.get("open_time","").replace("Z", "+00:00"))
                        duration  = (close_dt - open_dt).total_seconds() / 60
                    except Exception:
                        mins_left = duration = -1
                    log.info(f"    ticker={m.get('ticker')} closes_in={mins_left:.1f}m dur={duration:.0f}m title={m.get('title','')[:60]}")
        except Exception as exc:
            log.info(f"  series={series!r} -> ERROR: {exc}")

    for scan_series in ("KXBTCD", "BTCD-B", "KXBTC", "KXBTC15M", "BTC"):
        try:
            async with session.get(
                bot_state.KALSHI_BASE_URL + path,
                headers=kalshi_headers("GET", path),
                params={"status": "open", "series_ticker": scan_series, "limit": 100},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    log.info(f"  scan series={scan_series!r} -> HTTP {resp.status}")
                    continue
                d = await resp.json()
                all_short = d.get("markets", [])
            log.info(f"  scan series={scan_series!r} -> {len(all_short)} markets")
            for m in all_short[:10]:
                try:
                    close_dt = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
                    mins_left = (close_dt - now_utc).total_seconds() / 60
                    open_dt   = datetime.fromisoformat(m.get("open_time","").replace("Z", "+00:00"))
                    duration  = (close_dt - open_dt).total_seconds() / 60
                except Exception:
                    mins_left = duration = -1
                log.info(f"    ticker={m.get('ticker')} closes_in={mins_left:.1f}m dur={duration:.0f}m title={m.get('title','')[:60]}")
        except Exception as exc:
            log.info(f"  scan series={scan_series!r} ERROR: {exc}")

    try:
        async with session.get(
            bot_state.KALSHI_BASE_URL + "/series",
            headers=kalshi_headers("GET", "/series"),
            params={"limit": 200},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                d = await resp.json()
                all_series = d.get("series", [])
                btc_series = [s for s in all_series
                              if "btc" in s.get("ticker","").lower()
                              or "bitcoin" in s.get("title","").lower()
                              or "btc" in s.get("title","").lower()]
                log.info(f"  /series: {len(all_series)} total, {len(btc_series)} BTC-related")
                for s in btc_series:
                    log.info(f"    series_ticker={s.get('ticker')} title={s.get('title','')[:60]}")
            else:
                log.info(f"  /series returned HTTP {resp.status}")
    except Exception as exc:
        log.info(f"  /series ERROR: {exc}")

    log.info("=== KALSHI MARKET DISCOVERY END ===")


async def run_preflight_checks(config: dict) -> None:
    """
    Runs before first trade. Prints warnings and blocks live trading
    if critical validations haven't been completed.

    LIVE mode + unresolved issues  -> sys.exit(1). Hard stop.
    PAPER mode + unresolved issues -> warn and continue (bot must run to collect data).
    preflight_override: true in config.json  -> skip the live-mode block (NOT RECOMMENDED).
    """
    issues: list[str] = []
    W = 60

    if not os.path.isfile(bot_state._PRICE_VAL_CSV):
        issues.append(
            "NO PRICE VALIDATION DATA -- price_validation_log.csv does not exist. "
            "Run paper mode for 200+ cycles first."
        )
    else:
        try:
            with open(bot_state._PRICE_VAL_CSV, encoding="utf-8") as _f:
                row_count = max(0, sum(1 for _ in _f) - 1)
        except Exception:
            row_count = 0
        if row_count < 200:
            issues.append(
                f"INSUFFICIENT PRICE VALIDATION -- only {row_count}/200 samples collected. "
                "Keep running paper mode."
            )

    fee = config.get("kalshi_fee_per_contract_cents", 0)
    if not (isinstance(fee, (int, float)) and fee > 0):
        issues.append(
            f"FEE NOT CONFIGURED -- kalshi_fee_per_contract_cents={fee!r}. "
            "Set to 7 (Kalshi charges 7c/contract)."
        )

    dll = config.get("daily_loss_limit_dollars", 999999)
    if dll > 500:
        issues.append(
            f"DAILY LOSS LIMIT TOO HIGH -- currently ${dll}. "
            "Set to a realistic value (e.g. $50)."
        )

    mode      = config.get("mode", "paper")
    is_live   = mode == "live"
    override  = bool(config.get("preflight_override", False))

    if is_live and issues:
        print("=" * W)
        print("LIVE TRADING BLOCKED -- PRE-FLIGHT CHECK FAILED")
        print("=" * W)
        for issue in issues:
            print(f"  [FAIL] {issue}")
        print()
        print("Switch to paper mode or resolve these issues before trading live.")
        print("To override (NOT RECOMMENDED): set preflight_override: true in config.json")
        print("=" * W)
        await send_telegram(
            "<b>LIVE TRADING BLOCKED -- pre-flight failed</b>\n"
            + "\n".join(f"- {i}" for i in issues)
            + "\nResolve all issues before retrying live mode."
        )
        if not override:
            sys.exit(2)
        else:
            log.warning("PRE-FLIGHT OVERRIDE ACTIVE -- proceeding into live mode despite failures. "
                        "This is NOT recommended.")
            print()
            print("  *** OVERRIDE ACTIVE -- LIVE MODE STARTING ANYWAY ***")
            print("  *** THIS IS NOT RECOMMENDED. YOU WERE WARNED.   ***")
            print("=" * W)

    elif issues:
        print("=" * W)
        print("PRE-FLIGHT WARNINGS (paper mode -- not blocking)")
        print("=" * W)
        for issue in issues:
            print(f"  [WARN] {issue}")
        print("=" * W)

    else:
        log.info("=" * W)
        log.info(f"  Pre-flight: PASS -- {mode.upper()} mode.  "
                 f"fee={fee}c  dll=${dll}  "
                 f"reversal={'ON' if config.get('enable_reversal_signal') else 'OFF'}")
        log.info("=" * W)
