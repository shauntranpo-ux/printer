"""
bot.py — Core trading logic for the Kalshi 15-minute prediction market bot.

Connects to Coinbase for live crypto prices, polls Kalshi for the
soonest-expiring 15-minute markets (ETH, SOL, XRP), evaluates
evidence-based strategy signals, places paper or live orders, and enforces
daily loss / profit limits. Writes bot_state.json every cycle for server.py.

Start via runner.py, not directly.
"""

import asyncio
import json
import math
import sqlite3
import logging
import os
import re
import sys
import tempfile
import time
from base64 import b64encode
from collections import deque
from datetime import datetime, timezone, timedelta

import aiosqlite
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from obi_monitor import OBIMonitor
import asset_manager
import bot_state
from bot_config import (
    atomic_write_json, read_config, write_config,
    get_asset_config, _init_config,
)
from bot_db import (
    init_db, test_db_write, db_write_trade,
    db_update_trade, db_write_market_log, db_get_today_pnl,
)
from asset_manager import (
    ASSET_CONFIG,
    get_price           as _am_get_price,
    price_age_seconds   as _am_price_age,
    coinbase_price_task,
)
from bot_kalshi import (
    load_credentials, kalshi_headers, get_btc_price,
    fetch_current_market, fetch_market_for_asset, parse_strike,
    seconds_remaining, seconds_elapsed, fetch_orderbook,
    _simulated_amm_midpoint, _log_price_validation,
)
from bot_notify import (
    _phase_for_eth, _notify_ctx,
    _maybe_fill_verification_notify, send_telegram,
)

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ logging â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("bot")

# Separate logger for Brain v3 decisions — writes to brain.log only
brain_log = logging.getLogger("brain")
brain_log.setLevel(logging.INFO)
brain_log.propagate = False   # don't bleed into the main bot log
_brain_fh = logging.FileHandler("brain.log", encoding="utf-8")
_brain_fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
brain_log.addHandler(_brain_fh)

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  BTC position vs strike
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Contract price velocity tracking
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def track_contract_price(ticker: str, price: float) -> None:
    """Record the latest contract ask price for velocity and lag analysis."""
    if ticker not in bot_state._contract_price_history:
        bot_state._contract_price_history[ticker] = deque(maxlen=60)
    bot_state._contract_price_history[ticker].append((time.time(), price))


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Printer Brain v3 — Empirically Calibrated from 4.5M rows of BTC 1-min data
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _session_ev_adjustment() -> float:
    return 0.0




def _strategy_name_for(asset, duration_min=15.0):
    """Human-readable strategy name for the dashboard per-asset card."""
    return {"BTC": "B3", "ETH": "E1", "SOL": "S1", "XRP": "X3", "DOGE": "D3"}.get(asset, "15m")


def _get_or_make_strategy_s2(asset: str, config, market_duration_min: float = 15.0):
    """Lazily construct per-asset strategy singleton. Returns None on failure."""
    # Fix sys.path FIRST so every strategies.* import resolves to src/strategies/.
    # The root strategies/ directory (YAML configs) would otherwise be picked up as a
    # namespace package and poison sys.modules['strategies'] before src/ is on the path.
    import sys as _sys
    _src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
    if _src not in _sys.path:
        # Purge any stale root-strategies namespace package cached before this fix runs
        for _k in [k for k in _sys.modules if k == "strategies" or k.startswith("strategies.")]:
            del _sys.modules[_k]
        _sys.path.insert(0, _src)
    try:
        mtime = os.path.getmtime(bot_state._CONFIG_FILE)
        if mtime != bot_state._config_mtime:
            bot_state._S2_SINGLETONS.clear()
            bot_state._config_mtime = mtime
            log.info("config.json changed — strategy singletons cleared")
    except OSError:
        pass
    try:
        from strategies.signals.time_windows import get_trading_window, get_window_params
        import time as _tw_now
        _new_window = get_trading_window(_tw_now.time(), config.get("timezone", "America/Los_Angeles"))
        if _new_window != bot_state._current_window:
            bot_state._S2_SINGLETONS.clear()
            bot_state._current_window = _new_window
            log.info("Trading window changed to %s — strategy singletons cleared", bot_state._current_window)
    except Exception:
        _new_window = bot_state._current_window or "normal"

    cache_key = asset
    if cache_key in bot_state._S2_SINGLETONS:
        return bot_state._S2_SINGLETONS[cache_key]
    try:
        from strategies.skip_layer import SkipConfig
        from strategies.signals.time_windows import get_window_params

        _min_price = float(config.get("min_entry_price_cents", 20.0))
        _max_price = float(config.get("max_entry_price_cents", 76.0))
        _tw = _new_window
        _wp = get_window_params(config, _tw)
        _max_price = min(_max_price, float(_wp["max_entry_price_cents"]))
        if _max_price <= _min_price:
            log.warning(
                "[%s] time_window=%s has max_entry=%.0fc <= min_entry=%.0fc — all entries will be blocked",
                asset, _tw, _max_price, _min_price,
            )
        skip_cfg = SkipConfig(
            max_spread_cents=float(get_asset_config(config, asset, "max_spread_cents", 3.0)),
            min_seconds_left=float(config.get("min_seconds_left", 30.0)),
            min_entry_price_cents=_min_price,
            max_entry_price_cents=_max_price,
            cold_start_samples=int(config.get("cold_start_samples", 60)),
            vol_ratio_threshold=float(get_asset_config(config, asset, "vol_gate_thresh", 1.80)),
        )
        overrides = config.get("asset_overrides", {}).get(asset, {})
        _ev_default = config.get("min_ev_base_15m", config.get("min_ev_base", 8))
        _ev_base = float(overrides.get("min_ev_base", _ev_default)) + float(_wp["min_ev_delta"])
        min_ev = _ev_base / 100.0
        stake = float(config.get("trade_amount_dollars", 25))

        from strategies.fifteen_min_strategy import FifteenMinStrategy
        strat = FifteenMinStrategy(
            asset=asset,
            skip_config=skip_cfg,
            min_ev=min_ev,
            stake_dollars=stake,
        )

        bot_state._S2_SINGLETONS[cache_key] = strat
        log.info(f"Strategy initialized: {cache_key} (15m, stake=${stake})")
        return strat
    except Exception as exc:
        log.warning(f"{asset} strategy init failed, falling back to legacy: {exc}")
        return None


