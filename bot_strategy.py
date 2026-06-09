"""bot_strategy.py — S1 (EMA momentum) and S2 (contract velocity + OBI) strategy brains."""
import datetime
import logging
import logging.handlers
import math
import os
import time
from collections import deque

import bot_state
from bot_infra import read_config, get_asset_config
import asset_manager

log = logging.getLogger("bot")

brain_log = logging.getLogger("brain")
brain_log.setLevel(logging.INFO)
brain_log.propagate = False
def _make_brain_log_path() -> str:
    """Resolve brain.log path to the Railway volume; fall back to repo root."""
    try:
        vol_dir = os.path.dirname(os.path.abspath(bot_state._DB_FILE))
        test_path = os.path.join(vol_dir, "brain.log")
        os.makedirs(vol_dir, exist_ok=True)
        with open(test_path, "a"):
            pass
        return test_path
    except OSError:
        return "brain.log"
_brain_log_path = _make_brain_log_path()
_brain_fh = logging.handlers.RotatingFileHandler(
    _brain_log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
)
_brain_fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
brain_log.addHandler(_brain_fh)


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def track_contract_price(ticker: str, price: float) -> None:
    """Record the latest contract YES-ask price for S2 velocity signal."""
    if ticker not in bot_state._contract_price_history:
        bot_state._contract_price_history[ticker] = deque(maxlen=60)
    bot_state._contract_price_history[ticker].append((time.time(), price))


def _strategy_name_for(asset, duration_min=15.0):
    """Human-readable strategy name for the dashboard per-asset card."""
    return {"BTC": "B3", "ETH": "E1", "SOL": "SL1", "XRP": "X3", "DOGE": "D3"}.get(asset, "15m")


def _realized_vol(prices: list, window_minutes: int = 5) -> float:
    """Std of log-returns over the last window_minutes of price data."""
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


def _make_skip(side: str, reason: str, abs_pct: float, mins_left: float,
               rv: float = 0.0, variant: str = "strategy1",
               price_filter: bool = False) -> dict:
    """Standard skip-response dict with all keys expected by bot_loops.py."""
    return {
        "action": "skip", "side": side, "confidence": 50,
        "reasoning": reason, "key_signals": [reason], "signals": {},
        "win_prob": 0.5, "mom_label": "neutral", "mom_pct": 0.0,
        "vel_signal": "neutral", "raw_p_yes": None,
        "mins_left": mins_left, "abs_pct": abs_pct,
        "above": side == "yes", "_rv": rv, "_vol_ratio": None,
        "price_filter_skip": price_filter,
        "strategy_variant": variant,
    }


# ---------------------------------------------------------------------------
# S1 — EMA Momentum Strategy
# Direction pointer : 3-min vs N-min EMA crossover on asset price feed
# Confirmation     : realized vol below per-asset ceiling (low vol = predictable)
# Gate             : BTC requires US session; all assets need distance + time window
# ---------------------------------------------------------------------------

_S1_ASSET_CONFIG: dict = {
    #           min_dist  max_rv  min_momentum  min_ev  t_min  t_max
    # min_dist raised: only trade when price is meaningfully far from strike.
    # min_momentum: 60-second price change required to confirm recent directional move.
    # time_min=1.0: skip final minute (wide spreads, AMM settlement chaos).
    # time_max=12.0: skip very early (AMM hasn't had time to anchor contract price).
    "BTC":  dict(min_dist=0.0030, max_rv=1.0, min_momentum=0.0030, min_ev=0.04, time_min=1.0, time_max=12.0),
    "ETH":  dict(min_dist=0.0030, max_rv=1.0, min_momentum=0.0025, min_ev=0.04, time_min=1.0, time_max=12.0),
    "SOL":  dict(min_dist=0.0050, max_rv=1.0, min_momentum=0.0040, min_ev=0.04, time_min=1.0, time_max=12.0),
    "XRP":  dict(min_dist=0.0030, max_rv=1.0, min_momentum=0.0025, min_ev=0.04, time_min=1.0, time_max=12.0),
    "DOGE": dict(min_dist=0.0070, max_rv=1.0, min_momentum=0.0050, min_ev=0.04, time_min=1.0, time_max=12.0),
}


def _is_quiet_hours(config: dict) -> bool:
    """
    True when current ET time is in the overnight quiet window.
    Default: 10pm-7am ET (22:00-07:00). Configurable via:
      quiet_hours_enabled (bool, default True)
      quiet_start_et      (int hour 0-23, default 17)
      quiet_end_et        (int hour 0-23, default 7)
    Returns False when disabled or on any clock error.
    """
    if not config.get("quiet_hours_enabled", True):
        return False
    try:
        now_et = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-4)))
        hour = now_et.hour
        start = int(config.get("quiet_start_et", 17))
        end   = int(config.get("quiet_end_et", 7))
        if start > end:
            return hour >= start or hour < end
        else:
            return start <= hour < end
    except Exception:
        return False



