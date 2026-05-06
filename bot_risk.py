"""bot_risk.py — Daily limits, midnight reset, state file, strike parser."""
import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta

import bot_state
from bot_db import db_get_today_pnl, db_write_market_log
from bot_config import atomic_write_json, read_config, write_config
from bot_notify import send_telegram, _phase_for_eth
from bot_kalshi import seconds_remaining, seconds_elapsed
from bot_strategy import _session_ev_adjustment, _strategy_name_for
from asset_manager import (
    get_price           as _am_get_price,
    price_age_seconds   as _am_price_age,
)

log = logging.getLogger("bot")

_STRIKE_RE_T_SUFFIX       = re.compile(r"-T(\d+)$")
_STRIKE_RE_NUMERIC_SUFFIX = re.compile(r"-(\d+)$")


# ══════════════════════════════════════════════════════════════════════════════
#  Daily limits
# ══════════════════════════════════════════════════════════════════════════════

async def check_daily_limits(config: dict) -> tuple[bool, str]:
    """
    Check daily loss limit and profit target for live/demo mode.

    live  — DLL/profit target flips mode to 'paper' in config.json
    demo  — DLL disables bot entirely and fires Telegram; profit target flips to paper

    Returns:
        (triggered: bool, reason: str)
    """

    mode = config.get("mode", "paper")
    if mode == "paper":
        return False, ""

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
                    f"<b>[DEMO] Daily loss limit — bot disabled</b>\n"
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


# ══════════════════════════════════════════════════════════════════════════════
#  State file
# ══════════════════════════════════════════════════════════════════════════════

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
                   "min_ev_pct": round((config.get("min_ev_base", 3.0) / 100.0 + _session_ev_adjustment()) * 100),
                   "vol_gate_thresh": config.get("vol_gate_thresh", 1.80)},
        "bot_state.limit_triggered": bot_state.limit_triggered,
        "bot_state.limit_reason": bot_state.limit_reason,
        "open_position": bot_state.current_position,
        "consecutive_losses": bot_state._consecutive_losses,
    }

    # Per-asset snapshot for multi-asset dashboard display
    assets_snap: dict = {}
    # Non-BTC assets — pulled from the in-memory bot_state._asset_states dict
    for _a, _st in bot_state._asset_states.items():
        _m  = _st.get("market")
        _sl = seconds_remaining(_m) if _m else 0
        _ev = _st.get("eval", {})
        _a_phase = _st.get("phase", "DONE")
        _a_status = "TRADING" if _a_phase == "LOCKED" else (_ev.get("status") or _a_phase)
        # Dashboard-extension fields (session_type / strategy_name / strike / phase).
        # Market duration is derived from open_time/close_time when available, and
        # falls back to elapsed+remaining when the timestamps are missing.
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
            "signals":      _ev.get("signals", {}),
            "position":     _st.get("position"),
            "session_type": _a_session_type,
            "strategy_name": _a_strategy_name,
            "phase_label":  _a_window_phase,
            "window_phase": _a_window_phase,
        }
    # BTC — uses separate globals; bot_state._asset_eval["BTC"] holds last eval snapshot
    _btc_ev = bot_state._asset_eval.get("BTC", {})
    _btc_status = "TRADING" if phase == "LOCKED" else (_btc_ev.get("status") or phase)
    _btc_ticker = market.get("ticker", "") if market else ""
    try:
        _btc_elapsed_sec = seconds_elapsed(market) if market else 0.0
    except Exception:
        _btc_elapsed_sec = 0.0
    _btc_duration_min = (float(_btc_elapsed_sec) + float(secs_left)) / 60.0 if market else 0.0
    _btc_session_type = "15m"
    _btc_strategy_name = _strategy_name_for("BTC", _btc_duration_min)
    _btc_strike = _parse_strike_from_ticker(_btc_ticker)
    if _btc_strike is None:
        _btc_strike = _btc_ev.get("strike")
        if _btc_strike is None and market:
            _btc_strike = market.get("strike_price")
    _btc_window_phase = _phase_for_eth("BTC", _btc_elapsed_sec)  # always None for BTC
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
