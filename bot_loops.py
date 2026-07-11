"""bot_loops.py - Phase handlers, asset loop, main trading loop."""
__all__ = ["handle_ready_phase", "handle_locked_phase", "main_loop"]

import asyncio
import json
import logging
import math
import os
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import aiohttp

import bot_state
import bot_stats
import asset_manager
from asset_manager import get_price as _am_get_price, price_age_seconds as _am_price_age
from bot_infra import read_config, write_config, get_asset_config, db_write_trade, db_update_trade, send_telegram, db_brain_scorecard, db_get_today_pnl, fmt_ts, display_tz, et_day_bounds_utc, db_write_decision, db_backfill_decision_outcome, db_pending_decision_tickers, db_write_maker_sample, db_write_settlement_basis, db_settled_decision_probs, db_settled_decision_zs, db_basis_rows, db_settled_picks
from bot_market import (
    fetch_current_market, fetch_market_for_asset, fetch_orderbook,
    seconds_remaining, seconds_elapsed, parse_strike, get_btc_price,
    kalshi_headers,
    calculate_contracts, implied_prob, place_order, _portfolio_has_position,
    _maybe_adjust_clock_skew,
)
from bot_strategy import (
    strategy_brain_s1, strategy_brain_s2,
    track_contract_price, track_contract_mid, update_implied_sigma,
    _is_quiet_hours, _market_implied_p_yes, _kelly_stake, shadow_fav_candidate,
)
from bot_risk import (
    check_daily_limits, midnight_reset, write_state_file, _log_entry,
    _execute_s1_trade, _settle_s1_trade, _try_settle_orphaned_s1,
    _settle_s1_orphans,
    _execute_slot_trade, _settle_slot_trades, _settle_slot_orphans,
    _settle_slot_rollover,
)
from bot_strategies import STRATEGY_REGISTRY, enabled_slots
from reconcile import fetch_open_positions

log = logging.getLogger("bot")

# The daily summary covers the previous full ET calendar day - the same timezone
# the markets, sessions and quiet-hours logic use - so no evening trade is ever
# missing from its day's report. Sent-state persists in config so restarts don't
# resend it.
_last_summary_sent_for: str = ""
_ET_TZ = ZoneInfo("America/New_York")

_ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE"]

# Edge-measurement: dedup so we log at most one decision_log row per (ticker, strategy)
# per window (each 15-min window has a unique ticker). Bounded to avoid unbounded growth.
# _logged_trade_decisions grants one EXTRA row when the brain later says trade: the
# full-signal skip gates (tgtbt/tail/fade/stale) almost always log a skip first, and
# without the second slot no would_trade=1 row would ever reach the edge harness.
_logged_decisions: set = set()
_logged_trade_decisions: set = set()

# Throttle the maker held-book fetch: {ticker: last_fetch_ts}. ~25s sampling of the ask path
# is plenty for the counterfactual - avoids an extra orderbook fetch on every ~10s hold cycle.
_maker_track_last_fetch: dict = {}
_MAKER_TRACK_MIN_INTERVAL = 25.0


def _bump_slot_activity(slot: str, brain: dict) -> None:
    """
    Count every lab-slot brain evaluation: trades and skip reasons (keyed on the
    reasoning up to the first ':' so parameterized reasons bucket together). This is
    the "why is a slot quiet?" telemetry - gate-stage skips never reach decision_log,
    so without it a silent strategy is indistinguishable from a broken one. Never raises.
    """
    try:
        act = bot_state._slot_activity.get(slot)
        if act is None:
            act = {"evals": 0, "trades": 0, "skips": {}, "since": time.time()}
            bot_state._slot_activity[slot] = act
        act["evals"] += 1
        if brain.get("action") == "trade":
            act["trades"] += 1
            return
        key = str(brain.get("reasoning") or "unknown").split(":", 1)[0][:48]
        skips = act["skips"]
        if key in skips or len(skips) < 24:
            skips[key] = skips.get(key, 0) + 1
    except Exception:
        pass


def _dump_slot_activity() -> None:
    """Persist the counters for the dashboard (separate process). Atomic; never raises."""
    try:
        from bot_infra import atomic_write_json
        path = os.path.join(bot_state._DATA_DIR, "lab_activity.json")
        atomic_write_json(dict(bot_state._slot_activity), path)
    except Exception as exc:
        log.debug("lab activity dump skipped: %s", exc)


def _record_prev_window_estimate(asset: str, prev_ticker: str, spot: float) -> None:
    """
    S6 memory for windows S2 never traded: at rollover, estimate the closed window's
    direction from the current spot vs the remembered strike (_last_window_strike).
    Defers to the official settlement entry when handle_locked_phase already recorded
    this ticker. Never raises.
    """
    try:
        lws = bot_state._last_window_strike.get(asset)
        if not lws or lws[0] != prev_ticker or not lws[1] or spot is None or spot <= 0:
            return
        existing = bot_state._prev_window_outcome.get(asset)
        if existing and existing.get("ticker") == prev_ticker:
            return   # official (or earlier) entry for this window wins
        bot_state._prev_window_outcome[asset] = {
            "result": "yes" if float(spot) > float(lws[1]) else "no",
            "strike": float(lws[1]), "spot_at_close": float(spot),
            "ts": time.time(), "ticker": prev_ticker, "estimated": True,
        }
    except Exception:
        pass


def _remember_window_strike(asset: str, market: "dict | None") -> None:
    """Keep _last_window_strike fresh: pair the strike with its OWN market object so a
    LOCKED-phase rollover (where locals mix old ticker / new strike) cannot mispair."""
    try:
        if not market:
            return
        _t = market.get("ticker")
        _s = parse_strike(market)
        if _t and _s:
            bot_state._last_window_strike[asset] = (_t, float(_s))
    except Exception:
        pass


async def _log_decision(brain: dict, ticker: str, asset: str, secs_left: float,
                        yes_ask, no_ask, config: dict, strategy: str) -> None:
    """
    Record one brain evaluation in decision_log (once per ticker+strategy). Only logs
    decisions that reached the model/EV stage (signals carry model_raw_p_yes), i.e. the
    population relevant to measuring whether the signal beats the market. Never raises.
    """
    try:
        if not config.get("measurement_enabled", True):
            return
        sig = brain.get("signals") or {}
        if "model_raw_p_yes" not in sig:
            return  # gate-stage skip (time/dist/etc.) - no model opinion to score
        is_trade = brain.get("action") == "trade"
        key = (ticker, strategy)
        if key in _logged_decisions:
            if not is_trade or key in _logged_trade_decisions:
                return
            if len(_logged_trade_decisions) > 5000:
                _logged_trade_decisions.clear()
            _logged_trade_decisions.add(key)
        else:
            if len(_logged_decisions) > 5000:
                _logged_decisions.clear()
            _logged_decisions.add(key)
            if is_trade:
                if len(_logged_trade_decisions) > 5000:
                    _logged_trade_decisions.clear()
                _logged_trade_decisions.add(key)

        side = brain.get("side")
        mkt_p_side = sig.get("mkt_p")
        market_mid_p_yes = _market_implied_p_yes(yes_ask, no_ask)
        entry_price = yes_ask if side == "yes" else no_ask
        await db_write_decision({
            "ts": datetime.now(timezone.utc).isoformat(),
            "ticker": ticker, "asset": asset, "strategy": strategy,
            "mode": config.get("mode", "paper"), "side": side,
            "model_p_yes": sig.get("model_raw_p_yes"),
            "market_mid_p_yes": market_mid_p_yes,
            "market_edge": sig.get("market_edge"),
            "entry_price_cents": entry_price,
            "secs_left": secs_left,
            "would_trade": is_trade,
            "spot": sig.get("spot"), "strike": sig.get("strike"),
            "sigma_eff": sig.get("sigma_eff"),
            # Prefer the de-scaled z: the sigma_scale refit needs a target that does
            # not move when the applied scale changes.
            "z": sig.get("z_raw", sig.get("z")),
        })
    except Exception as exc:
        log.debug("_log_decision skipped (%s/%s): %s", ticker, strategy, exc)


_last_decision_backfill_ts: float = 0.0
_last_recalibration_ts: float = 0.0


async def _backfill_pending_decisions(session) -> None:
    """
    Fetch official settlement results for pending decision_log tickers - including ones we
    never traded - so SKIPPED decisions can be scored (this is what makes the edge report
    free of survivorship bias). Bounded per call; only touches tickers whose window closed.
    """
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
        tickers = await db_pending_decision_tickers(cutoff, limit=25)
        for _t in tickers:
            try:
                _path = f"/markets/{_t}"
                async with session.get(
                    bot_state.KALSHI_BASE_URL + _path,
                    headers=kalshi_headers("GET", _path),
                    timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
                ) as _resp:
                    _mdata = await _resp.json()
                _res = (_mdata.get("market") or _mdata).get("result")
                if _res in ("yes", "no"):
                    await db_backfill_decision_outcome(_t, _res)
            except Exception as _exc:
                log.debug("decision backfill fetch failed for %s: %s", _t, _exc)
    except Exception as exc:
        log.debug("_backfill_pending_decisions skipped: %s", exc)


def _record_settlement_basis(ticker: str, asset: str, strike: float, our_spot: float,
                             market_result: str, settled_official: bool) -> None:
    """
    Record the gap between our Coinbase spot-vs-strike implied side and Kalshi's
    official settlement, in memory and in the settlement_basis table. The persisted
    rows feed the per-asset basis-offset fit in the recalibration job. Never raises.
    """
    try:
        if not settled_official or market_result not in ("yes", "no") or not strike:
            return  # only official results are trustworthy basis ground truth
        our_side = "yes" if our_spot > strike else "no"
        signed_dist = (our_spot - strike) / strike if strike > 0 else 0.0
        sample = {
            "ts": datetime.now(timezone.utc).isoformat(), "ticker": ticker, "asset": asset,
            "strike": strike, "our_spot": our_spot, "kalshi": market_result,
            "ours": our_side, "agree": our_side == market_result, "signed_dist": signed_dist,
        }
        bot_state._settlement_basis.append(sample)
        try:
            asyncio.get_running_loop().create_task(db_write_settlement_basis(sample))
        except RuntimeError:
            pass  # no running loop (sync test context): keep the in-memory sample only
        if our_side != market_result:
            # disagreements near the strike are where the basis (index vs spot, snap timing) shows
            log.info("SETTLE_BASIS %s %s kalshi=%s ours=%s dist=%.5f (DISAGREE)",
                     asset, ticker, market_result, our_side, signed_dist)
    except Exception as exc:
        log.debug("_record_settlement_basis skipped for %s: %s", ticker, exc)