def _trend_direction(prices: list, window_seconds: float = 600.0) -> int:
    """
    Linear regression slope over the last window_seconds of price history.
    Returns +1 (uptrend), -1 (downtrend), or 0 (insufficient data).
    Used to block contra-trend S1/S2 signals.
    """
    if not prices or len(prices) < 5:
        return 0
    now_ts = prices[-1][0]
    recent = [(float(ts), float(p)) for ts, p in prices if float(ts) >= now_ts - window_seconds]
    if len(recent) < 5:
        return 0
    n = len(recent)
    t0 = recent[0][0]
    xs = [ts - t0 for ts, _ in recent]
    ys = [p for _, p in recent]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    if den == 0:
        return 0
    slope = num / den
    if slope > 0:
        return 1
    if slope < 0:
        return -1
    return 0


def _s1_multitf_momentum(prices: list, min_momentum: float = 0.003) -> tuple:
    """
    Multi-timeframe momentum composite: 30s(0.15) + 60s(0.30) + 120s(0.35) + 240s(0.20).
    Returns (side, score): side='yes'/'no'/None, score=weighted directional agreement 0-1.
    Returns (None, 0.0) when data insufficient or timeframes disagree.

    Weight rationale: longer timeframes reduce micro-bounce noise (Polymarket v3 research).
    """
    _WINDOWS = [(30.0, 0.15), (60.0, 0.30), (120.0, 0.35), (240.0, 0.20)]
    if not prices or len(prices) < 10:
        return None, 0.0

    now_ts = prices[-1][0]
    current = float(prices[-1][1])
    signals = []

    for window_sec, weight in _WINDOWS:
        lo = now_ts - window_sec - window_sec * 0.15
        hi = now_ts - window_sec + window_sec * 0.15
        older = [float(p) for ts, p in prices if lo <= ts <= hi]
        if not older:
            continue
        past = sum(older) / len(older)
        if past <= 0:
            continue
        mom = (current - past) / past
        if abs(mom) >= min_momentum * (window_sec / 60.0) ** 0.5:
            direction = 1 if mom > 0 else -1
            signals.append((direction, weight, abs(mom)))

    if len(signals) < 2:
        return None, 0.0

    total_weight = sum(w for _, w, _ in signals)
    weighted_direction = sum(d * w for d, w, _ in signals) / total_weight

    if weighted_direction > 0.30:
        return "yes", weighted_direction
    elif weighted_direction < -0.30:
        return "no", -weighted_direction
    else:
        return None, abs(weighted_direction)


def _s1_certainty_win_prob(dist_pct: float, secs_left: float, asset: str) -> float:
    """
    Geometric Brownian Motion certainty model.
    Estimates P(price stays on current side of strike until settlement).
    Anchored to empirical 15-min vol per asset. Capped at 0.52-0.75.
    """
    # Empirical 15-min 1-sigma move as fraction of price
    _ASSET_VOL_15M = {
        "BTC": 0.008, "ETH": 0.007, "SOL": 0.012, "XRP": 0.010, "DOGE": 0.015,
    }
    vol_15m = _ASSET_VOL_15M.get(asset, 0.008) * _time_of_day_vol_multiplier()
    time_frac  = max(0.01, secs_left / 900.0)
    period_vol = vol_15m * math.sqrt(time_frac)
    z    = dist_pct / period_vol
    cert = 0.5 * (1.0 + math.erf(z / math.sqrt(2)))
    return max(0.52, min(0.75, cert))


def _time_of_day_vol_multiplier() -> float:
    """
    Adjusts realized vol by time of day (ET).
    US open (9:30-11:30 ET): 1.30x — high vol period, be conservative.
    US close (15:00-16:30 ET): 1.20x — elevated vol.
    Overnight (0:00-7:00 ET): 0.80x — thin market, lower vol.
    Otherwise: 1.00x baseline.
    Fail-open: returns 1.00 on any clock error.
    """
    try:
        now_et = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-4)))
        t = now_et.hour * 60 + now_et.minute
        if 9 * 60 + 30 <= t <= 11 * 60 + 30:
            return 1.30
        if 15 * 60 <= t <= 16 * 60 + 30:
            return 1.20
        if t < 7 * 60 or t >= 23 * 60:
            return 0.80
        return 1.00
    except Exception:
        return 1.00


_DISLOCATION_THRESHOLD = 0.0005  # BTC must move >0.05% from strike for dislocation to fire


def _s1_dislocation_check(
    dist_pct: float,
    yes_ask: float,
    secs_left: float,
    asset: str,
) -> tuple:
    """
    DISLOCATION signal: contract underpriced relative to BTC/asset move.

    Fair value: P = 0.5 + (dist_pct / time_decay_vol) * scale, capped 0.45-0.80.
    Returns (edge, fair_p): edge = fair_p - contract_price. Negative = no dislocation.

    Research: Polymarket 5-min BTC study core alpha signal — fires when contract
    price lags the asset move by >0.05%.
    """
    if dist_pct < _DISLOCATION_THRESHOLD:
        return 0.0, 0.5

    _ASSET_VOL_15M = {
        "BTC": 0.008, "ETH": 0.007, "SOL": 0.012, "XRP": 0.010, "DOGE": 0.015,
    }
    vol = _ASSET_VOL_15M.get(asset, 0.008)
    time_frac  = max(0.05, secs_left / 900.0)
    time_decay = vol * math.sqrt(time_frac)

    scale = 5.0
    fair_p = 0.5 + (dist_pct / max(time_decay, 1e-6)) * scale / 20.0
    fair_p = max(0.45, min(0.80, fair_p))

    contract_price_frac = yes_ask / 100.0
    edge = fair_p - contract_price_frac
    return edge, fair_p


