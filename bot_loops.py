"""bot_loops.py — Phase handlers, asset loop, main trading loop."""
__all__ = ["handle_ready_phase", "handle_locked_phase", "main_loop"]

import asyncio
import json
import logging
import math
import time
from datetime import datetime, timezone, timedelta

import aiohttp

import bot_state
import asset_manager
from asset_manager import get_price as _am_get_price, price_age_seconds as _am_price_age
from bot_infra import read_config, get_asset_config, db_write_trade, db_update_trade, send_telegram, _notify_ctx, _phase_for_eth
from bot_market import (
    fetch_current_market, fetch_market_for_asset, fetch_orderbook,
    seconds_remaining, seconds_elapsed, parse_strike, get_btc_price,
    kalshi_headers, _simulated_amm_midpoint, _log_price_validation,
    calculate_contracts, implied_prob, place_order,
)
from bot_strategy import (
    strategy_brain_s1, strategy_brain_s2,
    track_contract_price, _session_ev_adjustment, _strategy_name_for,
)
from bot_risk import (
    check_daily_limits, midnight_reset, write_state_file, _log_entry,
    _execute_s1_trade, _settle_s1_trade, _try_settle_orphaned_s1,
)

log = logging.getLogger("bot")


async def handle_ready_phase(
    session: aiohttp.ClientSession,
    config: dict,
    market: dict,
    ticker: str,
    btc_price: float,
    secs_left: float,
    strike: float,
    elapsed: float,
    asset: str = "BTC",
    state: dict | None = None,
) -> None:
    """
    Evaluate entry conditions for one READY-phase iteration.
    Advances to LOCKED on a successful fill, or logs the skip reason.

    asset: which asset this market is for ("BTC", "ETH", etc.)
    state: per-asset state dict (non-BTC); mutations go here instead of globals.
           Must contain keys: "phase", "position", "order_attempted" (set).
    """

    _use_state = state is not None
    mode = config.get("mode", "paper")

    # Hard expiry gate — truly nothing to do in the last 90 seconds
    if secs_left < 90:
        log.info(f"{ticker}: < 90s remaining. Moving to DONE.")
        if _use_state: state["phase"] = "DONE"
        else: bot_state.current_phase = "DONE"
        return

    # Early-window gate — skip first 90s while price is still anchoring
    _elapsed = seconds_elapsed(market)
    if _elapsed < 90:
        log.debug(f"{ticker}: {_elapsed:.0f}s elapsed — price anchoring, skipping")
        return

    # ── Multi-window best-pick (BTC only) ────────────────────────────────────────────────────────────────
    # If multiple 15-min windows are open simultaneously, evaluate all of them
    # and trade the one with the highest EV. Falls back to primary market if
    # only one window is open or fetching alternatives fails.
    # Non-BTC assets use a single market per cycle (no multi-window support).
    if asset == "BTC":
        try:
            all_windows = await fetch_current_market(session, return_all=True)
            if isinstance(all_windows, list) and len(all_windows) > 1:
                log.info(f"Multi-window: {len(all_windows)} open windows — evaluating all for best EV.")
                best_market  = market
                best_ticker  = ticker
                best_ev      = None
                best_ob      = None
                best_strike  = strike
                for candidate in all_windows:
                    try:
                        c_ticker   = candidate.get("ticker", "")
                        c_strike   = parse_strike(candidate)
                        if c_strike is None:
                            continue
                        # Use each window's own timing — they may have different close times
                        c_secs_left = seconds_remaining(candidate)
                        c_elapsed   = seconds_elapsed(candidate)
                        # Skip windows that are too close to expiry — same gate as primary market.
                        # Without this, the multi-window picker can select a market with 40s left
                        # AFTER the 90s gate already passed for the primary market.
                        if c_secs_left < 90:
                            log.info(f"  Window {c_ticker}: skipping — only {c_secs_left:.0f}s left")
                            continue
                        c_ob = await fetch_orderbook(session, c_ticker, candidate)
                        if c_ob is None:
                            continue
                        c_brain = strategy_brain_s2(
                            btc_price, c_strike,
                            c_ob["best_yes_ask"], c_ob["best_no_ask"],
                            c_elapsed, c_secs_left, c_ticker,
                            min_ev_base=get_asset_config(config, asset, "min_ev_base", 3.0),
                            vol_gate_thresh=get_asset_config(config, asset, "vol_gate_thresh", 1.80),
                            kalshi_fee=config.get("kalshi_fee_per_contract_cents", 7) / 100,
                            max_entry_price_cents=get_asset_config(config, asset, "max_entry_price_cents", 100.0),
                            min_reward_cents=get_asset_config(config, asset, "min_reward_cents", 0.0),
                            max_risk_reward_ratio=get_asset_config(config, asset, "max_risk_reward_ratio", 999.0),
                            asset=asset,
                        )
                        c_win_prob = c_brain.get("win_prob", 0.5)
                        c_entry    = c_ob["best_yes_ask"] if c_brain["side"] == "yes" else c_ob["best_no_ask"]
                        _c_fee_rate = config.get("kalshi_fee_per_contract_cents", 7) / 100
                        _c_p        = c_entry / 100.0
                        _c_fee      = _c_fee_rate * (1.0 - _c_p)
                        c_ev        = c_win_prob - _c_p - _c_fee
                        log.info(f"  Window {c_ticker}: ev={c_ev:+.1%} side={c_brain['side']} strike=${c_strike:,.0f}")
                        if best_ev is None or c_ev > best_ev:
                            best_ev     = c_ev
                            best_market = candidate
                            best_ticker = c_ticker
                            best_ob     = c_ob
                            best_strike = c_strike
                    except Exception as _exc:
                        log.warning(f"  Multi-window eval error for {candidate.get('ticker','?')}: {_exc}")
                if best_ticker != ticker:
                    log.info(f"Multi-window: switching to {best_ticker} (EV {best_ev:+.1%}) over {ticker}")
                    market   = best_market
                    ticker   = best_ticker
                    strike   = best_strike
                    secs_left = seconds_remaining(best_market)
                    elapsed   = seconds_elapsed(best_market)
                    bot_state.current_market = best_market
                ob = best_ob
            else:
                ob = None  # fetch below
        except Exception as _mw_exc:
            log.warning(f"Multi-window evaluation error: {_mw_exc}")
            ob = None
    else:
        ob = None  # non-BTC: no multi-window, orderbook fetched below

    # Orderbook — retry next cycle if temporarily unavailable
    def _no_data_eval(reason: str) -> dict:
        return {
            "strike":       strike,
            "distance_pct": round(abs(btc_price - strike) / strike * 100, 3) if strike else None,
            "direction":    None,
            "yes_ask":      None,
            "no_ask":       None,
            "ev":           None,
            "win_prob":     None,
            "status":       "NO_DATA",
            "skip_reason":  reason,
            "signals":      {},
        }

    if ob is None:
        try:
            ob = await fetch_orderbook(session, ticker, market)
        except Exception as exc:
            log.error(f"[{asset}] Orderbook error in READY: {exc}")
            _snap = _no_data_eval(f"orderbook error: {exc}")
            if _use_state: state["eval"] = _snap
            else: bot_state._asset_eval[asset] = _snap
            return

    if ob is None:
        log.warning(f"[{asset}] {ticker}: orderbook returned no price data — retrying next cycle")
        _snap = _no_data_eval("no orderbook data — retrying")
        if _use_state: state["eval"] = _snap
        else: bot_state._asset_eval[asset] = _snap
        bot_state.last_action, bot_state.last_skip_reason = "watching", "no price data — retrying"
        return

    yes_ask = ob["best_yes_ask"]
    no_ask  = ob["best_no_ask"]   # fetched directly from no_ask_dollars, not derived

    # ── Price validation: compare simulated vs real prices ──────────────────────────
    # Logs to price_validation_log.csv so we can audit whether the backtest's
    # AMM simulation matches live Kalshi prices (reviewer flagged 8-15c gap risk).
    try:
        _sim_yes, _sim_no = _simulated_amm_midpoint(btc_price, strike)
        _log_price_validation(
            ts=datetime.now(timezone.utc).isoformat(),
            ticker=ticker,
            btc_price=btc_price,
            strike=strike,
            sim_yes=_sim_yes,
            sim_no=_sim_no,
            real_yes=yes_ask,
            real_no=no_ask,
            mins_remaining=secs_left / 60,
        )
    except Exception as _pv_exc:
        log.debug(f"Price validation log error: {_pv_exc}")

    # Track YES price for velocity signal
    track_contract_price(ticker, yes_ask)

    # ── Printer Brain — primary decision engine (always runs, no API needed) ──
    brain = strategy_brain_s2(btc_price, strike, yes_ask, no_ask, elapsed, secs_left, ticker,
                     min_ev_base=get_asset_config(config, asset, "min_ev_base", 3.0),
                     vol_gate_thresh=get_asset_config(config, asset, "vol_gate_thresh", 1.80),
                     kalshi_fee=config.get("kalshi_fee_per_contract_cents", 7) / 100,
                     max_entry_price_cents=get_asset_config(config, asset, "max_entry_price_cents", 100.0),
                     min_reward_cents=get_asset_config(config, asset, "min_reward_cents", 0.0),
                     max_risk_reward_ratio=get_asset_config(config, asset, "max_risk_reward_ratio", 999.0),
                     asset=asset)
    brain_s1 = strategy_brain_s1(btc_price, strike, yes_ask, no_ask, elapsed, secs_left, ticker, asset=asset)


    await _execute_s1_trade(
        session, brain_s1, ticker, btc_price, strike, yes_ask, no_ask,
        elapsed, secs_left, asset, config, mode, ob, market,
    )
    side     = brain["side"]
    score    = brain["confidence"]
    do_trade = brain["action"] == "trade"
    skip_reason_ai = brain["reasoning"]

    # ── allowed_sides gate — disable NO side when model is uncalibrated ──────
    _side_aliases = {"up": "yes", "down": "no"}
    side = _side_aliases.get(side.lower(), side.lower()) if side else side
    _allowed_sides = get_asset_config(config, asset, "allowed_sides", config.get("allowed_sides"))
    _allowed_norm  = [_side_aliases.get(s.lower(), s.lower()) for s in (_allowed_sides or [])]
    if do_trade and _allowed_norm and side not in _allowed_norm:
        skip_reason_ai = f"side={side} not in allowed_sides={_allowed_sides}"
        do_trade = False

    # ── no_ask floor — block NO entries on nearly-resolved contracts ──────────
    if do_trade and side == "no":
        _min_no_ask = float(get_asset_config(config, asset, "min_no_ask_cents",
                                             config.get("min_no_ask_cents", 10.0)))
        if no_ask < _min_no_ask:
            skip_reason_ai = f"no_ask {no_ask:.0f}c below floor {_min_no_ask:.0f}c"
            do_trade = False

    # ── Consecutive price-filter skip tracking ────────────────────────────────────────────────────
    if brain.get("price_filter_skip"):
        bot_state._consecutive_price_skips += 1
        if bot_state._consecutive_price_skips == 20:
            _max_ep = get_asset_config(config, asset, "max_entry_price_cents", 82)
            log.warning(
                f"Price filter: {bot_state._consecutive_price_skips} consecutive skips — "
                f"all entry prices > {_max_ep}c"
            )
    else:
        bot_state._consecutive_price_skips = 0

    entry_price_cents = yes_ask if side == "yes" else no_ask
    _fee_rate = config.get("kalshi_fee_per_contract_cents", 7) / 100
    _entry_p  = entry_price_cents / 100.0
    _fee      = _fee_rate * (1.0 - _entry_p)  # fee/stake ≈ fee_rate*(1-p); matches ev.py formula
    brain_ev  = brain.get("win_prob", 0.5) - _entry_p - _fee
    brain_win_prob = brain.get("win_prob", 0.5)

    # Dashboard eval snapshot — updated at every exit point below
    _eval_snap = {
        "strike":       strike,
        "distance_pct": round(abs(btc_price - strike) / strike * 100, 3) if strike else None,
        "direction":    "UP" if side == "yes" else "DOWN",
        "yes_ask":      yes_ask,
        "no_ask":       no_ask,
        "ev":           round(brain_ev * 100, 1),
        "win_prob":     round(brain_win_prob * 100, 1),
        "vol_ratio":    brain.get("_vol_ratio"),
        "status":       "WATCHING",
        "skip_reason":  "",
        "signals":      brain.get("signals", {}),
    }

    entry_price_cents = yes_ask if side == "yes" else no_ask

    # Dashboard breakdown from Brain v3 components
    win_p_raw   = 0.70  # Supertrend assumed probability
    _mom_label  = brain.get("mom_label",  "neutral")
    _vel_signal = brain.get("vel_signal", "neutral")
    _abs_pct    = brain.get("abs_pct", abs((btc_price - strike) / strike))
    # Time score: less time remaining = outcome more certain = higher score (0→20)
    _time_score = round(max(0.0, min(20.0, 20.0 * (1.0 - secs_left / (13 * 60)))), 1)
    # Distance score: farther from strike = higher score (0→30, caps at 0.5%)
    _dist_score = round(min(30.0, _abs_pct * 100.0 / 0.5 * 30.0), 1)
    _brain_rv       = brain.get("_rv")
    _brain_vol_ratio = brain.get("_vol_ratio")
    breakdown = {
        "win_prob_raw":   round(win_p_raw * 100, 1),
        "win_prob_final": round(brain.get("win_prob", win_p_raw) * 100, 1),
        "ev":             round((brain.get("win_prob", 0.5) - entry_price_cents / 100 - _fee) * 100, 1),
        "contract_c":     round(entry_price_cents, 1),
        "momentum":       30 if _mom_label in ("bullish", "bearish") else 0,
        "momentum_label": _mom_label,
        "velocity":       30 if _vel_signal == "favorable" else (10 if _vel_signal == "neutral" else 0),
        "velocity_label": _vel_signal,
        "time":           _time_score,
        "distance":       _dist_score,
        "distance_pct":   round(_abs_pct * 100, 3),
        "side":           side,
        "vol_per_min":    round(_brain_rv * 100, 4) if _brain_rv is not None else None,
        "vol_ratio":      round(_brain_vol_ratio, 3) if _brain_vol_ratio is not None else None,
    }

    bot_state.last_confidence_score = score
    bot_state.last_confidence_breakdown = breakdown

    raw_win_pct = int(brain.get("win_prob", 0) * 100)
    conf_threshold = int(get_asset_config(config, asset, "confidence_threshold", config.get("confidence_threshold", 65)))
    if do_trade and raw_win_pct < conf_threshold:
        skip_reason_ai = f"win prob {raw_win_pct}% below floor {conf_threshold}%"
        do_trade = False

    # ── Reversal model — runs whenever main strategy skips ──────────────────────────
    _is_reversal = False

    if not do_trade:
        log.info(f"{ticker}: watching — {skip_reason_ai}")
        await _log_entry(market, "READY", secs_left, btc_price, strike,
                         int(entry_price_cents), score, "skip", skip_reason_ai, mode)
        _eval_snap.update({"status": "SKIPPED", "skip_reason": skip_reason_ai})
        if _use_state: state["eval"] = dict(_eval_snap)
        else: bot_state._asset_eval[asset] = dict(_eval_snap)
        bot_state.last_action, bot_state.last_skip_reason = "watching", skip_reason_ai
        return

    # Daily limits — may flip mode to paper
    limit_hit, _ = await check_daily_limits(config)
    if limit_hit:
        config = read_config()
        mode = config.get("mode", "paper")

    # Cooldown disabled — trade every session regardless of prior outcome

    # Position sizing — flat fixed amount
    # Reversal trades use 50% of configured amount (contrarian = smaller size)
    trade_amount = config.get("trade_amount_dollars", 25)
    avail_liquidity = ob["yes_liquidity"] if side == "yes" else ob["no_liquidity"]
    contracts, dollars_used = calculate_contracts(
        trade_amount, int(entry_price_cents), avail_liquidity,
    )
    if contracts == 0 or dollars_used < float(trade_amount) * 0.90:
        if contracts > 0:
            reason = (
                f"insufficient_liquidity: only {avail_liquidity} contracts available "
                f"(${dollars_used:.2f} of ${float(trade_amount):.0f} target — skip partial fill)"
            )
        else:
            reason = "trade amount too small for current contract price"
        log.info(f"{ticker}: {reason}")
        await _log_entry(market, "READY", secs_left, btc_price, strike,
                         int(entry_price_cents), score, "skip", reason, mode)
        _eval_snap.update({"status": "SKIPPED", "skip_reason": reason})
        if _use_state: state["eval"] = dict(_eval_snap)
        else: bot_state._asset_eval[asset] = dict(_eval_snap)
        bot_state.last_action, bot_state.last_skip_reason = "skip", reason
        return

    # Place order — mark ticker as attempted BEFORE placing so re-entry is blocked
    # even if the bot crashes or fill_confirmed comes back False
    if _use_state: state["order_attempted"].add(ticker)
    else: bot_state._order_attempted_tickers.add(ticker)
    log.info(f"{ticker}: TRADE {side} {contracts}x @ {int(entry_price_cents)}c (score={score}, mode={mode})")
    result = await place_order(session, ticker, side, contracts, int(entry_price_cents), mode, market, asset=asset, secs_left=secs_left)

    fill_confirmed = result["fill_confirmed"]
    _fp = result.get("fill_price_cents")
    fill_price = _fp if _fp is not None else int(entry_price_cents)
    order_id = result.get("order_id")
    # Use actual filled contract count (IOC may fill fewer than requested).
    # Must use explicit None check — 0 is falsy but a valid (unfilled) count.
    _fc = result.get("filled_contracts")
    contracts = _fc if _fc is not None else contracts

    trade_ts = datetime.now(timezone.utc).isoformat()

    await _log_entry(
        market, "READY", secs_left, btc_price, strike, int(entry_price_cents),
        score, "trade" if fill_confirmed else "skip",
        "" if fill_confirmed else "order not filled",
        mode,
    )

    if not fill_confirmed:
        if _use_state: state["phase"] = "DONE"
        else: bot_state.current_phase = "DONE"
        _eval_snap.update({"status": "SKIPPED", "skip_reason": "order not filled"})
        if _use_state: state["eval"] = dict(_eval_snap)
        else: bot_state._asset_eval[asset] = dict(_eval_snap)
        bot_state.last_action, bot_state.last_skip_reason = "skip", "order not filled"
        log.info(f"{ticker}: order not filled. Moving to DONE.")
        return

    # Only write to trades DB when the order actually filled.
    # Unfilled attempts are recorded in market_log (via _log_entry above).
    trade_data = {
        "ts": trade_ts,
        "market_id": ticker,
        "market_title": market.get("title", ""),
        "mode": mode,
        "side": side,
        "contracts": contracts,
        "entry_price_cents": fill_price,
        "trade_amount_dollars": round(dollars_used, 2),
        "confidence_score": score,
        "model_prob": brain.get("win_prob", 0.5),
        "implied_prob": implied_prob(entry_price_cents),
        "btc_price_at_entry": btc_price,
        "strike": strike,
        "seconds_left_at_entry": int(secs_left),
        "fill_confirmed": 1,
        "exit_price_cents": None,
        "exit_reason": None,
        "outcome": "pending",
        "pnl_dollars": None,
        "profit_percent": None,
        "order_id":          order_id,
        "asset":             asset,
        "raw_p_yes":         brain.get("raw_p_yes"),
        "entry_signals":    json.dumps({
            "supertrend_direction": (brain.get("signals") or {}).get("supertrend_direction"),
            "supertrend_side":      (brain.get("signals") or {}).get("supertrend_side"),
            "market_prob":          (brain.get("signals") or {}).get("market_prob"),
            "p_ev":                 (brain.get("signals") or {}).get("p_ev"),
            "decision_mode":        (brain.get("signals") or {}).get("decision_mode"),
        }),
        "strategy_variant": "strategy2",
    }
    trade_id = await db_write_trade(trade_data)

    _entry_ts = time.time()
    _abs_pct_at_entry = abs(btc_price - strike) / strike
    _mins_left_at_entry = secs_left / 60
    # Record the market's total duration (elapsed + remaining at entry) so
    # exit-side notifications can reference the market's window length.
    try:
        _market_elapsed_at_entry = seconds_elapsed(market) if market else 0.0
    except Exception:
        _market_elapsed_at_entry = 0.0
    try:
        _market_duration_min = (_market_elapsed_at_entry + float(secs_left)) / 60.0
    except (TypeError, ValueError):
        _market_duration_min = 0.0
    _new_position = {
        "trade_id": trade_id,
        "ticker": ticker,
        "side": side,
        "contracts": contracts,
        "entry_price_cents": fill_price,
        "mode": mode,
        "strike": strike,
        "entry_ts": _entry_ts,
        "market_duration_min": _market_duration_min,
        "elapsed_at_entry": _market_elapsed_at_entry,  # used by exit-side _phase_for_eth
        "market_close_time": market.get("close_time", ""),
        "order_id": order_id,
        "asset": asset,
    }
    if _use_state:
        state["position"] = _new_position
        state["phase"] = "LOCKED"
    else:
        bot_state.current_position = _new_position
        bot_state.current_phase = "LOCKED"
    _eval_snap.update({"status": "TRADING", "skip_reason": ""})
    if _use_state: state["eval"] = dict(_eval_snap)
    else: bot_state._asset_eval[asset] = dict(_eval_snap)
    bot_state.last_action, bot_state.last_skip_reason = "trade", ""
    log.info(f"{ticker}: LOCKED.")
    mode_icon  = {"paper": "[PAPER]", "demo": "[DEMO]"}.get(mode, "[LIVE]")
    _win_prob_used = brain.get("win_prob", 0)
    _win_pct   = int(_win_prob_used * 100)
    _ev        = round((_win_prob_used - fill_price / 100 - _fee) * 100, 1)
    _ev_str    = f"+{_ev}%" if _ev >= 0 else f"{_ev}%"
    _payout    = round((100 - fill_price) * contracts / 100, 2)
    _cost      = round(fill_price * contracts / 100, 2)
    _time_str  = datetime.now(timezone(timedelta(hours=-7))).strftime("%b %d %I:%M %p PST")
    _expiry_dt = datetime.now(timezone(timedelta(hours=-7))) + timedelta(seconds=secs_left)
    _expiry_str = _expiry_dt.strftime("%I:%M %p PST")
    _strat_tag = "REVERSAL" if _is_reversal else "ORDER FILLED"
    _fill_ctx = _notify_ctx(
        asset, ticker, (elapsed + secs_left) / 60.0,
        _phase_for_eth(asset, elapsed),
    )
    await send_telegram(
        f"<b>🔵 [S2 D3 Hybrid] {_fill_ctx} {mode_icon} {_strat_tag}</b>  —  {_time_str}\n"
        f"<b>{side.upper()} — {'UP' if side == 'yes' else 'DOWN'}</b>  {contracts} contracts @ <b>{fill_price}c</b>\n"
        f"Cost: ${_cost:.2f}  |  Max payout: ${_payout:.2f}\n"
        f"Win prob: {_win_pct}%  |  EV: {_ev_str}\n"
        f"Strike: ${strike:,.0f}  |  {asset}: ${btc_price:,.0f}\n"
        f"Expires {int(secs_left // 60)}m {int(secs_left % 60)}s -> {_expiry_str}"
    )