def strategy_brain_s2(
    btc_price, strike, yes_ask, no_ask,
    elapsed_seconds, secs_left, ticker,
    min_ev_base=3.0, vol_gate_thresh=1.80, kalshi_fee=0.07,
    asset="BTC", max_entry_price_cents=100.0,
    min_reward_cents=0.0, max_risk_reward_ratio=999.0,
):
    """Dispatch to FifteenMinStrategy (D3 hybrid). Returns brain dict tagged strategy2."""
    config = read_config()

    market_duration_min = (elapsed_seconds + secs_left) / 60.0
    strat = _get_or_make_strategy_s2(asset, config, market_duration_min=market_duration_min)
    if strat is None:
        # No validated strategy for this asset/duration. Skipping is better than
        # using the legacy printer_brain which has no calibrated edge on these markets
        # and produces random-confidence outputs (observed 50/50 win rate in paper trading).
        log.info(
            f"No strategy for {asset} at {market_duration_min:.0f}min "
            f"skipping (no strategy for duration)"
        )
        _above = btc_price > strike if strike > 0 else False
        return {
            "action": "skip",
            "side": "yes" if _above else "no",
            "confidence": 50,
            "reasoning": f"no_strategy:{asset}_{market_duration_min:.0f}min",
            "key_signals": [],
            "signals": {},
            "win_prob": 0.5,
            "mom_label": "no_strategy",
            "mom_pct": 0.0,
            "vel_signal": "neutral",
            "raw_p_yes": None,
            "mins_left": secs_left / 60.0,
            "abs_pct": abs(btc_price - strike) / strike if strike > 0 else 0.0,
            "above": _above,
            "_rv": None,
            "_vol_ratio": None,
            "price_filter_skip": False,
        }

    from strategies.feature_builder import build_features_from_bot_state
    try:
        if asset == "BTC":
            prices_deque = bot_state.btc_prices
            current_price = btc_price
        else:
            prices_deque = asset_manager._prices.get(asset)
            if not prices_deque:
                return {
                    "action": "skip", "side": "no", "confidence": 50,
                    "reasoning": f"no_price_feed:{asset}",
                    "key_signals": [], "signals": {}, "win_prob": 0.5,
                    "mom_label": "no_data", "mom_pct": 0.0, "vel_signal": "neutral",
                    "raw_p_yes": None, "mins_left": secs_left / 60.0,
                    "abs_pct": 0.0, "above": False, "_rv": None, "_vol_ratio": None,
                    "price_filter_skip": False,
                }
            current_price = prices_deque[-1][1]

        features = build_features_from_bot_state(
            asset=asset,
            ticker=ticker,
            current_price=current_price,
            strike=strike,
            btc_price=btc_price,
            seconds_left=secs_left,
            elapsed_seconds=elapsed_seconds,
            yes_ask=yes_ask,
            no_ask=no_ask,
            yes_bid=max(0.0, yes_ask - 1.0),
            no_bid=max(0.0, no_ask - 1.0),
            prices_deque=prices_deque,
            contract_history=bot_state._contract_price_history.get(ticker),
            btc_prices_deque=bot_state.btc_prices,
        )
    except Exception as exc:
        log.warning(f"{asset} feature_builder failed — skipping (not falling back to legacy): {exc}")
        _above = current_price > strike if strike > 0 else False
        return {
            "action": "skip", "side": "yes" if _above else "no", "confidence": 50,
            "reasoning": f"feature_builder_error:{exc.__class__.__name__}",
            "key_signals": [], "signals": {}, "win_prob": 0.5,
            "mom_label": "error", "mom_pct": 0.0, "vel_signal": "neutral",
            "raw_p_yes": None, "mins_left": secs_left / 60.0,
            "abs_pct": abs(current_price - strike) / strike if strike > 0 else 0.0,
            "above": _above, "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
        }

    try:
        decision = strat.decide(features)
    except Exception as exc:
        log.warning(f"{asset} strat.decide() failed — skipping (not falling back to legacy): {exc}")
        _above = current_price > strike if strike > 0 else False
        return {
            "action": "skip", "side": "yes" if _above else "no", "confidence": 50,
            "reasoning": f"decide_error:{exc.__class__.__name__}",
            "key_signals": [], "signals": {}, "win_prob": 0.5,
            "mom_label": "error", "mom_pct": 0.0, "vel_signal": "neutral",
            "raw_p_yes": None, "mins_left": secs_left / 60.0,
            "abs_pct": abs(current_price - strike) / strike if strike > 0 else 0.0,
            "above": _above, "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
        }

    above = current_price > strike
    naive = "yes" if above else "no"
    if decision.side is not None and decision.side != naive:
        brain_log.info(
            f"ROUTER_FLIPPED {asset} {ticker} | px={current_price:.4f} "
            f"strike={strike:.4f} naive={naive} picked={decision.side} | "
            f"yes_ev={decision.contributing_signals.get('yes_ev', float('nan')):+.3f} "
            f"no_ev={decision.contributing_signals.get('no_ev', float('nan')):+.3f} | "
            f"mode={decision.contributing_signals.get('decision_mode', '?')}"
        )

    abs_pct = abs(current_price - strike) / strike
    # new base.py sets p_model = P(chosen_side_wins) already; no inversion needed
    true_p = decision.p_model
    if decision.action == "trade":
        _st = decision.contributing_signals.get("supertrend_direction")
        _mkt = decision.contributing_signals.get("market_prob")
        log.info("[%s] signal=supertrend st=%s market=%.3f side=%s",
                 asset, _st, _mkt or 0, decision.side)
    return {
        "action": decision.action,
        "side": decision.side if decision.side else naive,
        "confidence": int(round(true_p * 100)),
        "reasoning": decision.reason,
        "key_signals": [f"{k}: {v}" for k, v in decision.contributing_signals.items()],
        "signals": dict(decision.contributing_signals),
        "win_prob": float(true_p),  # P(chosen side wins), used by confidence gate
        "mom_label": decision.contributing_signals.get(
            "regime", decision.contributing_signals.get("mom_label", "neutral")
        ),
        "mom_pct": float(decision.contributing_signals.get(
            "regime_adj", decision.contributing_signals.get("mom_adj", 0.0)
        )),
        "vel_signal": decision.contributing_signals.get(
            "velocity", decision.contributing_signals.get("vel_signal", "neutral")
        ),
        "raw_p_yes": decision.contributing_signals.get("raw_p_yes"),
        "mins_left": secs_left / 60.0,
        "abs_pct": abs_pct,
        "above": above,
        "_rv": features.realized_vol_1min,
        "_vol_ratio": None,
        "price_filter_skip": False,
        "strategy_variant": "strategy2",
        "strategy_version":  bot_state._S2_VERSION,
    }