def _prune_tracking_state(max_age_secs: float = 1800.0) -> None:
    """
    Drop per-ticker tracking state for windows long settled. Every 15-min window mints
    a fresh ticker, so these dicts grow forever on a weeks-long process without a
    sweep. Runs on the recalibration cadence; never raises.
    """
    try:
        now = time.time()
        for d in (bot_state._contract_mid_history, bot_state._contract_price_history,
                  bot_state._maker_track):
            stale = [t for t, dq in list(d.items())
                     if not dq or (now - dq[-1][0]) > max_age_secs]
            for t in stale:
                d.pop(t, None)
        stale = [t for t, ts in list(_maker_track_last_fetch.items())
                 if now - ts > max_age_secs]
        for t in stale:
            _maker_track_last_fetch.pop(t, None)
    except Exception as exc:
        log.debug("_prune_tracking_state skipped: %s", exc)


async def _recalibrate_model(config: dict) -> None:
    """
    Periodic self-calibration: fit prob_scale per strategy from settled decision_log
    rows and the per-asset basis offset from settlement_basis, apply to the live
    state slots, and persist to data/calibration.json. Fail-open: any error leaves
    the current calibration untouched.
    """
    _prune_tracking_state()
    try:
        from scripts.calibration import (fit_prob_scale, fit_sigma_scale, fit_basis_offset,
                                         fit_lead_beta, compute_auto_blocks, save_calibration)
        cal = {"prob_scale": {}, "sigma_scale": {}, "basis_offset": {}, "live_beta": {},
               "implied_sigma": {}, "auto_blocked": {},
               "updated_at": datetime.now(timezone.utc).isoformat()}

        # Sigma scale FIRST (vol space is where the 2026-07 failure lived), then
        # prob_scale mops up whatever shape error remains. Fit only strategy2 rows:
        # S1's z is computed from the beta-shifted predicted spot (lead-signal error
        # would leak into the vol scale) and s_fav rows are selection-biased favorites.
        # The logged z is de-scaled, so replacing the scale here is stationary.
        z_rows = await db_settled_decision_zs(strategy="strategy2")
        z_by_asset: dict = {}
        for asset, z, outcome in z_rows:
            z_by_asset.setdefault(asset, []).append((z, outcome))
        for asset, rows in z_by_asset.items():
            s = fit_sigma_scale(rows)
            bot_state._sigma_scale[asset] = s
            cal["sigma_scale"][asset] = s

        for strategy, slot in (("strategy1", bot_state._brain_cal_s1),
                               ("strategy2", bot_state._brain_cal_s2)):
            rows = await db_settled_decision_probs(strategy)
            scale = fit_prob_scale(rows)
            slot["prob_scale"] = scale
            slot["last_count"] = len(rows)
            cal["prob_scale"][strategy] = scale
        basis_rows = await db_basis_rows()
        by_asset: dict = {}
        for asset, dist, kalshi in basis_rows:
            by_asset.setdefault(asset, []).append((dist, kalshi))
        for asset, rows in by_asset.items():
            offset = fit_basis_offset(rows)
            bot_state._basis_offsets[asset] = offset
            cal["basis_offset"][asset] = offset

        # BTC-LEAD betas (alt return on the PRIOR BTC grid return - the quantity S1
        # uses). The contemporaneous fit_rolling_beta slope must not write _live_betas:
        # it runs 3-4x the lead value and inflated every S1 prediction.
        btc_pts = list(asset_manager._prices.get("BTC") or [])
        for asset in ("ETH", "SOL", "XRP", "DOGE"):
            beta, n = fit_lead_beta(btc_pts, list(asset_manager._prices.get(asset) or []))
            if beta is not None:
                bot_state._live_betas[asset] = beta
                cal["live_beta"][asset] = beta

        # Persist the implied-sigma EWMA so a redeploy does not cold-start vol back
        # onto the static table.
        for asset, entry in bot_state._implied_sigma.items():
            if isinstance(entry, dict) and entry.get("sigma"):
                cal["implied_sigma"][asset] = {
                    "sigma": float(entry["sigma"]), "ts": float(entry.get("ts", 0.0)),
                    "n": int(entry.get("n", 0)),
                }

        # Auto-gate: recompute blocked buckets fresh each cycle (GATE-1 per bucket).
        from scripts.edge_report import _pnl_stats
        import sessions as _sessions
        picks = await db_settled_picks()
        blocks = compute_auto_blocks(picks, _sessions.session_for_iso, _pnl_stats)
        bot_state._auto_blocked_sessions = set(blocks["sessions"])
        bot_state._auto_blocked_assets = {tuple(sa) for sa in blocks["strategy_assets"]}
        cal["auto_blocked"] = {"sessions": blocks["sessions"],
                               "strategy_assets": blocks["strategy_assets"]}

        save_calibration(cal)
        log.info("Recalibration: sigma_scale=%s prob_scale=%s basis_offset=%s live_beta=%s auto_blocked=%s",
                 cal["sigma_scale"], cal["prob_scale"], cal["basis_offset"], cal["live_beta"],
                 cal["auto_blocked"])
    except Exception as exc:
        log.debug("_recalibrate_model skipped: %s", exc)


def _load_saved_calibration() -> None:
    """Restore the last persisted calibration at startup (fail-open to neutral)."""
    try:
        from scripts.calibration import load_calibration
        cal = load_calibration()
        for strategy, slot in (("strategy1", bot_state._brain_cal_s1),
                               ("strategy2", bot_state._brain_cal_s2)):
            w = cal.get("prob_scale", {}).get(strategy)
            if isinstance(w, (int, float)) and 0.5 <= w <= 1.2:
                slot["prob_scale"] = float(w)
        for asset, off in (cal.get("basis_offset") or {}).items():
            if isinstance(off, (int, float)) and abs(off) <= 0.0010:
                bot_state._basis_offsets[asset] = float(off)
        for asset, s in (cal.get("sigma_scale") or {}).items():
            if isinstance(s, (int, float)) and 0.5 <= s <= 2.0:
                bot_state._sigma_scale[asset] = float(s)
        # Accept a persisted live beta only within the same relative-to-static band
        # _asset_beta enforces (kills a stale absolute-range value from old deploys).
        from bot_strategy import _load_betas as _static_betas
        statics = _static_betas()
        for asset, beta in (cal.get("live_beta") or {}).items():
            st = float(statics.get(asset, 0.4) or 0.4)
            if isinstance(beta, (int, float)) and 0.5 * st <= beta <= 1.5 * st:
                bot_state._live_betas[asset] = float(beta)
        # Implied-sigma EWMA survives a restart only while reasonably fresh (2h).
        now_ts = time.time()
        for asset, entry in (cal.get("implied_sigma") or {}).items():
            try:
                sig = float(entry.get("sigma", 0.0))
                ts = float(entry.get("ts", 0.0))
                if sig > 0 and (now_ts - ts) <= 7200.0:
                    bot_state._implied_sigma[asset] = {
                        "sigma": sig, "ts": ts, "n": int(entry.get("n", 0)),
                    }
            except (TypeError, ValueError, AttributeError):
                continue
        blocked = cal.get("auto_blocked") or {}
        bot_state._auto_blocked_sessions = {s for s in (blocked.get("sessions") or [])
                                            if isinstance(s, str)}
        bot_state._auto_blocked_assets = {tuple(sa) for sa in (blocked.get("strategy_assets") or [])
                                          if isinstance(sa, (list, tuple)) and len(sa) == 2}
    except Exception as exc:
        log.debug("_load_saved_calibration skipped: %s", exc)


def _maker_fee_frac(price_cents: float) -> float:
    """Kalshi maker fee per contract in dollars: 0.0175 * p * (1-p) (~25% of taker)."""
    p = max(0.0, min(1.0, price_cents / 100.0))
    return 0.0175 * p * (1.0 - p)


async def _record_maker_counterfactual(pos: dict, asset: str, outcome: str, config: dict) -> "dict | None":
    """
    Would a passive maker (1c inside the entry-side ask, posted at entry) have filled,
    and at what P&L vs the taker fill we actually took? Uses the real settlement outcome
    so adverse selection is captured (a fill correlates with the contract moving against
    us). Writes one maker_log row and RETURNS the sample dict so the paper maker
    execution model (maker_execution_enabled) can re-price the trade from it. Returns
    None when no reliable sample could be built. Never raises.
    """
    ticker = pos.get("ticker")
    sample = None
    try:
        track = bot_state._maker_track.get(ticker)
        side = pos.get("side")
        entry_ask = float(pos.get("entry_price_cents") or 0.0)
        entry_ts = float(pos.get("entry_ts") or 0.0)
        contracts = int(pos.get("contracts") or 0)
        if entry_ts <= 0:
            return None  # no reliable entry time (e.g. orphan-recovered pos)
        maker_price = entry_ask - 1.0                 # 1c inside the ask (passive)
        idx = 1 if side == "yes" else 2               # held-book tuple (ts, yes_ask, no_ask)
        filled = False
        if track and maker_price > 0:
            for row in track:
                if row[0] >= entry_ts and float(row[idx]) <= maker_price:
                    filled = True
                    break
        payoff = 100.0 if outcome == "win" else 0.0
        taker_pnl = (payoff - entry_ask) / 100.0 - (0.07 * (entry_ask / 100.0) * (1 - entry_ask / 100.0))
        maker_pnl = ((payoff - maker_price) / 100.0 - _maker_fee_frac(maker_price)) if filled else None
        sample = {
            "ts": datetime.now(timezone.utc).isoformat(), "ticker": ticker, "asset": asset,
            "strategy": "strategy2", "mode": config.get("mode", "paper"), "side": side,
            "entry_ask_cents": entry_ask, "maker_price_cents": maker_price, "filled": filled,
            "outcome": outcome, "taker_pnl": round(taker_pnl, 4),
            "maker_pnl": round(maker_pnl, 4) if maker_pnl is not None else None,
            "contracts": contracts,
        }
        await db_write_maker_sample(sample)
    except Exception as exc:
        log.debug("_record_maker_counterfactual skipped for %s: %s", ticker, exc)
    finally:
        bot_state._maker_track.pop(ticker, None)
        _maker_track_last_fetch.pop(ticker, None)
    return sample