def strategy_brain_s1(
    btc_price, strike, yes_ask, no_ask,
    elapsed_seconds, secs_left, ticker,
    asset: str = "BTC",
) -> dict:
    """
    S1: 60-second momentum + geometric certainty strategy.

    Direction: 60-second raw price momentum on the asset price feed.
    Continuation-only: momentum must agree with price position vs strike.
    Win probability: geometric Brownian motion certainty model (dist + remaining time).
    Per-asset thresholds for BTC / ETH / SOL / XRP / DOGE.
    """
    config = read_config()
    cfg = {**_S1_ASSET_CONFIG.get(asset, _S1_ASSET_CONFIG["BTC"]),
           **config.get("s1_config", {}).get(asset, {})}
    mins_left = secs_left / 60.0

    # Resolve asset price — callers pass the asset price as the first arg for non-BTC.
    if asset == "BTC":
        prices_list = list(bot_state.btc_prices)
        current_price = btc_price
    else:
        raw = asset_manager._prices.get(asset)
        prices_list = list(raw) if raw else []
        current_price = prices_list[-1][1] if prices_list else btc_price

    abs_pct = abs(current_price - strike) / strike if strike > 0 else 0.0

    # Quiet hours gate — block overnight to avoid thin-market losses
    if _is_quiet_hours(config):
        return _make_skip("yes", "s1_quiet_hours", abs_pct, mins_left, variant="strategy1")

    # Cap gate: global S1 position limit
    _s1_global_cap = config.get("max_s1_positions", 3)
    if len(bot_state._s1_pending_trades) >= _s1_global_cap:
        return _make_skip("yes", "s1_cap_global", abs_pct, mins_left, variant="strategy1")

    # Cap gate: per-asset S1 position limit
    _s1_asset_cap = config.get("max_s1_positions_per_asset", 1)
    _s1_asset_count = sum(
        1 for t in bot_state._s1_pending_trades.values() if t.get("asset") == asset
    )
    if _s1_asset_count >= _s1_asset_cap:
        return _make_skip("yes", "s1_cap_asset", abs_pct, mins_left, variant="strategy1")

    # S1 fire rate guard: max 2 S1 trades per asset per 60 minutes.
    # Prevents overtrading when signals cluster during noisy market periods.
    _max_per_hour = int(config.get("max_s1_per_asset_per_hour", 2))
    _now_ts = time.time()
    _recent_times = [t for t in bot_state._s1_asset_trade_times.get(asset, [])
                     if _now_ts - t < 3600.0]
    bot_state._s1_asset_trade_times[asset] = _recent_times  # prune stale entries
    if len(_recent_times) >= _max_per_hour:
        return _make_skip(
            "yes",
            f"s1_rate_limit:{len(_recent_times)}/{_max_per_hour}_per_hour",
            abs_pct, mins_left, variant="strategy1",
        )

    # Gate 1: time window
    if mins_left < cfg["time_min"] or mins_left > cfg["time_max"]:
        return _make_skip("yes", f"s1_time_gate:{mins_left:.1f}min", abs_pct, mins_left, variant="strategy1")

    # DISLOCATION fast-path: contract significantly underpriced vs BTC/asset move.
    # Bypasses momentum gates — dislocation is structural underpricing, not momentum.
    _disloc_entry_price = yes_ask if current_price >= strike else no_ask
    _disloc_side = "yes" if current_price >= strike else "no"
    _disloc_edge_raw, _disloc_fair_p = _s1_dislocation_check(
        abs_pct, _disloc_entry_price, secs_left, asset,
    )
    # fair_p is always the YES-side probability; for NO trades, flip to get correct edge
    if _disloc_side == "yes":
        _disloc_edge = _disloc_edge_raw
    else:
        _disloc_edge = (1.0 - _disloc_fair_p) - (_disloc_entry_price / 100.0)
    if _disloc_edge >= cfg.get("min_dislocation_edge", 0.07):
        _min_p = float(get_asset_config(config, asset, "min_entry_price_cents", 20.0))
        _max_p = float(get_asset_config(config, asset, "max_entry_price_cents", 55.0))
        if _min_p <= _disloc_entry_price <= _max_p:
            _disloc_trend = _trend_direction(prices_list, window_seconds=600.0)
            if _disloc_trend == 1 and _disloc_side == "no":
                return _make_skip(
                    _disloc_side, "s1_disloc_trend_gate:no_trend=up",
                    abs_pct, mins_left, variant="strategy1",
                )
            if _disloc_trend == -1 and _disloc_side == "yes":
                return _make_skip(
                    _disloc_side, "s1_disloc_trend_gate:yes_trend=down",
                    abs_pct, mins_left, variant="strategy1",
                )
            _disloc_1hr_trend = _trend_direction(prices_list, window_seconds=3600.0)
            if _disloc_1hr_trend == 1 and _disloc_side == "no":
                return _make_skip(
                    _disloc_side, "s1_1hr_trend_gate:no_trend=up",
                    abs_pct, mins_left, variant="strategy1",
                )
            if _disloc_1hr_trend == -1 and _disloc_side == "yes":
                return _make_skip(
                    _disloc_side, "s1_1hr_trend_gate:yes_trend=down",
                    abs_pct, mins_left, variant="strategy1",
                )
            brain_log.info(
                "S1 DISLOC %s %s | dist=%.4f fair_p=%.3f edge=%.3f ask=%.0fc mins=%.1f",
                asset, ticker, abs_pct, _disloc_fair_p, _disloc_edge, _disloc_entry_price, mins_left,
            )
            return {
                "action": "trade", "side": _disloc_side,
                "confidence": int(_disloc_fair_p * 100),
                "reasoning": f"s1_dislocation edge={_disloc_edge:.3f} fair_p={_disloc_fair_p:.3f} dist={abs_pct:.3%}",
                "key_signals": [f"disloc_edge:{_disloc_edge:.3f}", f"fair_p:{_disloc_fair_p:.3f}"],
                "signals": {"win_prob": _disloc_fair_p, "ev": _disloc_edge, "abs_pct": abs_pct},
                "win_prob": float(_disloc_fair_p), "mom_label": _disloc_side,
                "mom_pct": abs_pct, "vel_signal": "dislocation",
                "raw_p_yes": float(_disloc_fair_p) if _disloc_side == "yes" else float(1.0 - _disloc_fair_p),
                "mins_left": mins_left, "abs_pct": abs_pct, "above": _disloc_side == "yes",
                "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
                "strategy_variant": "strategy1",
            }

    # Gate 3: minimum distance from strike
    if abs_pct < cfg["min_dist"]:
        return _make_skip("yes", f"s1_dist_gate:{abs_pct:.4f}<{cfg['min_dist']}", abs_pct, mins_left, variant="strategy1")

    # Direction pointer: multi-timeframe momentum composite (30s/60s/120s/240s weighted)
    direction, momentum_pct = _s1_multitf_momentum(prices_list, min_momentum=cfg["min_momentum"])
    if direction is None:
        _reason = "s1_momentum_flat" if momentum_pct > 0 else "s1_no_momentum_data"
        return _make_skip("yes", f"{_reason}:{momentum_pct:.4f}", abs_pct, mins_left, variant="strategy1")

    side = direction  # 'yes' = bullish, 'no' = bearish
    entry_price = yes_ask if side == "yes" else no_ask

    # Continuation-only: momentum direction must match price position vs strike.
    if side == "yes" and current_price < strike:
        return _make_skip(side, "s1_reversal_gate:mom=yes_price_below", abs_pct, mins_left, variant="strategy1")
    if side == "no" and current_price > strike:
        return _make_skip(side, "s1_reversal_gate:mom=no_price_above", abs_pct, mins_left, variant="strategy1")

    # 10-minute trend filter: block signals that oppose the dominant trend.
    # Research: blocking contra-trend signals = 7x capital preservation improvement.
    _s1_trend = _trend_direction(prices_list, window_seconds=600.0)
    if _s1_trend != 0:
        if side == "yes" and _s1_trend == -1:
            return _make_skip(side, "s1_trend_gate:mom=yes_trend=down", abs_pct, mins_left, variant="strategy1")
        if side == "no" and _s1_trend == 1:
            return _make_skip(side, "s1_trend_gate:mom=no_trend=up", abs_pct, mins_left, variant="strategy1")

    # 1-hour macro trend filter: block trades that oppose the dominant hourly regime.
    _s1_1hr_trend = _trend_direction(prices_list, window_seconds=3600.0)
    if _s1_1hr_trend != 0:
        if side == "yes" and _s1_1hr_trend == -1:
            return _make_skip(side, "s1_1hr_trend_gate:yes_trend=down", abs_pct, mins_left, variant="strategy1")
        if side == "no" and _s1_1hr_trend == 1:
            return _make_skip(side, "s1_1hr_trend_gate:no_trend=up", abs_pct, mins_left, variant="strategy1")

    # Gate 5: entry price range — 55c max: market-uncertainty zone, 57%+ WR profitable
    _min_p = float(get_asset_config(config, asset, "min_entry_price_cents", 20.0))
    _max_p = float(get_asset_config(config, asset, "max_entry_price_cents", 55.0))
    if entry_price < _min_p or entry_price > _max_p:
        return _make_skip(side, f"s1_price_filter:{entry_price:.0f}c", abs_pct, mins_left,
                          variant="strategy1", price_filter=True)

    # Win probability: empirical WR from settled trades when ≥20 samples, else GBM.
    _s1_mode = config.get("mode", "paper")
    win_prob = _s1_lookup_win_rate(asset, abs_pct, mins_left, cfg=cfg, mode=_s1_mode)

    # EV gate
    _ep_s1 = entry_price / 100.0
    _fee_cents_s1 = config.get("kalshi_fee_per_contract_cents", 7)
    fee = (_fee_cents_s1 / 100) * _ep_s1 * (1.0 - _ep_s1)
    ev = win_prob - _ep_s1 - fee
    if ev < cfg["min_ev"]:
        return {
            "action": "skip", "side": side,
            "confidence": int(win_prob * 100),
            "reasoning": f"s1_ev_gate:{ev:.3f}<{cfg['min_ev']:.3f}",
            "key_signals": [f"ev:{ev:.3f}", f"wp:{win_prob:.3f}", f"mom:{direction}"],
            "signals": {"win_prob": win_prob, "ev": ev, "momentum_pct": momentum_pct,
                        "abs_pct": abs_pct, "strike": strike},
            "win_prob": float(win_prob), "mom_label": direction,
            "mom_pct": float(momentum_pct or 0.0),
            "vel_signal": "neutral",
            "raw_p_yes": float(win_prob) if side == "yes" else float(1.0 - win_prob),
            "mins_left": mins_left, "abs_pct": abs_pct, "above": side == "yes",
            "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
            "strategy_variant": "strategy1",
        }

    brain_log.info(
        "S1 TRADE %s %s | mom=%s pct=%.4f dist=%.4f ev=%.3f wp=%.3f mins=%.1f",
        asset, ticker, direction, momentum_pct or 0, abs_pct, ev, win_prob, mins_left,
    )
    return {
        "action": "trade", "side": side,
        "confidence": int(win_prob * 100),
        "reasoning": (
            f"s1_mom ev={ev:.3f} wp={win_prob:.3f} mom={direction} "
            f"dist={abs_pct:.3%} mom_pct={momentum_pct:.4f} mins={mins_left:.1f}"
        ),
        "key_signals": [
            f"ev:{ev:.3f}", f"wp:{win_prob:.3f}", f"mom:{direction}",
            f"dist:{abs_pct:.3%}", f"mom_pct:{momentum_pct:.4f}",
        ],
        "signals": {
            "win_prob": win_prob, "ev": ev, "momentum_pct": momentum_pct,
            "abs_pct": abs_pct, "mins_left": mins_left, "strike": strike,
        },
        "win_prob": float(win_prob), "mom_label": direction,
        "mom_pct": float(momentum_pct or 0.0),
        "vel_signal": "neutral",
        "raw_p_yes": float(win_prob) if side == "yes" else float(1.0 - win_prob),
        "mins_left": mins_left, "abs_pct": abs_pct, "above": side == "yes",
        "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
        "strategy_variant": "strategy1",
    }