# ── S1: BV3 printer_brain constants (April 2026 profitable strategy) ──────────
_S1_BV3_TABLE = [
    # 1min   2min   3min   4min   5min   6min   7min   8min   9min  10min  11min  12min  13min
    [0.850, 0.796, 0.758, 0.727, 0.705, 0.686, 0.672, 0.656, 0.639, 0.624, 0.606, 0.595, 0.578],  # 0.0-0.1%
    [0.980, 0.956, 0.931, 0.904, 0.876, 0.856, 0.833, 0.807, 0.783, 0.752, 0.733, 0.706, 0.675],  # 0.1-0.2%
    [0.994, 0.983, 0.967, 0.951, 0.933, 0.909, 0.889, 0.868, 0.835, 0.811, 0.788, 0.756, 0.713],  # 0.2-0.3%
    [0.997, 0.990, 0.981, 0.968, 0.950, 0.935, 0.917, 0.893, 0.874, 0.840, 0.816, 0.778, 0.741],  # 0.3-0.4%
    [0.998, 0.993, 0.987, 0.977, 0.962, 0.948, 0.932, 0.908, 0.883, 0.869, 0.835, 0.809, 0.782],  # 0.4-0.5%
    [0.998, 0.997, 0.988, 0.979, 0.968, 0.960, 0.944, 0.925, 0.913, 0.876, 0.849, 0.824, 0.781],  # 0.5-0.6%
    [0.999, 0.994, 0.994, 0.979, 0.974, 0.963, 0.947, 0.936, 0.914, 0.897, 0.872, 0.839, 0.817],  # 0.6-0.75%
    [0.999, 0.996, 0.995, 0.988, 0.982, 0.968, 0.963, 0.942, 0.917, 0.905, 0.884, 0.845, 0.818],  # 0.75-1.0%
    [1.000, 0.999, 0.994, 0.992, 0.984, 0.980, 0.967, 0.964, 0.935, 0.919, 0.911, 0.862, 0.820],  # 1.0-1.25%
    [1.000, 0.997, 0.995, 0.991, 0.986, 0.972, 0.971, 0.960, 0.942, 0.921, 0.904, 0.874, 0.820],  # 1.25%+
]
_S1_BV3_DIST_BOUNDS = [0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.0075, 0.010, 0.0125]