_last_orphan_settle_ts: float = 0.0


def _format_scorecard_message(data: dict) -> str:
    lines = ["<b>Brain Scorecard</b>"]

    for brain_key, label in (("s1", "S1 (BTC-lead cross-asset)"), ("s2", "S2 (spot fair-value)")):
        lines.append(f"\n<b>{label}</b>")
        daily = data["daily"].get(brain_key, {})
        total_pnl = 0.0
        total_wins = 0
        total_losses = 0
        any_trade = False
        for asset in _ASSETS:
            row = daily.get(asset)
            if row:
                any_trade = True
                pnl = row["pnl"]
                total_pnl += pnl
                total_wins += row["wins"]
                total_losses += row["losses"]
                sign = "+" if pnl >= 0 else ""
                lines.append(f"  {asset:<5} {sign}${pnl:.2f}  {row['wins']}W/{row['losses']}L")
            else:
                lines.append(f"  {asset:<5} -")
        if any_trade:
            sign = "+" if total_pnl >= 0 else ""
            lines.append(f"  <b>Total: {sign}${total_pnl:.2f}  {total_wins}W/{total_losses}L</b>")
        else:
            lines.append("  (no trades today)")

    at_parts = []
    for brain_key, label in (("s1", "S1"), ("s2", "S2")):
        at = data["alltime"].get(brain_key, {})
        at_pnl = sum(r["pnl"] for r in at.values())
        at_wins = sum(r["wins"] for r in at.values())
        at_losses = sum(r["losses"] for r in at.values())
        sign = "+" if at_pnl >= 0 else ""
        at_parts.append(f"{label}: {sign}${at_pnl:.2f} {at_wins}W/{at_losses}L")
    lines.append("\n<b>All-time</b> | " + " | ".join(at_parts))

    s1_daily = sum(r["pnl"] for r in data["daily"].get("s1", {}).values())
    s2_daily = sum(r["pnl"] for r in data["daily"].get("s2", {}).values())
    if s1_daily > s2_daily:
        lines.append("Today's winner: <b>S1</b>")
    elif s2_daily > s1_daily:
        lines.append("Today's winner: <b>S2</b>")

    return "\n".join(lines)