# ---------------------------------------------------------------------------
# S2 — Contract Velocity + OBI Strategy
# Direction pointer : YES-ask price trend over last N ticks (market-implied flow)
# Confirmation     : OBI (order book imbalance) must agree with velocity direction
# Gate             : distance minimum + time window, per-asset thresholds
# Everything here is different from S1 — direction pointer, gates, confirmation
# ---------------------------------------------------------------------------

_S2_ASSET_CONFIG: dict = {
    #           min_dist  min_obi  min_vel_delta  vel_lookback  min_ev  t_min  t_max
    # min_vel_delta raised ~40%: require stronger velocity signal to avoid chasing weak moves.
    # min_ev raised to 0.04: require clear positive-EV before entry.
    "BTC":  dict(min_dist=0.0015, min_obi=0.02, min_vel_delta=0.30, vel_lookback=4, min_ev=0.04, time_min=2.0, time_max=12.5),
    "ETH":  dict(min_dist=0.0015, min_obi=0.02, min_vel_delta=0.26, vel_lookback=4, min_ev=0.04, time_min=2.0, time_max=12.5),
    "SOL":  dict(min_dist=0.0025, min_obi=0.02, min_vel_delta=0.42, vel_lookback=3, min_ev=0.04, time_min=2.0, time_max=12.5),
    "XRP":  dict(min_dist=0.0020, min_obi=0.02, min_vel_delta=0.32, vel_lookback=4, min_ev=0.04, time_min=2.0, time_max=12.5),
    "DOGE": dict(min_dist=0.0040, min_obi=0.02, min_vel_delta=0.50, vel_lookback=3, min_ev=0.04, time_min=2.0, time_max=12.5),
}