async def handle_locked_phase(
    session: aiohttp.ClientSession,
    btc_price: float,
    secs_left: float,
    config: dict,
    asset: str = "BTC",
    state: dict | None = None,
) -> None:
    """
    Hold an open position to expiry — exit at settlement.
    Exit only when the market settles and fetch the official Kalshi result.
    secs_left is passed as a fallback; the position's stored close_time is
    used when available so market rollovers don't break expiry detection.

    asset: which asset this position is for ("BTC", "ETH", etc.)
    state: per-asset state dict (non-BTC); mutations go here instead of globals.
    """

    _use_state = state is not None
    _cur_pos = state["position"] if _use_state else bot_state.current_position

    if _cur_pos is None:
        log.warning("LOCKED phase with no position. Moving to DONE.")
        if _use_state: state["phase"] = "DONE"
        else: bot_state.current_phase = "DONE"
        return

    pos = _cur_pos
    ticker = pos["ticker"]
    strike = pos["strike"]

    # Compute secs_left from the position's stored market close time.
    # This is immune to market rollovers — the passed secs_left can be stale
    # (from a new market) when the old market has already expired.
    _stored_close = pos.get("market_close_time", "")
    if _stored_close:
        try:
            close_dt = datetime.fromisoformat(_stored_close.replace("Z", "+00:00"))
            secs_left = max(0.0, (close_dt - datetime.now(timezone.utc)).total_seconds())
        except Exception:
            pass  # fall back to caller-supplied secs_left

    # Expiry check
    if secs_left <= 0:
        # Ask Kalshi for the official settlement result — retry up to 6x (30s)
        # to give the exchange time to settle the market.
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
                log.warning(f"Market result fetch error (attempt {_attempt}): {_exc}")
            await asyncio.sleep(5)

        if market_result == "yes":
            outcome = "win" if pos["side"] == "yes" else "loss"
        elif market_result == "no":
            outcome = "win" if pos["side"] == "no" else "loss"
        else:
            # Kalshi didn't settle in time — fall back to BTC price comparison
            log.warning(f"{ticker}: settlement result unavailable, falling back to BTC price check")
            outcome = "win" if (
                (pos["side"] == "yes" and btc_price > pos["strike"]) or
                (pos["side"] == "no"  and btc_price <= pos["strike"])
            ) else "loss"

        log.info(f"{ticker}: result={market_result!r} → {outcome}")
        exit_price = 100 if outcome == "win" else 0
        _entry_p = pos["entry_price_cents"] / 100.0
        _fee_rate = config.get("kalshi_fee_per_contract_cents", 7) / 100.0
        fee = math.ceil(_fee_rate * pos["contracts"] * _entry_p * (1.0 - _entry_p) * 100) / 100
        pnl = (exit_price - pos["entry_price_cents"]) * pos["contracts"] / 100 - fee
        profit_pct = (exit_price - pos["entry_price_cents"]) / pos["entry_price_cents"] * 100 \
                     if pos["entry_price_cents"] else 0

        log.info(f"{ticker} expired. Outcome={outcome}, P&L=${pnl:.2f} (fee=${fee:.2f})")
        pnl_str    = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        outcome_str = "✅ WIN" if outcome == "win" else "❌ LOSS"


        await db_update_trade(pos["trade_id"], {
            "exit_price_cents": exit_price,
            "exit_reason": "expiry",
            "outcome": outcome,
            "pnl_dollars": round(pnl, 2),
            "profit_percent": round(profit_pct, 2),
        })

        # Clear position immediately — before notifications so a Telegram failure
        # never leaves the asset stuck in LOCKED indefinitely.
        if _use_state:
            state["position"] = None
            state["phase"] = "DONE"
        else:
            bot_state.current_position = None
            bot_state.current_phase = "DONE"

        # Consecutive-loss tracker (no pause — informational only)
        if outcome == "win":
            bot_state._consecutive_losses = 0
        else:
            bot_state._consecutive_losses += 1
            max_cl = config.get("max_consecutive_losses", 5)
            if bot_state._consecutive_losses >= max_cl:
                _resume_str = "n/a"
                # Prefer the stored market duration so the session label is
                # stable regardless of how long the trade was held. Falls back
                # to held-time for any positions created before this field.
                _cl_dur_min = pos.get("market_duration_min") or (
                    (time.time() - pos.get("entry_ts", time.time())) / 60.0
                )
                _cl_ctx = _notify_ctx(
                    asset, pos.get("ticker", "?"), _cl_dur_min,
                    _phase_for_eth(asset, pos.get("elapsed_at_entry", 0)),
                )
                await send_telegram(
                    f"<b>🔵 [S2 D3 Hybrid] {_cl_ctx} {bot_state._consecutive_losses} consecutive losses</b>"
                )

        pct_str   = f"+{profit_pct:.0f}%" if profit_pct >= 0 else f"{profit_pct:.0f}%"
        mode_icon = {"paper": "[PAPER]", "demo": "[DEMO]"}.get(pos["mode"], "[LIVE]")
        _time_str = datetime.now(timezone(timedelta(hours=-7))).strftime("%b %d %I:%M %p PST")
        _dur_secs = int(time.time() - pos.get("entry_ts", time.time()))
        _dur_str  = f"{_dur_secs // 60}m {_dur_secs % 60}s"
        # Prefer the stored market duration so the session label is stable
        # regardless of hold length; fall back to held-time for backward
        # compat with positions created before this field existed.
        _close_dur_min = pos.get("market_duration_min") or (_dur_secs / 60.0)
        _close_ctx = _notify_ctx(
            asset, pos.get("ticker", ticker), _close_dur_min,
            _phase_for_eth(asset, pos.get("elapsed_at_entry", 0)),
        )
        await send_telegram(
            f"<b>🔵 [S2 D3 Hybrid] {_close_ctx} {mode_icon} {outcome_str}  {pnl_str}  ({pct_str})</b>  —  {_time_str}\n"
            f"{pos['side'].upper()}  {pos['contracts']} contracts  |  held {_dur_str}\n"
            f"Entry: {pos['entry_price_cents']}c  ->  Expiry: {exit_price}c\n"
            f"{asset}: ${btc_price:,.0f}  vs  Strike: ${pos['strike']:,.0f}"
        )
        await _settle_s1_trade(ticker, market_result, btc_price, config, asset)
        return

    # Still in the market — just hold and log
    log.info(
        f"[HOLDING] {ticker} | side={pos['side'].upper()} | entry={pos['entry_price_cents']}c "
        f"| price=${btc_price:,.4g} | strike=${pos['strike']:,.4g} | {secs_left:.0f}s left"
    )