async def _maybe_send_daily_summary() -> None:
    """Send ONE end-of-day summary covering the previous full ET calendar day.

    Fires at/after daily_summary_hour_et (default 0 = just after ET midnight).
    Replaces the old pair of 5pm-ET messages (scorecard + stats), which covered
    only part of the day and could be consumed early by a quiet-hours trigger.
    The sent marker persists in config so a restart never resends the summary.
    """
    global _last_summary_sent_for
    try:
        config = read_config()
        now_et = datetime.now(_ET_TZ)
        _hour = int(config.get("daily_summary_hour_et", 0))
        # 20-minute grace past the send hour so 15-min windows that straddled ET
        # midnight have settled and land in their own day's numbers.
        if (now_et.hour, now_et.minute) < (_hour, 20):
            return
        day = (now_et - timedelta(days=1)).date()
        key = day.isoformat()
        already = str(config.get("_last_daily_summary_for", ""))
        if key == _last_summary_sent_for or (already and key <= already):
            _last_summary_sent_for = key
            return
        stats = bot_stats.query_stats(
            bot_state._DB_FILE, today_date=key, day_bounds=et_day_bounds_utc(day))
        stats["consecutive_losses"] = bot_state._s2_consecutive_losses
        stats["mode"] = config.get("mode", "paper").upper()
        stats["as_of"] = fmt_ts(config=config)
        stats["display_tz"] = display_tz(config)
        await send_telegram(bot_stats.format_telegram(stats))
        _last_summary_sent_for = key
        try:
            cfg = read_config()
            cfg["_last_daily_summary_for"] = key
            write_config(cfg)
        except Exception as _persist_exc:
            log.warning("Daily summary marker persist failed: %s", _persist_exc)
    except Exception as exc:
        log.warning("Daily summary send failed (non-fatal): %s", exc)


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
    mode = config.get("s2_mode", config.get("mode", "paper"))
    mode_s1 = config.get("s1_mode", config.get("mode", "paper"))

    # Hard expiry gate - truly nothing to do in the last 90 seconds
    if secs_left < 90:
        log.info(f"{ticker}: < 90s remaining. Moving to DONE.")
        if _use_state: state["phase"] = "DONE"
        else: bot_state.current_phase = "DONE"
        return

    # Early-window gate - skip first 90s while price is still anchoring
    _elapsed = seconds_elapsed(market)
    if _elapsed < 90:
        log.debug(f"{ticker}: {_elapsed:.0f}s elapsed - price anchoring, skipping")
        return

    # Multi-window best-pick (BTC only)
    # If multiple 15-min windows are open simultaneously, evaluate all of them
    # and trade the one with the highest EV. Falls back to primary market if
    # only one window is open or fetching alternatives fails.
    # Non-BTC assets use a single market per cycle (no multi-window support).
    if asset == "BTC":
        try:
            all_windows = await fetch_current_market(session, return_all=True)
            if isinstance(all_windows, list) and len(all_windows) > 1:
                log.info(f"Multi-window: {len(all_windows)} open windows - evaluating all for best EV.")
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
                        # Use each window's own timing - they may have different close times
                        c_secs_left = seconds_remaining(candidate)
                        c_elapsed   = seconds_elapsed(candidate)
                        # Skip windows that are too close to expiry - same gate as primary market.
                        # Without this, the multi-window picker can select a market with 40s left
                        # AFTER the 90s gate already passed for the primary market.
                        if c_secs_left < 90:
                            log.info(f"  Window {c_ticker}: skipping - only {c_secs_left:.0f}s left")
                            continue
                        c_ob = await fetch_orderbook(session, c_ticker, candidate)
                        if c_ob is None:
                            continue
                        bot_state._ticker_obi[c_ticker] = c_ob["obi"]
                        track_contract_mid(c_ticker, c_ob["best_yes_ask"], c_ob["best_no_ask"])
                        update_implied_sigma(asset, btc_price, c_strike,
                                             c_ob["best_yes_ask"], c_ob["best_no_ask"],
                                             c_secs_left, config)
                        c_brain = strategy_brain_s2(
                            btc_price, c_strike,
                            c_ob["best_yes_ask"], c_ob["best_no_ask"],
                            c_elapsed, c_secs_left, c_ticker,
                            asset=asset,
                        )
                        c_win_prob = c_brain.get("win_prob", 0.5)
                        c_entry    = c_ob["best_yes_ask"] if c_brain["side"] == "yes" else c_ob["best_no_ask"]
                        _c_fee_rate = config.get("kalshi_fee_per_contract_cents", 7) / 100
                        _c_p        = c_entry / 100.0
                        _c_fee      = _c_fee_rate * _c_p * (1.0 - _c_p)
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

    # Orderbook - retry next cycle if temporarily unavailable
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

    if ob is not None:
        bot_state._ticker_obi[ticker] = ob["obi"]

    if ob is None:
        log.warning(f"[{asset}] {ticker}: orderbook returned no price data - retrying next cycle")
        _snap = _no_data_eval("no orderbook data - retrying")
        if _use_state: state["eval"] = _snap
        else: bot_state._asset_eval[asset] = _snap
        bot_state.last_action, bot_state.last_skip_reason = "watching", "no price data - retrying"
        return

    yes_ask = ob["best_yes_ask"]
    no_ask  = ob["best_no_ask"]   # fetched directly from no_ask_dollars, not derived

    # Track YES price for velocity signal + de-vigged mid for the staleness gate, and
    # fold the quote into the implied-sigma anchor (no extra API calls - the book is
    # already in hand).
    track_contract_price(ticker, yes_ask)
    track_contract_mid(ticker, yes_ask, no_ask)
    # btc_price holds THIS asset's spot (the non-BTC loop passes the asset price).
    update_implied_sigma(asset, btc_price, strike, yes_ask, no_ask, secs_left, config)

    # Daily drawdown kill switch - only active when a positive limit is configured.
    # Default 0 = no daily loss cap (the bot keeps trading; bleed control is the EV gate).
    # Scoped to the main-line strategies: the lab slots' paper P&L must not halt S1/S2.
    _daily_limit = float(config.get("daily_loss_limit_dollars", 0))
    if _daily_limit > 0:
        _today_pnl = await db_get_today_pnl(mode=config.get("mode", "paper"),
                                            variants=("strategy1", "strategy2"))
        if _today_pnl <= -_daily_limit:
            log.warning(
                "DAILY LOSS LIMIT HIT: %.2f <= -%.2f - skipping all trades for today",
                _today_pnl, _daily_limit,
            )
            return

    # S2: spot fair-value dislocation per-asset strategy
    brain = strategy_brain_s2(btc_price, strike, yes_ask, no_ask, elapsed, secs_left, ticker,
                     asset=asset)
    # S1: BTC-lead cross-asset dislocation per-asset strategy
    brain_s1 = strategy_brain_s1(btc_price, strike, yes_ask, no_ask, elapsed, secs_left, ticker, asset=asset)

    # Edge-measurement: log both brains' decisions (once per ticker) for offline scoring.
    await _log_decision(brain, ticker, asset, secs_left, yes_ask, no_ask, config, "strategy2")
    await _log_decision(brain_s1, ticker, asset, secs_left, yes_ask, no_ask, config, "strategy1")

    # Shadow favorite-bias candidate (zero capital): one decision_log row per ticker so
    # the settlement backfill scores the buy-the-favorite idea alongside the live brains.
    if config.get("measurement_enabled", True) and config.get("shadow_fav_enabled", True):
        try:
            _fav_key = (ticker, "s_fav")
            if _fav_key not in _logged_decisions:
                _fav = shadow_fav_candidate(asset, btc_price, strike, yes_ask, no_ask,
                                            secs_left, config)
                if _fav is not None:
                    if len(_logged_decisions) > 5000:
                        _logged_decisions.clear()
                    _logged_decisions.add(_fav_key)
                    _fav["ticker"] = ticker
                    _fav["ts"] = datetime.now(timezone.utc).isoformat()
                    _fav["mode"] = config.get("mode", "paper")
                    await db_write_decision(_fav)
        except Exception as _fexc:
            log.debug("shadow_fav skipped for %s: %s", ticker, _fexc)

    await _execute_s1_trade(
        session, brain_s1, ticker, btc_price, strike, yes_ask, no_ask,
        elapsed, secs_left, asset, config, mode_s1, ob, market,
    )

    # Book tick for the S5 maker fill model: settlement scans this path to decide
    # whether a passive quote posted during READY would have filled. LOCKED appends
    # its own ticks; this covers tickers S2 never locks.
    try:
        bot_state._maker_track.setdefault(ticker, deque(maxlen=120)).append(
            (time.time(), float(yes_ask), float(no_ask)))
    except Exception:
        pass

    # Test-slot lab dispatch (S3+): every enabled registry brain evaluates this market,
    # logs its decision (skips included - the harness scores near-misses), and trades
    # through the generic paper executor. Slots never block each other or S1/S2.
    for _slot_id in enabled_slots(config):
        try:
            _slot_brain = STRATEGY_REGISTRY[_slot_id]["brain"](
                btc_price, strike, yes_ask, no_ask, elapsed, secs_left, ticker, asset=asset)
            _bump_slot_activity(_slot_id, _slot_brain)
            await _log_decision(_slot_brain, ticker, asset, secs_left, yes_ask, no_ask,
                                config, _slot_id)
            await _execute_slot_trade(
                session, _slot_id, _slot_brain, ticker, btc_price, strike,
                yes_ask, no_ask, secs_left, asset, config, ob, market)
        except Exception as _slot_exc:
            log.debug("[%s] slot eval failed for %s: %s", _slot_id, ticker, _slot_exc)

    side     = brain["side"]
    score    = brain["confidence"]
    do_trade = brain["action"] == "trade"
    skip_reason_ai = brain["reasoning"]

    # S1+S2 same-ticker dedup. In duel mode (default) both brains may hold opposing
    # positions on the same market - it is paper, so there is no capital conflict, and
    # suppressing S2 whenever S1 is active would poison the head-to-head comparison.
    # The bypass applies ONLY when both strategies are on paper: with real capital,
    # doubled/opposing live positions are never acceptable, duel or not.
    _duel_paper = (config.get("strategy_duel_mode", True)
                   and mode == "paper" and mode_s1 == "paper")
    if not _duel_paper:
        if do_trade and ticker in bot_state._s1_pending_trades:
            skip_reason_ai = "s2_dedup:s1_active"
            do_trade = False

    # allowed_sides gate - disable NO side when model is uncalibrated
    _side_aliases = {"up": "yes", "down": "no"}
    side = _side_aliases.get(side.lower(), side.lower()) if side else side
    _allowed_sides = get_asset_config(config, asset, "allowed_sides", config.get("allowed_sides"))
    _allowed_norm  = [_side_aliases.get(s.lower(), s.lower()) for s in (_allowed_sides or [])]
    if do_trade and _allowed_norm and side not in _allowed_norm:
        skip_reason_ai = f"side={side} not in allowed_sides={_allowed_sides}"
        do_trade = False

    # no_ask floor - block NO entries on nearly-resolved contracts
    if do_trade and side == "no":
        _min_no_ask = float(get_asset_config(config, asset, "min_no_ask_cents",
                                             config.get("min_no_ask_cents", 10.0)))
        if no_ask < _min_no_ask:
            skip_reason_ai = f"no_ask {no_ask:.0f}c below floor {_min_no_ask:.0f}c"
            do_trade = False

    # Consecutive price-filter skip tracking
    if brain.get("price_filter_skip"):
        bot_state._consecutive_price_skips += 1
        if bot_state._consecutive_price_skips == 20:
            _max_ep = get_asset_config(config, asset, "max_entry_price_cents", 82)
            log.warning(
                f"Price filter: {bot_state._consecutive_price_skips} consecutive skips - "
                f"all entry prices > {_max_ep}c"
            )
    else:
        bot_state._consecutive_price_skips = 0

    entry_price_cents = yes_ask if side == "yes" else no_ask
    _fee_rate = config.get("kalshi_fee_per_contract_cents", 7) / 100
    _entry_p  = entry_price_cents / 100.0
    _fee      = _fee_rate * _entry_p * (1.0 - _entry_p)
    brain_ev  = brain.get("win_prob", 0.5) - _entry_p - _fee
    brain_win_prob = brain.get("win_prob", 0.5)

    # S1/S2 direction arrows come from the side each brain actually leans (its fair-value
    # model), shown only once the evaluation reached the model stage; earlier gate skips
    # have no meaningful direction.
    def _brain_dir(b: dict) -> str:
        if b.get("signals", {}).get("model_raw_p_yes") is None:
            return "neutral"
        return "UP" if b.get("side") == "yes" else ("DOWN" if b.get("side") == "no" else "neutral")
    _s1_dir = _brain_dir(brain_s1)
    _s2_dir = _brain_dir(brain)
    # Gate funnels in the order the current brains check them (substring match below).
    _S1_GATE_ORD = ["s1_disabled", "s1_quiet_hours", "s1_session_gate", "s1_cooldown",
                    "s1_cap", "s1_rate_limit", "s1_window_guard", "s1_time_gate",
                    "s1_bad_price", "s1_no_data", "s1_thin_window", "s1_mom_flat",
                    "s1_no_confirm", "s1_btc_disagree", "s1_price_filter", "s1_ev_gate",
                    "s1_auto_gate"]
    _S2_GATE_ORD = ["s2_fv_disabled", "s2_quiet_hours", "s2_session_gate", "s2_time_gate",
                    "s2_fv_bad_price", "s2_degenerate", "s2_lowz", "s2_flicker",
                    "s2_wide_spread", "s2_no_market_data", "s2_not_favorite",
                    "s2_too_certain", "s2_price_filter", "s2_model_reject", "s2_auto_gate"]
    def _cnt_gates(gates, reason, traded):
        if traded: return len(gates)
        for i, g in enumerate(gates):
            if g in reason: return i
        return 0
    _s1_passed = _cnt_gates(_S1_GATE_ORD, brain_s1.get("reasoning", ""), brain_s1.get("action") == "trade")
    _s2_passed = _cnt_gates(_S2_GATE_ORD, brain.get("reasoning", ""), brain.get("action") == "trade")

    # Dashboard eval snapshot - updated at every exit point below
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
        "s1_dir":   _s1_dir,
        "s1_skip":  brain_s1.get("reasoning", ""),
        "s1_gates": {"passed": _s1_passed, "total": len(_S1_GATE_ORD)},
        "s2_dir":   _s2_dir,
        "s2_skip":  brain.get("reasoning", "") if brain.get("action") != "trade" else "",
        "s2_gates": {"passed": _s2_passed, "total": len(_S2_GATE_ORD)},
    }

    # Dashboard breakdown
    win_p_raw   = brain.get("win_prob", 0.5)
    _mom_label  = brain.get("mom_label",  "neutral")
    _vel_signal = brain.get("vel_signal", "neutral")
    _abs_pct    = brain.get("abs_pct", abs((btc_price - strike) / strike) if strike else 0.0)
    # Time score: less time remaining = outcome more certain = higher score (0->20)
    _time_score = round(max(0.0, min(20.0, 20.0 * (1.0 - secs_left / (15 * 60)))), 1)
    # Distance score: farther from strike = higher score (0->30, caps at 0.5%)
    _dist_score = round(min(30.0, _abs_pct * 100.0 / 0.5 * 30.0), 1)
    _brain_rv       = brain.get("_rv")
    _brain_vol_ratio = brain.get("_vol_ratio")
    breakdown = {
        "win_prob_raw":   round(win_p_raw * 100, 1),
        "win_prob_final": round(brain.get("win_prob", win_p_raw) * 100, 1),
        "ev":             round((brain.get("win_prob", 0.5) - entry_price_cents / 100 - _fee) * 100, 1),
        "contract_c":     round(entry_price_cents, 1),
        "momentum":       30 if _mom_label in ("yes", "no", "bullish", "bearish") else 0,
        "momentum_label": _mom_label,
        "velocity":       30 if _vel_signal in ("yes", "no", "favorable") else (10 if _vel_signal == "neutral" else 0),
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

    if not do_trade:
        log.info(f"{ticker}: watching - {skip_reason_ai}")
        await _log_entry(market, "READY", secs_left, btc_price, strike,
                         int(entry_price_cents), score, "skip", skip_reason_ai, mode)
        _eval_snap.update({"status": "SKIPPED", "skip_reason": skip_reason_ai})
        if _use_state: state["eval"] = dict(_eval_snap)
        else: bot_state._asset_eval[asset] = dict(_eval_snap)
        bot_state.last_action, bot_state.last_skip_reason = "watching", skip_reason_ai
        return

    # Daily limits - may flip mode to paper
    limit_hit, _ = await check_daily_limits(config)
    if limit_hit:
        config = read_config()
        mode = config.get("mode", "paper")

    # Cooldown disabled - trade every session regardless of prior outcome

    # Position sizing: quarter-Kelly on the shrunk win prob, scaled DOWN from the
    # configured clip (never up). Thin edges risk less; the clip stays the ceiling.
    trade_amount = _kelly_stake(brain.get("win_prob", 0.5), entry_price_cents, config)
    avail_liquidity = ob["yes_liquidity"] if side == "yes" else ob["no_liquidity"]
    contracts, dollars_used = calculate_contracts(
        trade_amount, int(entry_price_cents), avail_liquidity,
    )
    # The 90% fill check guards against thin books, not integer rounding: a small Kelly
    # stake at a 70c+ entry can only round to ~85% of target even with deep liquidity,
    # so apply the check only when liquidity actually capped the size.
    _wanted = int(float(trade_amount) * 100 / int(entry_price_cents)) if entry_price_cents else 0
    _liq_capped = avail_liquidity < _wanted
    if contracts == 0 or (_liq_capped and dollars_used < float(trade_amount) * 0.90):
        if contracts > 0:
            reason = (
                f"insufficient_liquidity: only {avail_liquidity} contracts available "
                f"(${dollars_used:.2f} of ${float(trade_amount):.0f} target - skip partial fill)"
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

    # Place order - mark ticker as attempted BEFORE placing so re-entry is blocked
    # even if the bot crashes or fill_confirmed comes back False
    if _use_state: state["order_attempted"].add(ticker)
    else: bot_state._s2_attempted_tickers.add(ticker)
    log.info(f"{ticker}: TRADE {side} {contracts}x @ {int(entry_price_cents)}c (score={score}, mode={mode})")
    result = await place_order(session, ticker, side, contracts, int(entry_price_cents), mode, market, asset=asset, secs_left=secs_left)

    fill_confirmed = result["fill_confirmed"]
    _fp = result.get("fill_price_cents")
    fill_price = _fp if _fp is not None else int(entry_price_cents)
    order_id = result.get("order_id")
    # Use actual filled contract count (IOC may fill fewer than requested).
    # Must use explicit None check - 0 is falsy but a valid (unfilled) count.
    _fc = result.get("filled_contracts")
    contracts = _fc if _fc is not None else contracts

    trade_ts = datetime.now(timezone.utc).isoformat()

    await _log_entry(
        market, "READY", secs_left, btc_price, strike, int(entry_price_cents),
        score, "trade" if fill_confirmed else "skip",
        "" if fill_confirmed else "order not filled",
        mode,
    )

    if not fill_confirmed and mode != "paper":
        # In live/demo mode, a network error in _verify_order_fill could mask a real fill.
        # Check the portfolio directly before declaring the order unfilled.
        try:
            if await _portfolio_has_position(session, ticker, side):
                # contracts count not updated - actual fill count unknown at this point;
                # the trade record will use the originally requested count (may overstate a partial fill).
                fill_price = int(entry_price_cents)
                fill_confirmed = True
                log.warning(
                    f"{ticker}: fill_confirmed=False but portfolio shows open position "
                    f"- treating as filled at {fill_price}c"
                )
        except Exception as _pf_exc:
            log.warning(f"{ticker}: portfolio fallback check failed: {_pf_exc}")

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
        "entry_signals":    json.dumps(brain.get("signals", {})),
        "strategy_variant": "strategy2",
        "strategy_version": bot_state._S2_VERSION,
        "brain": "s2",
    }
    trade_id = await db_write_trade(trade_data)
    if trade_id is None:
        log.critical("[S2] %s: DB write failed - position tracked in-memory only; reconcile manually", ticker)

    if config.get("notify_on_entry", False):
        try:
            _ends = datetime.now(timezone.utc) + timedelta(seconds=float(secs_left or 0))
            await send_telegram(bot_stats.format_entry_message(
                asset, "s2", side, fill_price, contracts,
                contracts * fill_price / 100.0, fmt_ts(_ends, config=config), mode))
        except Exception as _notify_exc:
            log.warning("Entry notification failed (non-fatal): %s", _notify_exc)

    _entry_ts = time.time()
    _abs_pct_at_entry = abs(btc_price - strike) / strike if strike else 0.0
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
        "elapsed_at_entry": _market_elapsed_at_entry,
        "market_close_time": market.get("close_time", ""),
        "order_id": order_id,
        "asset": asset,
    }
    # Seed the maker held-book track at entry so the counterfactual covers the entry->lock
    # window (handle_locked_phase only samples post-lock). Anchors t0 = entry_ts.
    if config.get("measurement_enabled", True):
        try:
            bot_state._maker_track.setdefault(ticker, deque(maxlen=120)).append(
                (_entry_ts, float(yes_ask), float(no_ask)))
            _maker_track_last_fetch[ticker] = _entry_ts
        except Exception:
            pass
    if _use_state:
        state["position"] = _new_position
        state["phase"] = "LOCKED"
    else:
        bot_state.current_position = _new_position
        bot_state.current_phase = "LOCKED"
        # Write state file immediately for BTC so crash recovery sees LOCKED phase.
        # Non-BTC positions are in _asset_states which write_state_file reads normally.
        await write_state_file(
            config, market, "LOCKED", secs_left, btc_price,
            score, bot_state.last_confidence_breakdown, "trade", "",
        )
    _eval_snap.update({"status": "TRADING", "skip_reason": ""})
    if _use_state: state["eval"] = dict(_eval_snap)
    else: bot_state._asset_eval[asset] = dict(_eval_snap)
    bot_state.last_action, bot_state.last_skip_reason = "trade", ""
    log.info(f"{ticker}: LOCKED.")
    _s2_dir = "UP" if side == "yes" else "DOWN"