def _s1_empirical_win_prob(asset: str, abs_pct: float, mins_left: float) -> float:
    """BV3 table lookup with live-correction via _brain_cal prob_scale."""
    vol_ratio = bot_state._S1_ASSET_VOL_RATIO.get(asset, 1.0)
    effective_pct = abs_pct / vol_ratio  # normalise to BTC-equivalent risk distance
    row = len(_S1_BV3_DIST_BOUNDS)
    for i, bound in enumerate(_S1_BV3_DIST_BOUNDS):
        if effective_pct < bound:
            row = i
            break
    col = max(0, min(12, int(round(mins_left)) - 1))
    base_prob = _S1_BV3_TABLE[row][col]
    prob_scale = bot_state._brain_cal_s1.get("prob_scale", 1.0)
    return float(0.50 + (base_prob - 0.50) * prob_scale)


def _s1_calculate_momentum(prices, seconds: int = 180, threshold: float = 0.0005) -> tuple:
    """Return (pct_change, label) over the last `seconds` of price data."""
    if not prices or len(prices) < 2:
        return 0.0, "neutral"
    now = prices[-1][0]
    cutoff = now - seconds
    old = [(ts, p) for ts, p in prices if ts <= cutoff]
    ref = old[-1][1] if old else prices[0][1]
    current = prices[-1][1]
    if ref <= 0:
        return 0.0, "neutral"
    pct = (current - ref) / ref
    label = "bullish" if pct > threshold else ("bearish" if pct < -threshold else "neutral")
    return pct, label


def _s1_realized_vol(prices, window_minutes: int = 10) -> float:
    """Realized vol: std of recent log returns over window_minutes."""
    if not prices or len(prices) < 2:
        return 0.001
    now = prices[-1][0]
    recent = [p for ts, p in prices if ts >= now - window_minutes * 60]
    if len(recent) < 2:
        return 0.001
    rets = [math.log(recent[i] / recent[i - 1]) for i in range(1, len(recent)) if recent[i - 1] > 0]
    if not rets:
        return 0.001
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return math.sqrt(var) if var > 0 else 0.001


def _s1_contract_velocity(ticker: str) -> str:
    """favorable/unfavorable/neutral based on recent contract price trend."""
    history = bot_state._contract_price_history.get(ticker)
    if not history or len(history) < 4:
        return "neutral"
    prices = [p for _, p in history]
    recent_avg = sum(prices[-3:]) / 3
    old_avg = sum(prices[:3]) / 3
    delta = recent_avg - old_avg
    if delta > 0.5:
        return "favorable"
    if delta < -0.5:
        return "unfavorable"
    return "neutral"