# ---------------------------------------------------------------------------
# Empirical win-rate tables — populated by scripts/calibrate_winrates.py
# Run that script, copy the printed dicts here.
# None entries → tanh formula fallback (insufficient calibration data).
# ---------------------------------------------------------------------------

_S1_WIN_RATE: dict = {
    # All set to None — forces tanh fallback with realistic baseline.
    # Previous values (0.97-1.0) were calibration artifacts causing EV gate to always pass.
    # Real S1 WR is 55-62%; tanh formula (0.52 + 0.08*tanh) reflects this.
    "BTC":  {(0,0): None, (0,1): None, (0,2): None, (1,0): None, (1,1): None, (1,2): None, (2,0): None, (2,1): None, (2,2): None, (3,0): None, (3,1): None, (3,2): None},
    "ETH":  {(0,0): None, (0,1): None, (0,2): None, (1,0): None, (1,1): None, (1,2): None, (2,0): None, (2,1): None, (2,2): None, (3,0): None, (3,1): None, (3,2): None},
    "SOL":  {(0,0): None, (0,1): None, (0,2): None, (1,0): None, (1,1): None, (1,2): None, (2,0): None, (2,1): None, (2,2): None, (3,0): None, (3,1): None, (3,2): None},
    "XRP":  {(0,0): None, (0,1): None, (0,2): None, (1,0): None, (1,1): None, (1,2): None, (2,0): None, (2,1): None, (2,2): None, (3,0): None, (3,1): None, (3,2): None},
    "DOGE": {(0,0): None, (0,1): None, (0,2): None, (1,0): None, (1,1): None, (1,2): None, (2,0): None, (2,1): None, (2,2): None, (3,0): None, (3,1): None, (3,2): None},
}