async def handle_locked_phase(
    session: aiohttp.ClientSession,
    btc_price: float,
    secs_left: float,
    config: dict,
    asset: str = "BTC",
    state: dict | None = None,
) -> None:
    """
    Hold an open position to expiry - exit at settlement.
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
    # This is immune to market rollovers - the passed secs_left can be stale
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
        # Ask Kalshi for the official settlement result - retry up to 8x (40s)
        # to give the exchange time to settle the market before we guess.
        market_result = None
        for _attempt in range(8):
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

        # Track whether the outcome is the exchange's official result or our own
        # spot-price estimate, so settlement guesses are visible in the DB and
        # never silently masquerade as official (reconciliation can re-check later).
        _settled_official = market_result in ("yes", "no")
        if market_result == "yes":
            outcome = "win" if pos["side"] == "yes" else "loss"
        elif market_result == "no":
            outcome = "win" if pos["side"] == "no" else "loss"
        else:
            # Kalshi didn't settle in time - estimate from the asset price vs strike.
            # btc_price here is this asset's latest price (caller passes the asset price).
            log.warning(f"{ticker}: official settlement unavailable after 40s - "
                        f"estimating outcome from spot price {btc_price:.4g} vs strike {pos['strike']:.4g}")
            outcome = "win" if (
                (pos["side"] == "yes" and btc_price > pos["strike"]) or
                (pos["side"] == "no"  and btc_price <= pos["strike"])
            ) else "loss"

        _exit_reason = "expiry" if _settled_official else "expiry_estimated"
        log.info(f"{ticker}: result={market_result!r} ({'official' if _settled_official else 'ESTIMATED'}) -> {outcome}")
        # Edge-measurement: stamp the absolute YES/NO settlement onto this ticker's
        # decision_log rows (the periodic backfill covers skipped/untraded tickers).
        _settle_side = market_result if _settled_official else ("yes" if btc_price > pos["strike"] else "no")
        # Previous-window memory for the S6 window-carry brain: which way this window
        # resolved, how decisively, and when. Overwritten every settlement; the rollover
        # estimate below defers to this official entry for the same ticker.
        bot_state._prev_window_outcome[asset] = {
            "result": _settle_side, "strike": float(pos.get("strike") or 0.0),
            "spot_at_close": float(btc_price or 0.0), "ts": time.time(),
            "ticker": ticker, "estimated": not _settled_official,
        }
        # Settle lab-slot positions BEFORE the measurement block: the maker
        # counterfactual below pops _maker_track[ticker], which is the S5 fill
        # evidence the slot settler needs. Settling after it voided every S5 quote
        # on an S2-traded ticker as "unfilled". Guarded so a slot failure can never
        # block S2's own settlement below (which would leave the asset LOCKED).
        try:
            await _settle_slot_trades(ticker, market_result, btc_price, config)
        except Exception as _slot_exc:
            log.error("slot settlement failed for %s (S2 settle continues): %s",
                      ticker, _slot_exc, exc_info=True)
        _maker_exec = (config.get("maker_execution_enabled", False)
                       and pos.get("mode", config.get("mode", "paper")) == "paper")
        _maker_cf = None
        if config.get("measurement_enabled", True) or _maker_exec:
            await db_backfill_decision_outcome(ticker, _settle_side)
            _record_settlement_basis(ticker, asset, pos["strike"], btc_price, market_result, _settled_official)
            _maker_cf = await _record_maker_counterfactual(pos, asset, outcome, config)
        exit_price = 100 if outcome == "win" else 0
        _entry_p = pos["entry_price_cents"] / 100.0
        _fee_rate = config.get("kalshi_fee_per_contract_cents", 7) / 100.0
        fee = math.ceil(_fee_rate * pos["contracts"] * _entry_p * (1.0 - _entry_p) * 100) / 100
        pnl = (exit_price - pos["entry_price_cents"]) * pos["contracts"] / 100 - fee
        profit_pct = (exit_price - pos["entry_price_cents"]) / pos["entry_price_cents"] * 100 \
                     if pos["entry_price_cents"] else 0

        # Paper maker execution: re-price the settled trade as the resting maker order
        # the counterfactual tracked. Filled -> maker entry price + maker fee; not
        # filled -> the trade never happened (voided at $0, excluded from streaks).
        _entry_c_eff = pos["entry_price_cents"]
        if _maker_exec and _maker_cf is not None:
            if _maker_cf.get("filled"):
                _mp = float(_maker_cf["maker_price_cents"])
                _entry_c_eff = _mp
                _mp_frac = _mp / 100.0
                fee = math.ceil(0.0175 * pos["contracts"] * _mp_frac * (1.0 - _mp_frac) * 100) / 100
                pnl = (exit_price - _mp) * pos["contracts"] / 100 - fee
                profit_pct = (exit_price - _mp) / _mp * 100 if _mp else 0
                _exit_reason = "expiry_maker"
            else:
                pnl = 0.0
                profit_pct = 0.0
                outcome = "unfilled"
                _exit_reason = "maker_unfilled"

        log.info(f"{ticker} expired. Outcome={outcome}, P&L=${pnl:.2f} (fee=${fee:.2f})")
        pnl_str    = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"

        await db_update_trade(pos["trade_id"], {
            "exit_price_cents": exit_price,
            "exit_reason": _exit_reason,
            "outcome": outcome,
            "pnl_dollars": round(pnl, 2),
            "profit_percent": round(profit_pct, 2),
        })

        # Clear position immediately - before notifications so a Telegram failure
        # never leaves the asset stuck in LOCKED indefinitely.
        if _use_state:
            state["position"] = None
            state["phase"] = "DONE"
        else:
            bot_state.current_position = None
            bot_state.current_phase = "DONE"

        # Consecutive-loss tracker (no pause - informational only). A voided
        # maker-unfilled trade is neither a win nor a loss.
        if outcome == "win":
            bot_state._s2_consecutive_losses = 0
        elif outcome != "unfilled":
            bot_state._s2_consecutive_losses += 1
            max_cl = config.get("max_consecutive_losses", 5)
            # Alert once on the exact crossing - re-fires only after a win resets the streak.
            if bot_state._s2_consecutive_losses == max_cl:
                await send_telegram(f"S2: {bot_state._s2_consecutive_losses} consecutive losses (threshold {max_cl})")

        if outcome in ("win", "loss") and config.get("notify_on_settle", True):
            try:
                _pos_mode = pos.get("mode", config.get("mode", "paper"))
                _today_pnl = await db_get_today_pnl(_pos_mode)
                await send_telegram(bot_stats.format_settle_message(
                    outcome, pnl, asset, "s2", pos["side"], _entry_c_eff,
                    exit_price, pos["contracts"], fmt_ts(config=config), _today_pnl,
                    _pos_mode))
            except Exception as _notify_exc:
                log.warning("Settle notification failed (non-fatal): %s", _notify_exc)

        await _settle_s1_trade(ticker, market_result, btc_price, config, asset)
        return

    # Still in the market - just hold and log
    log.info(
        f"[HOLDING] {ticker} | side={pos['side'].upper()} | entry={pos['entry_price_cents']}c "
        f"| price=${btc_price:,.4g} | strike=${pos['strike']:,.4g} | {secs_left:.0f}s left"
    )

    # Maker instrumentation: record the held-book path (yes/no ask) so settlement can
    # decide whether a passive maker order posted at entry would have filled. Feeds both
    # the maker_log counterfactual and the paper maker execution model, so it must run
    # whenever either is on. Throttled to ~once per 25s/ticker.
    if config.get("measurement_enabled", True) or config.get("maker_execution_enabled", False):
        _now_mt = time.time()
        if _now_mt - _maker_track_last_fetch.get(ticker, 0.0) >= _MAKER_TRACK_MIN_INTERVAL:
            _maker_track_last_fetch[ticker] = _now_mt
            try:
                _hb = await fetch_orderbook(session, ticker, None)
                if _hb is not None:
                    _ya, _na = _hb.get("best_yes_ask"), _hb.get("best_no_ask")
                    if _ya is not None and _na is not None:
                        bot_state._maker_track.setdefault(ticker, deque(maxlen=120)).append(
                            (_now_mt, float(_ya), float(_na)))
                        track_contract_mid(ticker, _ya, _na)
                        update_implied_sigma(asset, btc_price, pos.get("strike"),
                                             _ya, _na, secs_left, config)
            except Exception as _mexc:
                log.debug("maker-track fetch failed for %s: %s", ticker, _mexc)


#  Non-BTC asset processing

def _init_asset_state(asset: str) -> dict:
    """Return a fresh per-asset state dict."""
    return {
        "phase": "DONE",
        "position": None,
        "order_attempted": set(),
        "prev_ticker": None,
        "market": None,
    }


async def _pick_best_strike(session, config: dict, asset: str, price: float,
                            default_market: dict) -> "dict | None":
    """
    Evaluate up to ladder_max_strikes candidate markets (sibling strikes in the
    current window plus the next window) with the S2 brain and return the one with
    the SMALLEST raw model-vs-market gap among trade signals. Win rate falls
    monotonically with the gap in the settled data, so maximizing EV inside the
    allowed band would re-create exactly the anti-predictive ordering that used to
    walk the ladder out to the cheapest strike. Ties break to the tighter spread.
    Falls back to default_market when no candidate fires or on any error. Bounded:
    caller throttles to every 30s.
    """
    try:
        max_n = int(config.get("ladder_max_strikes", 3))
        if max_n <= 1:
            return default_market
        candidates = await fetch_market_for_asset(session, asset, return_all=True)
        if not candidates or len(candidates) < 2:
            return default_market
        best, best_key = None, None
        for cand in candidates[:max_n]:
            c_ticker = cand.get("ticker")
            c_strike = parse_strike(cand)
            c_secs = seconds_remaining(cand)
            c_elapsed = seconds_elapsed(cand)
            if not c_ticker or c_strike is None or c_secs < 90:
                continue
            c_ob = await fetch_orderbook(session, c_ticker, cand)
            if not c_ob:
                continue
            # Free observations from a book already in hand: mid history for the
            # staleness gate and a quote for the implied-sigma anchor.
            track_contract_mid(c_ticker, c_ob["best_yes_ask"], c_ob["best_no_ask"])
            update_implied_sigma(asset, price, c_strike, c_ob["best_yes_ask"],
                                 c_ob["best_no_ask"], c_secs, config)
            c_brain = strategy_brain_s2(
                price, c_strike, c_ob["best_yes_ask"], c_ob["best_no_ask"],
                c_elapsed, c_secs, c_ticker, asset=asset,
            )
            if c_brain.get("action") != "trade":
                continue
            c_sig = c_brain.get("signals", {})
            # Smallest model-vs-market disagreement wins. The favorite-bias brain no
            # longer emits 'gap'; derive it from win_prob vs the market's own price
            # (the old key is kept as a fallback for any brain that still sends it).
            c_gap = c_sig.get("gap")
            if c_gap is None:
                _wp, _mp = c_sig.get("win_prob"), c_sig.get("mkt_p")
                if _wp is not None and _mp is not None:
                    c_gap = abs(float(_wp) - float(_mp))
            if c_gap is None:
                continue
            c_key = (float(c_gap), float(c_sig.get("spread_cents", 0.0) or 0.0))
            if best_key is None or c_key < best_key:
                best, best_key = cand, c_key
        if best is not None and best.get("ticker") != default_market.get("ticker"):
            log.info("[%s] ladder pick: %s (gap=%.3f spread=%.0fc) over %s",
                     asset, best.get("ticker"), best_key[0], best_key[1],
                     default_market.get("ticker"))
            return best
    except Exception as exc:
        log.debug("[%s] ladder pick skipped: %s", asset, exc)
    return default_market


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
        log.debug(f"[{asset}] no price yet - skipping")
        return
    age = _am_price_age(asset)
    if age is not None and age > 60:
        log.warning(f"[{asset}] price stale ({age:.0f}s) - skipping")
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
                log.warning(f"[{asset}] LOCKED: missing market_close_time - attempting forced settlement with secs_left=0")
            try:
                await handle_locked_phase(session, price, 0, config, asset=asset, state=st)
            except Exception as exc:
                log.error(f"[{asset}] LOCKED phase error (no market): {exc}", exc_info=True)
        else:
            log.debug(f"[{asset}] no active market")
            st["phase"] = "DONE"
            st["prev_ticker"] = None
        return

    _prev_market = st.get("market")
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
                log.info(f"[{asset}] strike TBD - using live price {strike:.2f}")
            else:
                log.warning(f"[{asset}] cannot parse strike - skipping")
                return
        else:
            log.warning(f"[{asset}] cannot parse strike - skipping")
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
            log.info(f"[{asset}] Market rolled to {ticker} but position still open on {prev_ticker} - staying LOCKED.")
            ticker = prev_ticker
            market = _prev_market or market
            st["market"] = market
        else:
            log.info(f"[{asset}] New market: {ticker} (was {prev_ticker}). Resetting to WATCH.")
            if prev_ticker in bot_state._s1_pending_trades:
                asyncio.create_task(_try_settle_orphaned_s1(session, prev_ticker, price, config, asset))
            asyncio.create_task(_settle_slot_rollover(session, prev_ticker, price, config))
            _record_prev_window_estimate(asset, prev_ticker, price)
            st["phase"] = "WATCH"
            st["position"] = None
            st["order_attempted"].discard(prev_ticker)
            st["prev_ticker"] = ticker

    # Remember this window's (ticker, strike) for the S6 rollover estimate. Paired from
    # the market object itself so LOCKED-rollover local mixing cannot mispair them.
    _remember_window_strike(asset, market)

    # WATCH
    if st["phase"] == "WATCH":
        if elapsed > bot_state.WATCH_PHASE_SECONDS:
            log.info(f"[{asset}] {ticker}: elapsed {elapsed:.0f}s -> READY.")
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
        # S1 runs independently - try entry even when S2 is LOCKED.
        # Block the final 90s: settlement-auction / liquidity-collapse zone where taker
        # entries are picked off as price snaps to 1c/99c.
        if secs_left > 90:
            try:
                ob_s1 = await fetch_orderbook(session, ticker, market)
                if ob_s1:
                    mode_s1 = config.get("s1_mode", config.get("mode", "paper"))
                    brain_s1 = strategy_brain_s1(
                        price, strike,
                        ob_s1["best_yes_ask"], ob_s1["best_no_ask"],
                        elapsed, secs_left, ticker, asset=asset,
                    )
                    await _execute_s1_trade(
                        session, brain_s1, ticker, price, strike,
                        ob_s1["best_yes_ask"], ob_s1["best_no_ask"],
                        elapsed, secs_left, asset, config, mode_s1, ob_s1, market,
                    )
                    # Lab slots also keep evaluating while S2 holds (paper - no conflict).
                    for _slot_id in enabled_slots(config):
                        try:
                            _sb = STRATEGY_REGISTRY[_slot_id]["brain"](
                                price, strike, ob_s1["best_yes_ask"], ob_s1["best_no_ask"],
                                elapsed, secs_left, ticker, asset=asset)
                            _bump_slot_activity(_slot_id, _sb)
                            await _log_decision(_sb, ticker, asset, secs_left,
                                                ob_s1["best_yes_ask"], ob_s1["best_no_ask"],
                                                config, _slot_id)
                            await _execute_slot_trade(
                                session, _slot_id, _sb, ticker, price, strike,
                                ob_s1["best_yes_ask"], ob_s1["best_no_ask"],
                                secs_left, asset, config, ob_s1, market)
                        except Exception as _sexc:
                            log.debug("[%s] slot LOCKED eval failed: %s", _slot_id, _sexc)
            except Exception as exc:
                log.debug("[%s] S1 LOCKED-phase entry attempt failed: %s", asset, exc)
        return

    # DONE
    if st["phase"] == "DONE":
        if secs_left > 3 * 60 and ticker not in st["order_attempted"]:
            log.info(f"[{asset}] DONE -> READY re-entry: {ticker} has {secs_left:.0f}s left.")
            st["phase"] = "READY"
        else:
            log.info(f"[{asset}] DONE. {secs_left:.0f}s left - waiting for next market.")
            return

    # READY
    if st["phase"] == "READY":
        # Best-strike ladder: evaluate sibling strikes / next windows and enter the
        # highest-EV one (mirrors the BTC multi-window picker). Throttled per asset.
        try:
            _now_lad = time.time()
            if _now_lad - st.get("last_ladder_ts", 0.0) >= 30.0:
                st["last_ladder_ts"] = _now_lad
                _lad_market = await _pick_best_strike(session, config, asset, price, market)
                if _lad_market is not None and _lad_market.get("ticker") != ticker:
                    _lad_strike = parse_strike(_lad_market)
                    if _lad_strike is not None:
                        market = _lad_market
                        ticker = market.get("ticker", ticker)
                        strike = _lad_strike
                        secs_left = seconds_remaining(market)
                        elapsed = seconds_elapsed(market)
        except Exception as exc:
            log.debug(f"[{asset}] ladder pick error: {exc}")
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
                # Populate PAUSED state so dashboard shows prices instead of OFFLINE.
                # Don't clobber LOCKED - those positions must still settle.
                for _pa in config.get("enabled_assets", ["ETH", "SOL", "XRP"]):
                    if _pa == "BTC":
                        continue
                    _pa_st = bot_state._asset_states.get(_pa)
                    if _pa_st is None:
                        # Use the full state shape so order_attempted/position exist - a bare
                        # stub caused a KeyError on st["order_attempted"] after re-enable.
                        bot_state._asset_states[_pa] = {**_init_asset_state(_pa), "phase": "PAUSED", "eval": {}}
                    elif _pa_st.get("phase") != "LOCKED":
                        _pa_st["phase"] = "PAUSED"
                # Still process any LOCKED assets so they can settle
                for _pa in list(config.get("enabled_assets", ["ETH", "SOL", "XRP"])):
                    if _pa != "BTC" and bot_state._asset_states.get(_pa, {}).get("phase") == "LOCKED":
                        try:
                            await _process_asset(session, config, _pa)
                        except Exception as _exc:
                            log.error("[%s] LOCKED settlement error (bot disabled): %s", _pa, _exc, exc_info=True)
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


#  Crash-recovery helpers

def _restore_without_verification(
    saved_pos: "dict | None",
    saved_phase: str,
    non_btc_positions: dict,
) -> None:
    """Restore positions from state file with no Kalshi check (paper mode or API error)."""
    if saved_pos and saved_phase == "LOCKED" and saved_pos.get("trade_id"):
        bot_state.current_position = saved_pos
        bot_state.current_phase = "LOCKED"
        log.warning(
            "Recovered open position from state file (unverified): "
            "trade_id=%s side=%s ticker=%s",
            saved_pos.get("trade_id"), saved_pos.get("side"), saved_pos.get("ticker"),
        )
    for _a, _apos in non_btc_positions.items():
        if _apos.get("phase") == "LOCKED" and _apos.get("position"):
            if _a not in bot_state._asset_states:
                bot_state._asset_states[_a] = {}
            bot_state._asset_states[_a]["phase"] = "LOCKED"
            bot_state._asset_states[_a]["position"] = _apos["position"]
            bot_state._asset_states[_a].setdefault("order_attempted", set())
            bot_state._asset_states[_a].setdefault("eval", {})
            log.warning(
                "Recovered LOCKED position for %s from state file (unverified): trade_id=%s",
                _a, _apos["position"].get("trade_id"),
            )


async def _verify_and_restore_positions(
    session: aiohttp.ClientSession,
    saved_pos: "dict | None",
    saved_phase: str,
    non_btc_positions: dict,
    mode: str,
) -> None:
    """
    Verify saved positions against Kalshi before trusting them.
    Sets bot_state.current_position, current_phase, _asset_states accordingly.
    Paper mode: restores unconditionally (no Kalshi to check).
    On fetch failure: restores from state file with recovery_unverified=True.
    """
    if mode == "paper":
        _restore_without_verification(saved_pos, saved_phase, non_btc_positions)
        return

    try:
        open_pos = await fetch_open_positions(session, mode)
    except Exception as exc:
        log.warning("Position verification fetch failed: %s - restoring unverified", exc)
        bot_state.recovery_unverified = True
        _restore_without_verification(saved_pos, saved_phase, non_btc_positions)
        return

    if open_pos is None:
        log.warning("Position verification returned None - restoring unverified")
        bot_state.recovery_unverified = True
        _restore_without_verification(saved_pos, saved_phase, non_btc_positions)
        return

    # BTC position
    if saved_pos and saved_phase == "LOCKED" and saved_pos.get("trade_id"):
        ticker = saved_pos.get("ticker", "")
        claimed = saved_pos.get("contracts", 0)
        if ticker in open_pos:
            kalshi_count = open_pos[ticker]["count"]
            if kalshi_count == claimed:
                bot_state.current_position = saved_pos
                bot_state.current_phase = "LOCKED"
                log.info(
                    "Recovered LOCKED position verified on Kalshi: trade_id=%s ticker=%s",
                    saved_pos.get("trade_id"), ticker,
                )
            else:
                log.warning(
                    "Position count mismatch %s: state=%d kalshi=%d - using Kalshi count",
                    ticker, claimed, kalshi_count,
                )
                verified = dict(saved_pos)
                verified["contracts"] = kalshi_count
                bot_state.current_position = verified
                bot_state.current_phase = "LOCKED"
                await send_telegram(
                    f"Position count mismatch on recovery: {ticker} "
                    f"state={claimed} kalshi={kalshi_count} - using Kalshi count"
                )
        else:
            log.warning(
                "State file claimed open position %s but Kalshi shows nothing - "
                "cleared. trade_id=%s", ticker, saved_pos.get("trade_id"),
            )
            bot_state.current_position = None
            bot_state.current_phase = ""
            await send_telegram(
                f"State file claimed open position {ticker} but Kalshi shows nothing - "
                "cleared, will be reconciled by next /portfolio/fills query."
            )

    # Non-BTC positions
    for _a, _apos in non_btc_positions.items():
        if _apos.get("phase") != "LOCKED" or not _apos.get("position"):
            continue
        _pos = _apos["position"]
        _ticker = _pos.get("ticker", "")
        _claimed = _pos.get("contracts", 0)
        if _ticker in open_pos:
            _kcount = open_pos[_ticker]["count"]
            if _a not in bot_state._asset_states:
                bot_state._asset_states[_a] = {}
            bot_state._asset_states[_a]["phase"] = "LOCKED"
            if _kcount != _claimed:
                _vpos = dict(_pos)
                _vpos["contracts"] = _kcount
                bot_state._asset_states[_a]["position"] = _vpos
                log.warning(
                    "Non-BTC %s count mismatch: state=%d kalshi=%d - using Kalshi count",
                    _a, _claimed, _kcount,
                )
                await send_telegram(
                    f"{_a} position count mismatch on recovery: "
                    f"state={_claimed} kalshi={_kcount}"
                )
            else:
                bot_state._asset_states[_a]["position"] = _pos
                log.info(
                    "Recovered LOCKED %s position verified on Kalshi: trade_id=%s",
                    _a, _pos.get("trade_id"),
                )
            bot_state._asset_states[_a].setdefault("order_attempted", set())
            bot_state._asset_states[_a].setdefault("eval", {})
        else:
            log.warning(
                "State file claimed %s LOCKED position %s but Kalshi shows nothing - "
                "cleared. trade_id=%s", _a, _ticker, _pos.get("trade_id"),
            )
            await send_telegram(
                f"State file claimed open position {_ticker} ({_a}) but Kalshi shows "
                "nothing - cleared, will be reconciled by next /portfolio/fills query."
            )


#  Main loop

async def main_loop() -> None:
    """
    Permanent 10-second loop driving all trading logic.
    All exceptions are caught per-iteration to prevent crashes.
    """

    global _last_orphan_settle_ts
    prev_ticker: str | None = None
    _mode = read_config().get("mode", "paper")

    # Restore the last persisted model calibration (prob_scale / basis offsets).
    _load_saved_calibration()

    # ── Read crash-recovery state (Kalshi verification happens inside session) ─
    _saved_pos: "dict | None" = None
    _saved_phase: str = ""
    _non_btc_positions: dict = {}
    try:
        with open(bot_state._STATE_FILE, "r") as _sf:
            _saved = json.load(_sf)
        _saved_pos   = _saved.get("open_position")
        _saved_phase = _saved.get("phase", "")
        _non_btc_positions = _saved.get("non_btc_positions", {})
        # Consecutive losses need no Kalshi - restore immediately.
        _saved_cl = _saved.get("consecutive_losses", 0)
        if isinstance(_saved_cl, int) and _saved_cl > 0:
            bot_state._s2_consecutive_losses = _saved_cl
        _saved_cl_s1 = _saved.get("s1_consecutive_losses", 0)
        if isinstance(_saved_cl_s1, int) and _saved_cl_s1 > 0:
            bot_state._s1_consecutive_losses = _saved_cl_s1
    except Exception:
        pass  # fresh start, no state to recover

    # TCPConnector with keepalive_timeout prevents stale pooled connections
    # from silently breaking API calls after many hours of uptime.
    connector = aiohttp.TCPConnector(keepalive_timeout=30, limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Probe clock skew before any authenticated Kalshi call.
        await _maybe_adjust_clock_skew(session)

        # Fire deferred demo-fallback alert if load_credentials disabled the bot.
        if bot_state.demo_fallback_alert:
            bot_state.demo_fallback_alert = False
            await send_telegram("SAFETY FALLBACK - DEMO creds missing. Bot DISABLED (paper mode forced).")

        # Verify saved positions against Kalshi before trusting the state file.
        await _verify_and_restore_positions(session, _saved_pos, _saved_phase, _non_btc_positions, _mode)

        # Settle any S1/slot positions that resolved while the bot was offline.
        _startup_config = read_config()
        await _settle_s1_orphans(session, _startup_config)
        await _settle_slot_orphans(session, _startup_config)
        _last_orphan_settle_ts = time.time()  # periodic timer starts after startup run

        # Non-BTC assets run in a separate background task so they aren't
        # gated by the BTC state machine's continue/sleep cycle.
        asyncio.create_task(_non_btc_asset_loop(session))

        while True:
            try:
                midnight_reset()

                # Periodic S1 orphan settlement - every 5 min, skipped while LOCKED
                # to avoid interfering with active position management.
                if bot_state.current_phase != "LOCKED":
                    _tick_ts = time.time()
                    if _tick_ts - _last_orphan_settle_ts >= 300:
                        try:
                            await _settle_s1_orphans(session, read_config())
                            await _settle_slot_orphans(session, read_config())
                            # Aged in-memory slot pendings: ladder-picked tickers never
                            # become a loop's prev_ticker, so no rollover settle fires
                            # for them. Anything older than a window + grace settles
                            # here via the official-result path.
                            _aged_cfg = read_config()
                            for _sst in list(bot_state._slot_state.values()):
                                for _tk, _pd in list(_sst.get("pending", {}).items()):
                                    if _tick_ts - float(_pd.get("entry_ts", _tick_ts)) > 18 * 60:
                                        _dq = asset_manager._prices.get(_pd.get("asset", ""))
                                        _sp = _dq[-1][1] if _dq else 0.0
                                        await _settle_slot_rollover(session, _tk, _sp, _aged_cfg)
                            # Prune held-book paths for tickers that never settled
                            # (READY-phase ticks accumulate for windows S2 skipped).
                            for _tk, _trk in list(bot_state._maker_track.items()):
                                if _trk and _tick_ts - _trk[-1][0] > 1800:
                                    bot_state._maker_track.pop(_tk, None)
                            # Publish lab activity counters for the dashboard process.
                            _dump_slot_activity()
                        except Exception as _oe:
                            log.error("Periodic orphan settlement error: %s", _oe, exc_info=True)
                        _last_orphan_settle_ts = time.time()

                # Periodic edge-measurement backfill: score skipped/untraded decisions.
                global _last_decision_backfill_ts
                if time.time() - _last_decision_backfill_ts >= 120:
                    if read_config().get("measurement_enabled", True):
                        await _backfill_pending_decisions(session)
                    _last_decision_backfill_ts = time.time()

                # Periodic self-calibration: refit prob_scale + basis offsets from the
                # bot's own settled data every 30 min.
                global _last_recalibration_ts
                if time.time() - _last_recalibration_ts >= 1800:
                    _recal_cfg = read_config()
                    if _recal_cfg.get("measurement_enabled", True) and \
                       _recal_cfg.get("calibration_enabled", True):
                        await _recalibrate_model(_recal_cfg)
                    _last_recalibration_ts = time.time()

                await _maybe_send_daily_summary()

                # Fresh config read
                try:
                    config = read_config()
                except Exception as exc:
                    log.error(f"Config read error: {exc}")
                    await asyncio.sleep(10)
                    continue

                if not config.get("bot_enabled", False) and bot_state.current_phase != "LOCKED":
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
                    log.warning(f"BTC price stale ({age}s old) - skipping cycle.")
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
                    # are expected - not errors.
                    if bot_state.current_phase == "LOCKED" and bot_state.current_position is not None:
                        _close_time = bot_state.current_position.get("market_close_time", "")
                        if not _close_time:
                            log.warning("LOCKED: missing market_close_time - attempting forced settlement with secs_left=0")
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
                        # The position is on the OLD ticker - keep monitoring it.
                        log.info(f"Market rolled to {ticker} but position still open on {prev_ticker} - staying LOCKED.")
                        # Keep using the old market object for SL monitoring this cycle
                        ticker = prev_ticker
                        market = bot_state._market_cache if bot_state._market_cache and bot_state._market_cache.get("ticker") == prev_ticker else market
                    else:
                        log.info(f"New market: {ticker} (was {prev_ticker}). Resetting to WATCH.")
                        if prev_ticker in bot_state._s1_pending_trades:
                            asyncio.create_task(_try_settle_orphaned_s1(session, prev_ticker, btc_price, config, "BTC"))
                        asyncio.create_task(_settle_slot_rollover(session, prev_ticker, btc_price, config))
                        _record_prev_window_estimate("BTC", prev_ticker, btc_price)
                        bot_state.current_phase = "WATCH"
                        bot_state.current_position = None
                        bot_state._s2_attempted_tickers.discard(prev_ticker)
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
                            log.info(f"{ticker}: strike TBD - using live price {strike:.2f}")
                        else:
                            log.warning(f"{ticker}: cannot parse strike. Skipping cycle.")
                            await asyncio.sleep(10)
                            continue
                    else:
                        log.warning(f"{ticker}: cannot parse strike. Skipping cycle.")
                        await asyncio.sleep(10)
                        continue

                # Remember this window's (ticker, strike) for the S6 rollover estimate.
                _remember_window_strike("BTC", market)

                # WATCH
                if bot_state.current_phase == "WATCH":
                    if elapsed > bot_state.WATCH_PHASE_SECONDS:
                        log.info(f"{ticker}: elapsed {elapsed:.0f}s -> READY.")
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

                # LOCKED
                if bot_state.current_phase == "LOCKED":
                    try:
                        await handle_locked_phase(
                            session, btc_price, secs_left, config
                        )
                    except Exception as exc:
                        log.error(f"LOCKED phase error: {exc}", exc_info=True)
                    # S1 runs independently - try entry even when S2 is LOCKED.
                    # Block the final 90s (settlement-auction / liquidity-collapse zone).
                    if secs_left > 90:
                        try:
                            ob_s1 = await fetch_orderbook(session, ticker, market)
                            if ob_s1:
                                mode_s1 = config.get("s1_mode", config.get("mode", "paper"))
                                brain_s1 = strategy_brain_s1(
                                    btc_price, strike,
                                    ob_s1["best_yes_ask"], ob_s1["best_no_ask"],
                                    elapsed, secs_left, ticker, asset="BTC",
                                )
                                await _execute_s1_trade(
                                    session, brain_s1, ticker, btc_price, strike,
                                    ob_s1["best_yes_ask"], ob_s1["best_no_ask"],
                                    elapsed, secs_left, "BTC", config, mode_s1, ob_s1, market,
                                )
                                # Lab slots keep evaluating while S2 holds (paper).
                                for _slot_id in enabled_slots(config):
                                    try:
                                        _sb = STRATEGY_REGISTRY[_slot_id]["brain"](
                                            btc_price, strike,
                                            ob_s1["best_yes_ask"], ob_s1["best_no_ask"],
                                            elapsed, secs_left, ticker, asset="BTC")
                                        _bump_slot_activity(_slot_id, _sb)
                                        await _log_decision(
                                            _sb, ticker, "BTC", secs_left,
                                            ob_s1["best_yes_ask"], ob_s1["best_no_ask"],
                                            config, _slot_id)
                                        await _execute_slot_trade(
                                            session, _slot_id, _sb, ticker, btc_price,
                                            strike, ob_s1["best_yes_ask"],
                                            ob_s1["best_no_ask"], secs_left, "BTC",
                                            config, ob_s1, market)
                                    except Exception as _sexc:
                                        log.debug("[%s] slot LOCKED eval failed: %s", _slot_id, _sexc)
                        except Exception as exc:
                            log.debug("S1 LOCKED-phase entry attempt failed: %s", exc)
                    await write_state_file(config, market, bot_state.current_phase, secs_left, btc_price,
                                           bot_state.last_confidence_score, bot_state.last_confidence_breakdown,
                                           bot_state.last_action, bot_state.last_skip_reason)
                    await asyncio.sleep(10)
                    continue

                # DONE
                if bot_state.current_phase == "DONE":
                    # Re-enter READY only if no order was attempted for this ticker.
                    # This prevents duplicate orders when fill_confirmed=False but
                    # the order actually went through on Kalshi.
                    if secs_left > 3 * 60 and ticker not in bot_state._s2_attempted_tickers:
                        log.info(
                            f"DONE -> READY re-entry: {ticker} has {secs_left:.0f}s left."
                        )
                        bot_state.current_phase = "READY"
                        # Fall through to READY handler below
                    else:
                        log.info(f"DONE phase. {secs_left:.0f}s left - waiting for next market.")
                        await write_state_file(config, market, bot_state.current_phase, secs_left, btc_price,
                                               bot_state.last_confidence_score, bot_state.last_confidence_breakdown,
                                               bot_state.last_action, bot_state.last_skip_reason)
                        await asyncio.sleep(10)
                        continue

                # READY
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
