"""bot_strategy.py — S1 (BV3 empirical) and S2 (D3 hybrid) strategy brains."""
import logging
import math
import os
import time
from collections import deque

import bot_state
from bot_infra import read_config, get_asset_config
import asset_manager

log = logging.getLogger("bot")

# Separate logger for Brain v3 decision records — writes to brain.log only
brain_log = logging.getLogger("brain")
brain_log.setLevel(logging.INFO)
brain_log.propagate = False
_brain_fh = logging.FileHandler("brain.log", encoding="utf-8")
_brain_fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
brain_log.addHandler(_brain_fh)

# ══════════════════════════════════════════════════════════════════════════════
#  Contract price velocity tracking
# ══════════════════════════════════════════════════════════════════════════════

def track_contract_price(ticker: str, price: float) -> None:
    """Record the latest contract ask price for velocity and lag analysis."""
    if ticker not in bot_state._contract_price_history:
        bot_state._contract_price_history[ticker] = deque(maxlen=60)
    bot_state._contract_price_history[ticker].append((time.time(), price))


# ══════════════════════════════════════════════════════════════════════════════
#  Printer Brain v3 — Empirically Calibrated from 4.5M rows of BTC 1-min data
# ══════════════════════════════════════════════════════════════════════════════

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