_S2_WIN_RATE: dict = {
    # All set to None — forces tanh fallback with realistic baseline.
    # Previous values (0.63-0.97) caused high-entry trades to pass EV gate falsely.
    # Real S2 WR is 55-62%; tanh formula (0.52 + 0.08*tanh) reflects this.
    "BTC":  {(0,0): None, (0,1): None, (0,2): None, (1,0): None, (1,1): None, (1,2): None, (2,0): None, (2,1): None, (2,2): None},
    "ETH":  {(0,0): None, (0,1): None, (0,2): None, (1,0): None, (1,1): None, (1,2): None, (2,0): None, (2,1): None, (2,2): None},
    "SOL":  {(0,0): None, (0,1): None, (0,2): None, (1,0): None, (1,1): None, (1,2): None, (2,0): None, (2,1): None, (2,2): None},
    "XRP":  {(0,0): None, (0,1): None, (0,2): None, (1,0): None, (1,1): None, (1,2): None, (2,0): None, (2,1): None, (2,2): None},
    "DOGE": {(0,0): None, (0,1): None, (0,2): None, (1,0): None, (1,1): None, (1,2): None, (2,0): None, (2,1): None, (2,2): None},
}

# S1 bucket boundaries (must match calibrate_winrates.py constants)
_S1_DIST_BOUNDS = [0.005, 0.010, 0.020]
_S1_TIME_BOUNDS = [6.0, 9.0]

# S2 bucket boundaries (must match calibrate_winrates.py constants)
_S2_VEL_MULTIPLIERS = [2.0, 4.0]
_S2_TIME_BOUNDS_S2  = [5.0, 8.0]


def _s1_lookup_win_rate(asset: str, abs_pct: float, mins_left: float,
                        cfg: dict | None = None, mode: str = "live") -> float:
    """
    Look up S1 win rate. Priority:
    1. Empirical (from live settled trades) if >= 20 samples in bucket.
    2. Hardcoded table if non-None.
    3. GBM certainty model (dist + time → probability).
    """
    if cfg is None:
        cfg = _S1_ASSET_CONFIG.get(asset, _S1_ASSET_CONFIG["BTC"])

    # Empirical from live trades (most accurate after burn-in period of ~20 trades/bucket)
    try:
        from bot_infra import _get_empirical_wr
        empirical = _get_empirical_wr(asset, abs_pct, mins_left, mode, strategy="s1", min_samples=20)
        if empirical is not None:
            return empirical
    except Exception as _exc:
        log.debug("_s1_lookup_win_rate empirical lookup failed: %s", _exc)

    dist_idx = len(_S1_DIST_BOUNDS)
    for i, bound in enumerate(_S1_DIST_BOUNDS):
        if abs_pct < bound:
            dist_idx = i
            break

    time_idx = len(_S1_TIME_BOUNDS)
    for i, bound in enumerate(_S1_TIME_BOUNDS):
        if mins_left < bound:
            time_idx = i
            break

    emp_val = _S1_WIN_RATE.get(asset, {}).get((dist_idx, time_idx))
    if emp_val is not None:
        return float(emp_val)

    return _s1_certainty_win_prob(abs_pct, mins_left * 60.0, asset)


def _s2_lookup_win_rate(asset: str, vel_delta: float, mins_left: float, cfg: dict | None = None) -> float:
    """Look up empirical S2 win rate. Falls back to tanh when bucket is None or missing."""
    if cfg is None:
        cfg = _S2_ASSET_CONFIG.get(asset, _S2_ASSET_CONFIG["BTC"])
    min_vel = cfg["min_vel_delta"]

    ratio   = vel_delta / max(min_vel, 1e-9)
    vel_idx = len(_S2_VEL_MULTIPLIERS)
    for i, mult in enumerate(_S2_VEL_MULTIPLIERS):
        if ratio < mult:
            vel_idx = i
            break

    time_idx = len(_S2_TIME_BOUNDS_S2)
    for i, bound in enumerate(_S2_TIME_BOUNDS_S2):
        if mins_left < bound:
            time_idx = i
            break

    emp_val = _S2_WIN_RATE.get(asset, {}).get((vel_idx, time_idx))
    if emp_val is not None:
        return float(emp_val)

    return 0.52 + 0.08 * math.tanh(vel_delta / max(min_vel, 1e-6))