# ══════════════════════════════════════════════════════════════════════════════════
#  Non-BTC asset processing
# ══════════════════════════════════════════════════════════════════════════════════

def _init_asset_state(asset: str) -> dict:
    """Return a fresh per-asset state dict."""
    return {
        "phase": "DONE",
        "position": None,
        "order_attempted": set(),
        "prev_ticker": None,
        "market": None,
    }


async def _process_asset(
    session: aiohttp.ClientSession,
    config: dict,
    asset: str,
) -> None:
    """
    Run one iteration of the trading state machine for a non-BTC asset.
    Called every cycle from _non_btc_asset_loop.
    """
    if asset not in bot_state._asset_states:
        bot_state._asset_states[asset] = _init_asset_state(asset)
    st = bot_state._asset_states[asset]

    # Price check
    price = _am_get_price(asset)
    if price is None:
        log.debug(f"[{asset}] no price yet — skipping")
        return
    age = _am_price_age(asset)
    if age is not None and age > 60:
        log.warning(f"[{asset}] price stale ({age:.0f}s) — skipping")
        return

    # Market fetch
    try:
        market = await fetch_market_for_asset(session, asset)
    except Exception as exc:
        log.warning(f"[{asset}] market fetch error: {exc}")
        return

    if market is None:
        # If a position is open with a stored close time, still run locked-phase handler
        # so it can settle. Repeated warnings each cycle are expected until close_time elapses.
        if st["phase"] == "LOCKED" and st.get("position") is not None:
            _close_time = st["position"].get("market_close_time", "")
            if not _close_time:
                log.error(f"[{asset}] LOCKED position missing market_close_time — cannot safely settle without market. Skipping.")
            else:
                log.warning(f"[{asset}] no active market — still processing open LOCKED position.")
                try:
                    await handle_locked_phase(session, price, 0, config, asset=asset, state=st)
                except Exception as exc:
                    log.error(f"[{asset}] LOCKED phase error (no market): {exc}", exc_info=True)
        else:
            log.debug(f"[{asset}] no active market")
            st["phase"] = "DONE"
            st["prev_ticker"] = None
        return

    st["market"] = market
    ticker = market.get("ticker", "")
    secs_left = seconds_remaining(market)
    elapsed = seconds_elapsed(market)

    # Strike
    try:
        strike = parse_strike(market)
    except Exception:
        strike = None
    if strike is None:
        yes_sub = market.get("yes_sub_title") or ""
        if "TBD" in yes_sub:
            strike = _am_get_price(asset)
            if strike:
                log.info(f"[{asset}] strike TBD — using live price {strike:.2f}")
            else:
                log.warning(f"[{asset}] cannot parse strike — skipping")
                return
        else:
            log.warning(f"[{asset}] cannot parse strike — skipping")
            return

    # Ticker rollover detection
    prev_ticker = st.get("prev_ticker")
    if prev_ticker is None:
        st["prev_ticker"] = ticker
        if st["phase"] == "DONE":
            st["phase"] = "WATCH"
            log.info(f"[{asset}] First market: {ticker}. Starting WATCH.")
    elif ticker != prev_ticker:
        if st["phase"] == "LOCKED":
            log.info(f"[{asset}] Market rolled to {ticker} but position still open on {prev_ticker} — staying LOCKED.")
            ticker = prev_ticker
            market = st.get("market") or market
        else:
            log.info(f"[{asset}] New market: {ticker} (was {prev_ticker}). Resetting to WATCH.")
            if prev_ticker in bot_state._s1_pending_trades:
                asyncio.create_task(_try_settle_orphaned_s1(session, prev_ticker, price, config, asset))
            st["phase"] = "WATCH"
            st["position"] = None
            st["order_attempted"].discard(prev_ticker)
            st["prev_ticker"] = ticker

    # WATCH
    if st["phase"] == "WATCH":
        if elapsed > bot_state.WATCH_PHASE_SECONDS:
            log.info(f"[{asset}] {ticker}: elapsed {elapsed:.0f}s → READY.")
            st["phase"] = "READY"
        else:
            log.info(f"[{asset}] {ticker}: WATCH ({elapsed:.0f}s elapsed).")
            return

    # LOCKED
    if st["phase"] == "LOCKED":
        try:
            await handle_locked_phase(session, price, secs_left, config, asset=asset, state=st)
        except Exception as exc:
            log.error(f"[{asset}] LOCKED phase error: {exc}", exc_info=True)
        return

    # DONE
    if st["phase"] == "DONE":
        if secs_left > 3 * 60 and ticker not in st["order_attempted"]:
            log.info(f"[{asset}] DONE → READY re-entry: {ticker} has {secs_left:.0f}s left.")
            st["phase"] = "READY"
        else:
            log.info(f"[{asset}] DONE. {secs_left:.0f}s left — waiting for next market.")
            return

    # READY
    if st["phase"] == "READY":
        try:
            await handle_ready_phase(
                session, config, market, ticker,
                price, secs_left, strike, elapsed,
                asset=asset, state=st,
            )
        except Exception as exc:
            log.error(f"[{asset}] READY phase error: {exc}", exc_info=True)