def strategy_brain_s1(
    btc_price, strike, yes_ask, no_ask,
    elapsed_seconds, secs_left, ticker,
    asset="BTC",
):
    """printer_brain v3 (April 2026 profitable strategy) tagged strategy1.

    BV3 empirical win-probability table (distance x time) + momentum + velocity + vol gate.
    Continuation-only: YES above strike, NO below -- never contrarian.
    """
    config = read_config()

    above = btc_price > strike if strike > 0 else False
    abs_pct = abs(btc_price - strike) / strike if strike > 0 else 0.0
    mins_left = secs_left / 60.0

    # price feed
    if asset == "BTC":
        prices_list = list(bot_state.btc_prices)
    else:
        raw = asset_manager._prices.get(asset)
        prices_list = list(raw) if raw else []

    # vol gate
    _rv = _s1_realized_vol(prices_list) if prices_list else 0.001
    _vol_ratio = _rv * (mins_left ** 0.5) / abs_pct if abs_pct > 0 else 999.0
    _vol_gate_thresh = float(config.get("vol_gate_thresh", 1.80))

    if _vol_ratio >= _vol_gate_thresh:
        return {
            "action": "skip", "side": "yes" if above else "no",
            "confidence": 50,
            "reasoning": f"s1_vol_gate:{_vol_ratio:.2f}>={_vol_gate_thresh:.2f}",
            "key_signals": [f"vol_ratio:{_vol_ratio:.2f}", f"rv:{_rv:.5f}"],
            "signals": {"vol_ratio": _vol_ratio, "_rv": _rv},
            "win_prob": 0.5, "mom_label": "neutral", "mom_pct": 0.0,
            "vel_signal": "neutral", "raw_p_yes": None, "mins_left": mins_left,
            "abs_pct": abs_pct, "above": above, "_rv": _rv, "_vol_ratio": _vol_ratio,
            "price_filter_skip": False, "strategy_variant": "strategy1",
        }

    # price filter
    _min_price = float(config.get("min_entry_price_cents", 20.0))
    _max_price = float(config.get("max_entry_price_cents", 76.0))
    _entry_price = yes_ask if above else no_ask
    if _entry_price < _min_price or _entry_price > _max_price:
        return {
            "action": "skip", "side": "yes" if above else "no",
            "confidence": 50,
            "reasoning": f"s1_price_filter:{_entry_price:.0f}c not in [{_min_price:.0f},{_max_price:.0f}]",
            "key_signals": [f"entry:{_entry_price:.0f}c"],
            "signals": {"entry_price": _entry_price},
            "win_prob": 0.5, "mom_label": "neutral", "mom_pct": 0.0,
            "vel_signal": "neutral", "raw_p_yes": None, "mins_left": mins_left,
            "abs_pct": abs_pct, "above": above, "_rv": _rv, "_vol_ratio": _vol_ratio,
            "price_filter_skip": True, "strategy_variant": "strategy1",
        }

    # win probability (BV3)
    win_prob = _s1_empirical_win_prob(asset, abs_pct, mins_left)

    # momentum adjustment — vol-normalize threshold so signal only fires
    # on moves that exceed 1.5x the per-period realized noise floor.
    # _rv is per-minute vol; scale to 3-min window then threshold.
    _rv_3min = (_rv or 0.001) * math.sqrt(3)
    _mom_threshold = max(0.0005, 1.5 * _rv_3min)
    mom_pct, mom_label = _s1_calculate_momentum(prices_list, threshold=_mom_threshold)
    if mom_label == "bullish":
        mom_adj = +0.05 if above else -0.05
    elif mom_label == "bearish":
        mom_adj = -0.05 if above else +0.05
    else:
        mom_adj = 0.0
    win_prob = max(0.05, min(0.98, win_prob + mom_adj))

    # velocity adjustment
    vel_signal = _s1_contract_velocity(ticker)
    vel_adj = +0.01 if vel_signal == "favorable" else (-0.01 if vel_signal == "unfavorable" else 0.0)
    if not above:
        vel_adj = -vel_adj
    win_prob = max(0.05, min(0.98, win_prob + vel_adj))

    # market anchor: sanity-check against AMM when model diverges strongly.
    # Trigger raised 25%->35% and pull reduced 40%->15% so real edge is
    # preserved; anchor only corrects extreme overconfidence.
    mkt_implied = _entry_price / 100.0
    diff = win_prob - mkt_implied
    if abs(diff) > 0.35:
        win_prob = win_prob - 0.15 * diff

    # OBI adjustment: near-ATM only, top-10 Coinbase orderbook imbalance.
    # Positive OBI (bid-heavy) supports continuation when price is above strike.
    if bot_state._obi_monitor is not None and abs_pct < 0.004:
        _obi_val = bot_state._obi_monitor.get_obi(asset)
        if _obi_val is not None:
            _obi_adj = 0.02 * _obi_val if above else -0.02 * _obi_val
            win_prob = max(0.05, min(0.98, win_prob + _obi_adj))

    # BTC/ETH funding dispersion adjustment (cross-venue imbalance signal).
    if asset in ("BTC", "ETH"):
        _fm = bot_state._funding_monitor_btc if asset == "BTC" else bot_state._funding_monitor_eth
        if _fm is not None:
            from strategies.original.signals.funding_dispersion import funding_dispersion_adjustment as _fda
            _fdisp = _fm.current_dispersion()
            _fadj, _ = _fda(_fdisp)
            if _fadj != 0.0:
                win_prob = max(0.05, min(0.98, win_prob + (_fadj if above else -_fadj)))

    # win_prob = P(continuation side wins): YES when above, NO when not above.
    # EV for each side for logging; ev is the actionable continuation EV.
    yes_ev = (win_prob if above else 1.0 - win_prob) - (yes_ask / 100.0) - 0.07
    no_ev = (1.0 - win_prob if above else win_prob) - (no_ask / 100.0) - 0.07
    ev = win_prob - (_entry_price / 100.0) - 0.07
    if above and bot_state._brain_cal_s1.get("bullish_wr", 0.5) < 0.35:
        ev -= 0.04
    if not above and bot_state._brain_cal_s1.get("bearish_wr", 0.5) < 0.35:
        ev -= 0.04

    # continuation direction only
    side = "yes" if above else "no"

    # EV gate — prefer asset-specific min_ev_base_s1, then asset min_ev_base,
    # then global min_ev_base_15m. Lets S1 and S2 have independent per-asset thresholds.
    _ev_s1_default = float(config.get("min_ev_base_15m", config.get("min_ev_base", 9)))
    _asset_cfg_s1 = config.get("asset_overrides", {}).get(asset, {})
    _min_ev = float(_asset_cfg_s1.get(
        "min_ev_base_s1", _asset_cfg_s1.get("min_ev_base", _ev_s1_default)
    )) / 100.0
    if ev < _min_ev:
        return {
            "action": "skip", "side": side,
            "confidence": int(win_prob * 100),
            "reasoning": f"s1_ev_gate:{ev:.3f}<{_min_ev:.3f}",
            "key_signals": [f"ev:{ev:.3f}", f"win_prob:{win_prob:.3f}", mom_label, vel_signal],
            "signals": {"yes_ev": yes_ev, "no_ev": no_ev, "win_prob": win_prob,
                        "mom_label": mom_label, "mom_pct": mom_pct, "vel_signal": vel_signal,
                        "vol_ratio": _vol_ratio, "_rv": _rv},
            "win_prob": float(win_prob), "mom_label": mom_label, "mom_pct": mom_pct,
            "vel_signal": vel_signal, "raw_p_yes": float(win_prob) if above else float(1.0 - win_prob), "mins_left": mins_left,
            "abs_pct": abs_pct, "above": above, "_rv": _rv, "_vol_ratio": _vol_ratio,
            "price_filter_skip": False, "strategy_variant": "strategy1",
        }

    return {
        "action": "trade", "side": side,
        "confidence": int(win_prob * 100),
        "reasoning": f"bv3 ev={ev:.3f} win_prob={win_prob:.3f} {mom_label} {vel_signal} dist={abs_pct:.3%} mins={mins_left:.1f}",
        "key_signals": [f"ev:{ev:.3f}", f"win_prob:{win_prob:.3f}", mom_label, vel_signal,
                        f"dist:{abs_pct:.3%}", f"mins:{mins_left:.1f}"],
        "signals": {"yes_ev": yes_ev, "no_ev": no_ev, "win_prob": win_prob,
                    "mom_label": mom_label, "mom_pct": mom_pct, "vel_signal": vel_signal,
                    "vol_ratio": _vol_ratio, "_rv": _rv, "abs_pct": abs_pct,
                    "mins_left": mins_left},
        "win_prob": float(win_prob), "mom_label": mom_label, "mom_pct": mom_pct,
        "vel_signal": vel_signal, "raw_p_yes": float(win_prob) if above else float(1.0 - win_prob), "mins_left": mins_left,
        "abs_pct": abs_pct, "above": above, "_rv": _rv, "_vol_ratio": _vol_ratio,
        "price_filter_skip": False, "strategy_variant": "strategy1",
    }