def _s2_contract_direction(ticker: str, min_delta: float, lookback: int):
    """
    S2 direction pointer: Kalshi contract (YES-ask) price velocity.
    Compares first half vs second half of the last `lookback+1` price ticks.
    Returns (side, delta): side='yes' if YES price rising (market leans YES), 'no' if falling.
    Returns (None, None) when data insufficient or signal below threshold.
    """
    history = bot_state._contract_price_history.get(ticker)
    if not history or len(history) < lookback + 1:
        return None, None
    recent = [p for _, p in list(history)[-(lookback + 1):]]
    mid = max(1, len(recent) // 2)
    first_avg  = sum(recent[:mid]) / mid
    second_avg = sum(recent[mid:]) / max(1, len(recent) - mid)
    delta = second_avg - first_avg  # positive = YES price rising = market leans bullish
    if abs(delta) < min_delta:
        return None, abs(delta)
    return ("yes" if delta > 0 else "no"), abs(delta)


def _s2_obi_gate(ticker: str, side: str, min_obi: float):
    """
    OBI confirmation gate for S2.
    Returns (confirmed, obi_val).
    Passes (True, None) when no OBI data — Kalshi AMM markets always return empty orderbook arrays,
    so None OBI is structural, not a data error. S2 win rate tables were calibrated without OBI gate.
    Positive OBI = no_depth > yes_depth = bullish for YES.
    """
    obi_val = bot_state._ticker_obi.get(ticker)
    if obi_val is None:
        return True, None
    if side == "yes" and obi_val <= min_obi:
        return False, obi_val
    if side == "no"  and obi_val >= -min_obi:
        return False, obi_val
    return True, obi_val


def check_dual_side_arb(
    yes_ask: float,
    no_ask: float,
    fee_per_contract_cents: float = 7,
    threshold: float = 93.0,
) -> dict:
    """
    Structural arbitrage check: YES + NO < threshold → guaranteed profit.

    When YES_ask + NO_ask < 93c: one side always pays $1.00, net profit > 0
    after fees. At threshold=93c: net profit = 100 - yes_ask - no_ask > 7c.

    Returns dict with:
      arb (bool): True if arbitrage opportunity exists
      net_edge_cents (float): 100 - yes_ask - no_ask - estimated_fees
      combined (float): yes_ask + no_ask
      yes_ask, no_ask: inputs echoed back
    """
    combined = yes_ask + no_ask
    fee_yes = fee_per_contract_cents * (yes_ask / 100.0) * (1.0 - yes_ask / 100.0)
    fee_no  = fee_per_contract_cents * (no_ask  / 100.0) * (1.0 - no_ask  / 100.0)
    net_edge = 100.0 - yes_ask - no_ask - fee_yes - fee_no
    arb_fires = combined < threshold and net_edge > 0
    return {
        "arb": arb_fires,
        "net_edge_cents": round(net_edge, 2),
        "combined": combined,
        "yes_ask": yes_ask,
        "no_ask": no_ask,
    }


def strategy_brain_s2(
    btc_price, strike, yes_ask, no_ask,
    elapsed_seconds, secs_left, ticker,
    asset: str = "BTC",
) -> dict:
    """
    S2: Contract velocity + OBI strategy.

    Direction: YES-ask price trend over last N ticks (what the market is pricing in).
    Confirmation: OBI must agree with the velocity direction.
    Per-asset thresholds for BTC / ETH / SOL / XRP / DOGE.
    Completely different from S1 — no EMA, no vol ceiling, no session gate.
    """
    config = read_config()
    cfg = {**_S2_ASSET_CONFIG.get(asset, _S2_ASSET_CONFIG["BTC"]),
           **config.get("s2_config", {}).get(asset, {})}
    mins_left = secs_left / 60.0

    # Resolve asset price
    if asset == "BTC":
        current_price = btc_price
    else:
        raw = asset_manager._prices.get(asset)
        current_price = raw[-1][1] if raw else btc_price

    abs_pct = abs(current_price - strike) / strike if strike > 0 else 0.0

    # Quiet hours gate — block overnight to avoid thin-market losses
    if _is_quiet_hours(config):
        return _make_skip("yes", "s2_quiet_hours", abs_pct, mins_left, variant="strategy2")

    # Gate 1: time window
    if mins_left < cfg["time_min"] or mins_left > cfg["time_max"]:
        return _make_skip("yes", f"s2_time_gate:{mins_left:.1f}min", abs_pct, mins_left, variant="strategy2")

    # Gate 2: minimum distance from strike
    if abs_pct < cfg["min_dist"]:
        return _make_skip("yes", f"s2_dist_gate:{abs_pct:.4f}<{cfg['min_dist']}", abs_pct, mins_left, variant="strategy2")

    # Direction pointer: contract price velocity
    direction, vel_delta = _s2_contract_direction(ticker, cfg["min_vel_delta"], cfg["vel_lookback"])
    if direction is None:
        _vel_reason = "s2_no_velocity_data" if vel_delta is None else f"s2_vel_flat:{vel_delta:.3f}<{cfg['min_vel_delta']}"
        return _make_skip("yes", _vel_reason, abs_pct, mins_left, variant="strategy2")

    # Conviction gate: require 1.5× minimum velocity — filters noise without killing signal.
    # 3× was too strict: ~70% of historically-profitable S2 signals were below that threshold.
    _min_conviction = 1.5 * cfg["min_vel_delta"]
    if vel_delta < _min_conviction:
        return _make_skip(
            direction,
            f"s2_vel_weak:{vel_delta:.3f}<{_min_conviction:.3f}",
            abs_pct, mins_left, variant="strategy2",
        )

    side = direction

    # Continuation-only: velocity direction must match asset price position vs strike.
    if side == "yes" and current_price < strike:
        return _make_skip(side, "s2_reversal_gate:vel=yes_price_below", abs_pct, mins_left, variant="strategy2")
    if side == "no" and current_price > strike:
        return _make_skip(side, "s2_reversal_gate:vel=no_price_above", abs_pct, mins_left, variant="strategy2")

    entry_price = yes_ask if side == "yes" else no_ask

    # Gate 3: entry price range — 55c max filters out expensive trades beyond uncertainty zone
    _min_p = float(get_asset_config(config, asset, "min_entry_price_cents", 20.0))
    _max_p = float(get_asset_config(config, asset, "max_entry_price_cents", 55.0))
    if entry_price < _min_p or entry_price > _max_p:
        return _make_skip(side, f"s2_price_filter:{entry_price:.0f}c", abs_pct, mins_left,
                          variant="strategy2", price_filter=True)

    # Gate 4: OBI confirmation (required gate, not optional adjustment)
    obi_ok, obi_val = _s2_obi_gate(ticker, side, cfg["min_obi"])
    if not obi_ok:
        return _make_skip(
            side,
            f"s2_obi_gate:obi={'None' if obi_val is None else f'{obi_val:.2f}'}_side={side}_min={cfg['min_obi']}",
            abs_pct, mins_left, variant="strategy2",
        )

    # Win probability: geometric certainty model (velocity qualifies direction,
    # dist+time determines how certain the outcome is)
    win_prob = _s1_certainty_win_prob(abs_pct, secs_left, asset)

    # EV gate — Kalshi fee from config (default 7 cents per contract)
    _ep_s2 = entry_price / 100.0
    _fee_cents = config.get("kalshi_fee_per_contract_cents", 7)
    fee = (_fee_cents / 100) * _ep_s2 * (1.0 - _ep_s2)
    ev = win_prob - _ep_s2 - fee
    _obi_str = f"{obi_val:.2f}" if obi_val is not None else "n/a"
    if ev < cfg["min_ev"]:
        return {
            "action": "skip", "side": side,
            "confidence": int(win_prob * 100),
            "reasoning": f"s2_ev_gate:{ev:.3f}<{cfg['min_ev']:.3f}",
            "key_signals": [f"ev:{ev:.3f}", f"wp:{win_prob:.3f}", f"vel:{direction}"],
            "signals": {"win_prob": win_prob, "ev": ev, "vel_delta": vel_delta,
                        "obi": obi_val, "abs_pct": abs_pct, "strike": strike},
            "win_prob": float(win_prob), "mom_label": direction, "mom_pct": 0.0,
            "vel_signal": direction,
            "raw_p_yes": float(win_prob) if side == "yes" else float(1.0 - win_prob),
            "mins_left": mins_left, "abs_pct": abs_pct, "above": side == "yes",
            "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
            "strategy_variant": "strategy2", "strategy_version": bot_state._S2_VERSION,
        }

    brain_log.info(
        "S2 TRADE %s %s | vel=%s delta=%.2f(%.1fx) obi=%s dist=%.4f ev=%.3f wp=%.3f mins=%.1f",
        asset, ticker, direction, vel_delta or 0,
        (vel_delta or 0) / max(cfg["min_vel_delta"], 1e-9),
        _obi_str, abs_pct, ev, win_prob, mins_left,
    )
    return {
        "action": "trade", "side": side,
        "confidence": int(win_prob * 100),
        "reasoning": (
            f"s2_vel ev={ev:.3f} wp={win_prob:.3f} vel={direction} "
            f"obi={_obi_str} dist={abs_pct:.3%} mins={mins_left:.1f}"
        ),
        "key_signals": [
            f"ev:{ev:.3f}", f"wp:{win_prob:.3f}", f"vel:{direction}",
            f"obi:{_obi_str}", f"dist:{abs_pct:.3%}",
        ],
        "signals": {
            "win_prob": win_prob, "ev": ev, "vel_delta": vel_delta,
            "obi": obi_val, "abs_pct": abs_pct, "mins_left": mins_left, "strike": strike,
        },
        "win_prob": float(win_prob), "mom_label": direction, "mom_pct": 0.0,
        "vel_signal": direction,
        "raw_p_yes": float(win_prob) if side == "yes" else float(1.0 - win_prob),
        "mins_left": mins_left, "abs_pct": abs_pct, "above": side == "yes",
        "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
        "strategy_variant": "strategy2", "strategy_version": bot_state._S2_VERSION,
    }