async def _non_btc_asset_loop(session: aiohttp.ClientSession) -> None:
    """
    Independent 10-second loop processing all non-BTC enabled assets.
    Runs as a background asyncio task alongside main_loop (which handles BTC).
    """
    while True:
        try:
            config = read_config()
            if not config.get("bot_enabled", False):
                # Populate PAUSED state so dashboard shows prices instead of OFFLINE
                for _pa in config.get("enabled_assets", ["ETH", "SOL", "XRP"]):
                    if _pa == "BTC":
                        continue
                    if _pa not in bot_state._asset_states:
                        bot_state._asset_states[_pa] = {"phase": "PAUSED", "market": None, "eval": {}}
                    else:
                        bot_state._asset_states[_pa]["phase"] = "PAUSED"
                await asyncio.sleep(10)
                continue
            for asset in config.get("enabled_assets", ["ETH", "SOL", "XRP"]):
                if asset == "BTC":
                    continue
                try:
                    await _process_asset(session, config, asset)
                except Exception as exc:
                    log.error(f"Non-BTC asset loop error [{asset}]: {exc}", exc_info=True)
        except Exception as exc:
            log.error(f"Non-BTC asset loop outer error: {exc}", exc_info=True)
        await asyncio.sleep(10)


# ══════════════════════════════════════════════════════════════════════════════════
#  Main loop
# ══════════════════════════════════════════════════════════════════════════════════