# â”€â”€ End feature-flagged routing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


from bot_orders import (
    calculate_contracts, implied_prob,
    _portfolio_has_position, _verify_order_fill, place_order,
)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Daily limits
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  State file
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

_STRIKE_RE_T_SUFFIX = re.compile(r"-T(\d+)$")
_STRIKE_RE_NUMERIC_SUFFIX = re.compile(r"-(\d+)$")

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


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Phase handlers
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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

    # â”€â”€ Multi-window best-pick (BTC only) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

    # â”€â”€ Price validation: compare simulated vs real prices â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

    # â”€â”€ Printer Brain — primary decision engine (always runs, no API needed) â”€â”€
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

    # â”€â”€ allowed_sides gate — disable NO side when model is uncalibrated â”€â”€â”€â”€â”€â”€
    _side_aliases = {"up": "yes", "down": "no"}
    side = _side_aliases.get(side.lower(), side.lower()) if side else side
    _allowed_sides = get_asset_config(config, asset, "allowed_sides", config.get("allowed_sides"))
    _allowed_norm  = [_side_aliases.get(s.lower(), s.lower()) for s in (_allowed_sides or [])]
    if do_trade and _allowed_norm and side not in _allowed_norm:
        skip_reason_ai = f"side={side} not in allowed_sides={_allowed_sides}"
        do_trade = False


    # â”€â”€ Consecutive price-filter skip tracking â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

    # â”€â”€ Reversal model — runs whenever main strategy skips â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
    dir_icon   = "YES" if side == "yes" else "NO"
    _win_prob_used = _rev["prob"] if _is_reversal and _rev else brain.get("win_prob", 0)
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


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Non-BTC asset processing
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Main loop
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def main_loop() -> None:
    """
    Permanent 10-second loop driving all trading logic.
    All exceptions are caught per-iteration to prevent crashes.
    """

    prev_ticker: str | None = None

    # â”€â”€ Recover open position and consecutive-loss state after a crash/restart â”€
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

                # â”€â”€ WATCH â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

                # â”€â”€ LOCKED â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

                # â”€â”€ DONE â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

                # â”€â”€ READY â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Entry point
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def verify_kalshi_connection(session: aiohttp.ClientSession) -> None:
    """Verify Kalshi credentials work and log all available BTC market series."""
    # Auth check via /portfolio/balance — avoids market query-exchange service entirely
    balance_path = "/portfolio/balance"
    try:
        async with session.get(
            bot_state.KALSHI_BASE_URL + balance_path,
            headers=kalshi_headers("GET", balance_path),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json()
            if resp.status == 401:
                log.error("KALSHI AUTH FAILED (401) — check KALSHI_API_KEY and KALSHI_PRIVATE_KEY")
                sys.exit(1)
            if resp.status != 200:
                log.error(f"Kalshi connection check failed: HTTP {resp.status} — {data}")
                sys.exit(1)
            balance = data.get("balance", "?")
            log.info(f"Kalshi auth OK. Account balance: {balance} cents")
    except SystemExit:
        raise
    except Exception as exc:
        log.error(f"Kalshi connection check failed: {exc}")
        sys.exit(1)

    path = "/markets"

    # â”€â”€ Market discovery: log everything BTC-related so we can find the right ticker â”€â”€
    now_utc = datetime.now(timezone.utc)
    log.info("=== KALSHI MARKET DISCOVERY START ===")

    # 1. Try every known series ticker (KXBTCD / BTCD-B first — the active "above/below" BTC markets)
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

    # 2. Broad scan (avoids Kalshi 500 on filterless queries)
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

    # 3. /series endpoint — find any BTC-related series
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

    LIVE mode + unresolved issues  → sys.exit(1). Hard stop.
    PAPER mode + unresolved issues → warn and continue (bot must run to collect data).
    preflight_override: true in config.json  → skip the live-mode block (NOT RECOMMENDED).
    """
    issues: list[str] = []
    W = 60

    # â”€â”€ Check 1: Price validation data â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if not os.path.isfile(bot_state._PRICE_VAL_CSV):
        issues.append(
            "NO PRICE VALIDATION DATA — price_validation_log.csv does not exist. "
            "Run paper mode for 200+ cycles first."
        )
    else:
        try:
            with open(bot_state._PRICE_VAL_CSV, encoding="utf-8") as _f:
                row_count = max(0, sum(1 for _ in _f) - 1)   # minus header
        except Exception:
            row_count = 0
        if row_count < 200:
            issues.append(
                f"INSUFFICIENT PRICE VALIDATION — only {row_count}/200 samples collected. "
                "Keep running paper mode."
            )

    # â”€â”€ Check 2: Fee constant is set and > 0 â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    fee = config.get("kalshi_fee_per_contract_cents", 0)
    if not (isinstance(fee, (int, float)) and fee > 0):
        issues.append(
            f"FEE NOT CONFIGURED — kalshi_fee_per_contract_cents={fee!r}. "
            "Set to 7 (Kalshi charges 7c/contract)."
        )

    # â”€â”€ Check 3: Daily loss limit is real â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    dll = config.get("daily_loss_limit_dollars", 999999)
    if dll > 500:
        issues.append(
            f"DAILY LOSS LIMIT TOO HIGH — currently ${dll}. "
            "Set to a realistic value (e.g. $50)."
        )

    # â”€â”€ Check 4: mode gate â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    mode      = config.get("mode", "paper")
    is_live   = mode == "live"   # demo uses simulated funds — only block for real-money live
    override  = bool(config.get("preflight_override", False))

    if is_live and issues:
        print("=" * W)
        print("LIVE TRADING BLOCKED — PRE-FLIGHT CHECK FAILED")
        print("=" * W)
        for issue in issues:
            print(f"  [FAIL] {issue}")
        print()
        print("Switch to paper mode or resolve these issues before trading live.")
        print("To override (NOT RECOMMENDED): set preflight_override: true in config.json")
        print("=" * W)
        await send_telegram(
            "<b>LIVE TRADING BLOCKED — pre-flight failed</b>\n"
            + "\n".join(f"- {i}" for i in issues)
            + "\nResolve all issues before retrying live mode."
        )
        if not override:
            sys.exit(2)
        else:
            log.warning("PRE-FLIGHT OVERRIDE ACTIVE — proceeding into live mode despite failures. "
                        "This is NOT recommended.")
            print()
            print("  *** OVERRIDE ACTIVE — LIVE MODE STARTING ANYWAY ***")
            print("  *** THIS IS NOT RECOMMENDED. YOU WERE WARNED.   ***")
            print("=" * W)

    elif issues:
        # Paper mode — warn but continue; the bot must run to collect validation data.
        print("=" * W)
        print("PRE-FLIGHT WARNINGS (paper mode — not blocking)")
        print("=" * W)
        for issue in issues:
            print(f"  [WARN] {issue}")
        print("=" * W)

    else:
        log.info("=" * W)
        log.info(f"  Pre-flight: PASS — {mode.upper()} mode.  "
                 f"fee={fee}c  dll=${dll}  "
                 f"reversal={'ON' if config.get('enable_reversal_signal') else 'OFF'}")
        log.info("=" * W)


async def main() -> None:
    """Bootstrap: load credentials, init DB, start BTC feed, run main loop."""
    _init_config()
    load_credentials(mode=read_config().get("mode", "paper"))
    init_db()
    test_db_write()

    # Clean up zombie "pending" trades from prior crashed sessions.
    # Any trade still pending after 30+ minutes never settled — mark it as expired.
    try:
        conn = sqlite3.connect(bot_state._DB_FILE)
        cleaned = conn.execute(
            """UPDATE trades
               SET outcome         = 'expired_untracked',
                   exit_price_cents = 0,
                   pnl_dollars      = -(COALESCE(entry_price_cents,0) * COALESCE(contracts,1) / 100.0),
                   fill_confirmed   = 0
               WHERE outcome IN ('pending', '', NULL)
                 AND ts < datetime('now', '-30 minutes')"""
        ).rowcount
        conn.commit()
        conn.close()
        if cleaned:
            log.warning(f"Startup cleanup: marked {cleaned} zombie pending trade(s) as expired_untracked")
    except Exception as _e:
        log.warning(f"Startup zombie-trade cleanup failed (non-fatal): {_e}")


    # Verify Kalshi credentials and log account balance before doing anything.
    # Skipped in paper mode — no real credentials are loaded there.
    if read_config().get("mode", "paper") != "paper":
        async with aiohttp.ClientSession() as verify_session:
            await verify_kalshi_connection(verify_session)

    # Start Coinbase price feed for all assets
    _startup_config = read_config()
    _enabled = _startup_config.get("enabled_assets", ["ETH", "SOL", "XRP"])
    # Always subscribe BTC regardless of enabled_assets — other strategies use
    # btc_prices_60m for correlation signals and the deque must stay populated.
    _feed_assets = list(dict.fromkeys(["BTC"] + _enabled))
    from asset_manager import seed_price_history
    await seed_price_history(_feed_assets)
    asyncio.create_task(coinbase_price_task(_feed_assets))

    # OBI monitor (Coinbase Exchange level2 WebSocket)
    bot_state._obi_monitor = OBIMonitor(["BTC", "ETH", "SOL", "XRP", "DOGE"])
    asyncio.create_task(bot_state._obi_monitor.run())
    log.info("OBI monitor started for BTC, ETH, SOL, XRP, DOGE")

    # BTC/ETH funding dispersion monitors (Hyperliquid + Binance)
    _src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
    import sys as _sys_fm
    if _src_path not in _sys_fm.path:
        _sys_fm.path.insert(0, _src_path)
    from strategies.original.signals.funding_dispersion import FundingDispersionMonitor as _FDM
    bot_state._funding_monitor_btc = _FDM("BTC")
    bot_state._funding_monitor_eth = _FDM("ETH")

    async def _funding_refresh_loop() -> None:
        while True:
            try:
                await bot_state._funding_monitor_btc.refresh()
                await bot_state._funding_monitor_eth.refresh()
            except Exception as _fe:
                log.warning(f"funding refresh (BTC/ETH) error: {_fe}")
            await asyncio.sleep(60)

    asyncio.create_task(_funding_refresh_loop())
    log.info("BTC/ETH funding monitors started")

    # Wait for the first price from any enabled asset (timeout after 120s so Railway doesn't hang)
    _first_asset = _enabled[0] if _enabled else "ETH"
    log.info(f"Waiting for price feeds ({_enabled})...")
    waited = 0
    while _am_get_price(_first_asset) is None and waited < 120:
        await asyncio.sleep(1)
        waited += 1
        if waited % 30 == 0:
            log.warning(f"Still waiting for {_first_asset} price feed ({waited}s elapsed)...")
    _first_price = _am_get_price(_first_asset)
    if _first_price is None:
        log.warning("Price feed not available after 120s — continuing anyway; prices will populate shortly.")
    else:
        log.info(f"Price feed ready after {waited}s. {_first_asset}: ${_first_price:,.2f}")
    _startup_cfg = read_config()
    _btc_display = f"${get_btc_price():,.2f}" if get_btc_price() is not None else f"{_first_asset}: ${_first_price:,.2f}" if _first_price else "price N/A"
    await send_telegram(f"<b>Printer bot started</b>\n{_btc_display}\nMode: {_startup_cfg.get('mode','?').upper()}  |  Bot enabled: {_startup_cfg.get('bot_enabled', False)}")

    # Pre-flight check runs once before trading begins.
    # LIVE mode with unresolved issues → sys.exit(1). Paper mode → warn and continue.
    await run_preflight_checks(_startup_cfg)

    await main_loop()


if __name__ == "__main__":
    asyncio.run(main())