async def main_loop() -> None:
    """
    Permanent 10-second loop driving all trading logic.
    All exceptions are caught per-iteration to prevent crashes.
    """

    prev_ticker: str | None = None

    # ── Recover open position and consecutive-loss state after a crash/restart ─
    try:
        with open(bot_state._STATE_FILE, "r") as _sf:
            _saved = json.load(_sf)
        _saved_pos = _saved.get("open_position")
        _saved_phase = _saved.get("phase", "")
        if _saved_pos and _saved_phase == "LOCKED" and _saved_pos.get("trade_id"):
            bot_state.current_position = _saved_pos
            bot_state.current_phase    = "LOCKED"
            log.warning(
                f"Recovered open position from state file: "
                f"trade_id={_saved_pos.get('trade_id')} "
                f"side={_saved_pos.get('side')} "
                f"ticker={_saved_pos.get('ticker')}"
            )
        saved_cl = _saved.get("consecutive_losses", 0)
        if isinstance(saved_cl, int) and saved_cl > 0:
            bot_state._consecutive_losses = saved_cl
    except Exception:
        pass  # fresh start, no state to recover

    # Warn about S1 positions that were open when the bot last stopped.
    # These are financially live on Kalshi but untracked in _s1_pending_trades.
    # Manual reconciliation against the Kalshi fills API may be needed.
    try:
        import sqlite3 as _sqlite3
        _s1_chk = _sqlite3.connect(bot_state._DB_FILE)
        _s1_orphans = _s1_chk.execute(
            "SELECT id, market_id, asset FROM trades "
            "WHERE strategy_variant='strategy1' AND outcome='pending'"
        ).fetchall()
        _s1_chk.close()
        for _row in _s1_orphans:
            log.warning(
                "S1 orphan from prior session — trade_id=%s market=%s asset=%s "
                "(outcome still pending; check Kalshi fills manually)",
                _row[0], _row[1], _row[2],
            )
    except Exception as _s1_chk_exc:
        log.debug(f"S1 orphan check skipped: {_s1_chk_exc}")

    # TCPConnector with keepalive_timeout prevents stale pooled connections
    # from silently breaking API calls after many hours of uptime.
    connector = aiohttp.TCPConnector(keepalive_timeout=30, limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Non-BTC assets run in a separate background task so they aren't
        # gated by the BTC state machine's continue/sleep cycle.
        asyncio.create_task(_non_btc_asset_loop(session))

        while True:
            try:
                midnight_reset()


                # Fresh config read
                try:
                    config = read_config()
                except Exception as exc:
                    log.error(f"Config read error: {exc}")
                    await asyncio.sleep(10)
                    continue

                if not config.get("bot_enabled", False):
                    await write_state_file(config, bot_state.current_market, "PAUSED", 0,
                                           get_btc_price(), bot_state.last_confidence_score,
                                           bot_state.last_confidence_breakdown, bot_state.last_action, bot_state.last_skip_reason)
                    await asyncio.sleep(10)
                    continue

                if "BTC" not in config.get("enabled_assets", []):
                    await write_state_file(config, None, "DONE", 0,
                                           get_btc_price(), 0, {}, "btc_disabled", "")
                    await asyncio.sleep(10)
                    continue

                btc_price = get_btc_price()
                if btc_price is None:
                    log.warning("Waiting for BTC price...")
                    await write_state_file(config, bot_state.current_market, bot_state.current_phase, 0,
                                           None, bot_state.last_confidence_score,
                                           bot_state.last_confidence_breakdown, bot_state.last_action, bot_state.last_skip_reason)
                    await asyncio.sleep(10)
                    continue
                _btc_age = _am_price_age("BTC")
                if _btc_age is not None and _btc_age > 60:
                    age = int(_btc_age)
                    log.warning(f"BTC price stale ({age}s old) — skipping cycle.")
                    await write_state_file(config, bot_state.current_market, bot_state.current_phase, 0,
                                           btc_price, bot_state.last_confidence_score,
                                           bot_state.last_confidence_breakdown, "skip", f"btc_stale_{age}s")
                    await asyncio.sleep(10)
                    continue

                # Fetch active market
                try:
                    market = await fetch_current_market(session)
                except Exception as exc:
                    log.error(f"Market fetch error: {exc}")
                    await asyncio.sleep(10)
                    continue

                if market is None:
                    # If we have an open position with a stored close time, still run the
                    # locked-phase handler so the trade can settle even when no new market
                    # is visible. Repeated warnings each cycle until close_time elapses
                    # are expected — not errors.
                    if bot_state.current_phase == "LOCKED" and bot_state.current_position is not None:
                        _close_time = bot_state.current_position.get("market_close_time", "")
                        if not _close_time:
                            log.error("LOCKED position missing market_close_time — cannot safely settle without market. Skipping.")
                        else:
                            log.warning("No active BTC markets — still processing open LOCKED position.")
                            try:
                                await handle_locked_phase(session, btc_price, 0, config)
                            except Exception as exc:
                                log.error(f"LOCKED phase error (no market): {exc}", exc_info=True)
                        await write_state_file(config, None, bot_state.current_phase, 0, btc_price,
                                               bot_state.last_confidence_score, bot_state.last_confidence_breakdown,
                                               bot_state.last_action, bot_state.last_skip_reason)
                    else:
                        log.warning("No active BTC markets found.")
                        await write_state_file(config, None, "DONE", 0, btc_price,
                                               bot_state.last_confidence_score, bot_state.last_confidence_breakdown,
                                               bot_state.last_action, bot_state.last_skip_reason)
                    await asyncio.sleep(10)
                    continue

                bot_state.current_market = market
                ticker = market.get("ticker", "")

                # Detect new market (different ticker)
                if prev_ticker is None:
                    prev_ticker = ticker
                    if bot_state.current_phase == "DONE":
                        bot_state.current_phase = "WATCH"
                        log.info(f"First market: {ticker}. Starting WATCH.")
                elif ticker != prev_ticker:
                    if bot_state.current_phase == "LOCKED":
                        # Never reset a live position when the market rolls over.
                        # The position is on the OLD ticker — keep monitoring it.
                        log.info(f"Market rolled to {ticker} but position still open on {prev_ticker} — staying LOCKED.")
                        # Keep using the old market object for SL monitoring this cycle
                        ticker = prev_ticker
                        market = bot_state._market_cache if bot_state._market_cache and bot_state._market_cache.get("ticker") == prev_ticker else market
                    else:
                        log.info(f"New market: {ticker} (was {prev_ticker}). Resetting to WATCH.")
                        if prev_ticker in bot_state._s1_pending_trades:
                            asyncio.create_task(_try_settle_orphaned_s1(session, prev_ticker, btc_price, config, "BTC"))
                        bot_state.current_phase = "WATCH"
                        bot_state.current_position = None
                        bot_state._order_attempted_tickers.discard(prev_ticker)
                        prev_ticker = ticker

                secs_left = seconds_remaining(market)
                elapsed = seconds_elapsed(market)

                # Parse strike
                try:
                    strike = parse_strike(market)
                except Exception as exc:
                    log.error(f"Strike parse exception: {exc}")
                    strike = None

                if strike is None:
                    yes_sub = market.get("yes_sub_title") or ""
                    if "TBD" in yes_sub:
                        strike = _am_get_price("BTC")
                        if strike:
                            log.info(f"{ticker}: strike TBD — using live price {strike:.2f}")
                        else:
                            log.warning(f"{ticker}: cannot parse strike. Skipping cycle.")
                            await asyncio.sleep(10)
                            continue
                    else:
                        log.warning(f"{ticker}: cannot parse strike. Skipping cycle.")
                        await asyncio.sleep(10)
                        continue

                # ── WATCH ──────────────────────────────────────────────────────────────────
                if bot_state.current_phase == "WATCH":
                    if elapsed > bot_state.WATCH_PHASE_SECONDS:
                        log.info(f"{ticker}: elapsed {elapsed:.0f}s → READY.")
                        bot_state.current_phase = "READY"
                    else:
                        log.info(f"{ticker}: WATCH ({elapsed:.0f}s elapsed).")
                        await _log_entry(market, "WATCH", secs_left, btc_price, strike,
                                         None, 0, "skip", f"WATCH phase, {elapsed:.0f}s elapsed",
                                         config.get("mode", "paper"))
                        await write_state_file(config, market, bot_state.current_phase, secs_left,
                                               btc_price, 0, {}, "watch", "")
                        await asyncio.sleep(10)
                        continue

                # ── LOCKED ─────────────────────────────────────────────────────────────────
                if bot_state.current_phase == "LOCKED":
                    try:
                        await handle_locked_phase(
                            session, btc_price, secs_left, config
                        )
                    except Exception as exc:
                        log.error(f"LOCKED phase error: {exc}", exc_info=True)
                    await write_state_file(config, market, bot_state.current_phase, secs_left, btc_price,
                                           bot_state.last_confidence_score, bot_state.last_confidence_breakdown,
                                           bot_state.last_action, bot_state.last_skip_reason)
                    await asyncio.sleep(10)
                    continue

                # ── DONE ───────────────────────────────────────────────────────────────────
                if bot_state.current_phase == "DONE":
                    # Re-enter READY only if no order was attempted for this ticker.
                    # This prevents duplicate orders when fill_confirmed=False but
                    # the order actually went through on Kalshi.
                    if secs_left > 3 * 60 and ticker not in bot_state._order_attempted_tickers:
                        log.info(
                            f"DONE → READY re-entry: {ticker} has {secs_left:.0f}s left."
                        )
                        bot_state.current_phase = "READY"
                        # Fall through to READY handler below
                    else:
                        log.info(f"DONE phase. {secs_left:.0f}s left — waiting for next market.")
                        await write_state_file(config, market, bot_state.current_phase, secs_left, btc_price,
                                               bot_state.last_confidence_score, bot_state.last_confidence_breakdown,
                                               bot_state.last_action, bot_state.last_skip_reason)
                        await asyncio.sleep(10)
                        continue

                # ── READY ──────────────────────────────────────────────────────────────────
                if bot_state.current_phase == "READY":
                    try:
                        await handle_ready_phase(
                            session, config, market, ticker,
                            btc_price, secs_left, strike, elapsed
                        )
                    except Exception as exc:
                        log.error(f"READY phase error: {exc}", exc_info=True)

                await write_state_file(config, market, bot_state.current_phase, secs_left, btc_price,
                                       bot_state.last_confidence_score, bot_state.last_confidence_breakdown,
                                       bot_state.last_action, bot_state.last_skip_reason)

                if bot_state.current_phase == "READY":
                    await asyncio.sleep(5)
                    continue

            except Exception as exc:
                log.error(f"Main loop unhandled error: {exc}", exc_info=True)

            await asyncio.sleep(10)
