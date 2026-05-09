"""bot_strategy.py — S1 (EMA momentum) and S2 (contract velocity + OBI) strategy brains."""
import datetime
import logging
import math
import time
from collections import deque

import bot_state
from bot_infra import read_config, get_asset_config
import asset_manager

log = logging.getLogger("bot")

brain_log = logging.getLogger("brain")
brain_log.setLevel(logging.INFO)
brain_log.propagate = False
_brain_fh = logging.FileHandler("brain.log", encoding="utf-8")
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
    return {"BTC": "B3", "ETH": "E1", "SOL": "S1", "XRP": "X3", "DOGE": "D3"}.get(asset, "15m")


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
    #           min_dist  max_rv  ema_short  ema_long  session  min_ev  t_min  t_max
    "BTC":  dict(min_dist=0.0025, max_rv=0.0080, ema_short=3, ema_long=10,
                 session_gate=True,  min_ev=0.08, time_min=3.0, time_max=12.0),
    "ETH":  dict(min_dist=0.0030, max_rv=0.0120, ema_short=3, ema_long=10,
                 session_gate=False, min_ev=0.09, time_min=3.0, time_max=12.0),
    "SOL":  dict(min_dist=0.0050, max_rv=0.0200, ema_short=3, ema_long=8,
                 session_gate=False, min_ev=0.10, time_min=3.0, time_max=10.0),
    "XRP":  dict(min_dist=0.0040, max_rv=0.0160, ema_short=3, ema_long=10,
                 session_gate=False, min_ev=0.09, time_min=3.0, time_max=12.0),
    "DOGE": dict(min_dist=0.0080, max_rv=0.0300, ema_short=2, ema_long=8,
                 session_gate=False, min_ev=0.12, time_min=3.0, time_max=10.0),
}


def _s1_is_us_session() -> bool:
    """True during US open (09:30-11:30 ET) or close (15:00-16:00 ET)."""
    try:
        # EDT = UTC-4; EST = UTC-5. Using UTC-4 year-round; up to 1h edge error acceptable.
        now_et = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-4)))
        t = now_et.hour * 60 + now_et.minute
        return (9 * 60 + 30 <= t <= 11 * 60 + 30) or (15 * 60 <= t <= 16 * 60)
    except Exception:
        return True  # fail open — never block trades on a clock error


def _s1_ema_direction(prices: list, short_min: float, long_min: float):
    """
    EMA crossover direction pointer.
    Returns (side, ratio): side='yes' (bullish) or 'no' (bearish), ratio=short/long EMA.
    Returns (None, None) when data is insufficient.
    """
    if not prices or len(prices) < 4:
        return None, None
    now = prices[-1][0]
    short_px = [float(p) for ts, p in prices if ts >= now - short_min * 60]
    long_px  = [float(p) for ts, p in prices if ts >= now - long_min  * 60]
    if len(short_px) < 2 or len(long_px) < 3:
        return None, None

    def _ema(vals: list) -> float:
        alpha = 2.0 / (len(vals) + 1)
        v = float(vals[0])
        for x in vals[1:]:
            v = alpha * float(x) + (1.0 - alpha) * v
        return v

    s_ema = _ema(short_px)
    l_ema = _ema(long_px)
    ratio = s_ema / l_ema if l_ema > 0 else 1.0
    return ("yes" if s_ema > l_ema else "no"), ratio


def strategy_brain_s1(
    btc_price, strike, yes_ask, no_ask,
    elapsed_seconds, secs_left, ticker,
    asset: str = "BTC",
) -> dict:
    """
    S1: EMA momentum strategy.

    Direction: 3-min vs N-min EMA crossover on the asset price feed.
    Confirmation: realized vol must be below per-asset ceiling.
    BTC additionally requires US market-open or market-close session.
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

    # Gate 1: session (BTC only)
    if cfg["session_gate"] and not _s1_is_us_session():
        return _make_skip("yes", "s1_session_gate", abs_pct, mins_left, variant="strategy1")

    # Gate 2: time window
    if mins_left < cfg["time_min"] or mins_left > cfg["time_max"]:
        return _make_skip("yes", f"s1_time_gate:{mins_left:.1f}min", abs_pct, mins_left, variant="strategy1")

    # Gate 3: minimum distance from strike
    if abs_pct < cfg["min_dist"]:
        return _make_skip("yes", f"s1_dist_gate:{abs_pct:.4f}<{cfg['min_dist']}", abs_pct, mins_left, variant="strategy1")

    # Gate 4: realized vol ceiling — low vol means more predictable directional move
    rv = _realized_vol(prices_list, window_minutes=5) if prices_list else 0.001
    if rv > cfg["max_rv"]:
        return _make_skip("yes", f"s1_rv_gate:{rv:.5f}>{cfg['max_rv']}", abs_pct, mins_left, rv=rv, variant="strategy1")

    # Direction pointer: EMA crossover
    direction, ema_ratio = _s1_ema_direction(prices_list, cfg["ema_short"], cfg["ema_long"])
    if direction is None:
        return _make_skip("yes", "s1_no_ema_data", abs_pct, mins_left, rv=rv, variant="strategy1")

    side = direction  # 'yes' = bullish, 'no' = bearish
    entry_price = yes_ask if side == "yes" else no_ask

    # Continuation-only: EMA direction must agree with price position relative to strike.
    # EMA bullish but price below strike = reversal bet → skip (win prob ~20%, not 70%).
    if side == "yes" and current_price < strike:
        return _make_skip(side, "s1_reversal_gate:ema=yes_price_below", abs_pct, mins_left, rv=rv, variant="strategy1")
    if side == "no" and current_price > strike:
        return _make_skip(side, "s1_reversal_gate:ema=no_price_above", abs_pct, mins_left, rv=rv, variant="strategy1")

    # Gate 5: entry price range (per-asset via get_asset_config)
    _min_p = float(get_asset_config(config, asset, "min_entry_price_cents", 20.0))
    _max_p = float(get_asset_config(config, asset, "max_entry_price_cents", 76.0))
    if entry_price < _min_p or entry_price > _max_p:
        return _make_skip(side, f"s1_price_filter:{entry_price:.0f}c", abs_pct, mins_left,
                          rv=rv, variant="strategy1", price_filter=True)

    # Win probability: empirical lookup (tanh fallback when bucket uncalibrated)
    # No additive adjustments — calibration already prices EMA + session selection in.
    base_p = _s1_lookup_win_rate(asset, abs_pct, mins_left, cfg)
    win_prob = min(0.99, base_p)

    # EV gate — Kalshi fee from config (default 7 cents per contract)
    _ep_s1 = entry_price / 100.0
    _fee_cents_s1 = config.get("kalshi_fee_per_contract_cents", 7)
    fee = (_fee_cents_s1 / 100) * _ep_s1 * (1.0 - _ep_s1)
    ev = win_prob - _ep_s1 - fee
    if ev < cfg["min_ev"]:
        return {
            "action": "skip", "side": side,
            "confidence": int(win_prob * 100),
            "reasoning": f"s1_ev_gate:{ev:.3f}<{cfg['min_ev']:.3f}",
            "key_signals": [f"ev:{ev:.3f}", f"wp:{win_prob:.3f}", f"ema:{direction}"],
            "signals": {"win_prob": win_prob, "ev": ev, "ema_ratio": ema_ratio,
                        "rv": rv, "abs_pct": abs_pct, "strike": strike},
            "win_prob": float(win_prob), "mom_label": direction,
            "mom_pct": float((ema_ratio or 1.0) - 1.0),
            "vel_signal": "neutral",
            "raw_p_yes": float(win_prob) if side == "yes" else float(1.0 - win_prob),
            "mins_left": mins_left, "abs_pct": abs_pct, "above": side == "yes",
            "_rv": rv, "_vol_ratio": None, "price_filter_skip": False,
            "strategy_variant": "strategy1",
        }

    brain_log.info(
        "S1 TRADE %s %s | ema=%s ratio=%.5f rv=%.5f dist=%.4f ev=%.3f wp=%.3f mins=%.1f",
        asset, ticker, direction, ema_ratio or 0, rv, abs_pct, ev, win_prob, mins_left,
    )
    return {
        "action": "trade", "side": side,
        "confidence": int(win_prob * 100),
        "reasoning": (
            f"s1_ema ev={ev:.3f} wp={win_prob:.3f} ema={direction} "
            f"dist={abs_pct:.3%} rv={rv:.5f} mins={mins_left:.1f}"
        ),
        "key_signals": [
            f"ev:{ev:.3f}", f"wp:{win_prob:.3f}", f"ema:{direction}",
            f"dist:{abs_pct:.3%}", f"rv:{rv:.5f}",
        ],
        "signals": {
            "win_prob": win_prob, "ev": ev, "ema_ratio": ema_ratio,
            "rv": rv, "abs_pct": abs_pct, "mins_left": mins_left, "strike": strike,
        },
        "win_prob": float(win_prob), "mom_label": direction,
        "mom_pct": float((ema_ratio or 1.0) - 1.0),
        "vel_signal": "neutral",
        "raw_p_yes": float(win_prob) if side == "yes" else float(1.0 - win_prob),
        "mins_left": mins_left, "abs_pct": abs_pct, "above": side == "yes",
        "_rv": rv, "_vol_ratio": None, "price_filter_skip": False,
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
    "BTC":  dict(min_dist=0.0035, min_obi=0.20, min_vel_delta=0.80, vel_lookback=4,
                 min_ev=0.09, time_min=2.0, time_max=13.0),
    "ETH":  dict(min_dist=0.0030, min_obi=0.15, min_vel_delta=0.70, vel_lookback=4,
                 min_ev=0.09, time_min=2.0, time_max=13.0),
    "SOL":  dict(min_dist=0.0060, min_obi=0.25, min_vel_delta=1.20, vel_lookback=3,
                 min_ev=0.11, time_min=2.0, time_max=11.0),
    "XRP":  dict(min_dist=0.0050, min_obi=0.20, min_vel_delta=0.90, vel_lookback=4,
                 min_ev=0.10, time_min=2.0, time_max=12.0),
    "DOGE": dict(min_dist=0.0100, min_obi=0.30, min_vel_delta=1.50, vel_lookback=3,
                 min_ev=0.13, time_min=2.0, time_max=10.0),
}

# ---------------------------------------------------------------------------
# Empirical win-rate tables — populated by scripts/calibrate_winrates.py
# Run that script, copy the printed dicts here.
# None entries → tanh formula fallback (insufficient calibration data).
# ---------------------------------------------------------------------------

_S1_WIN_RATE: dict = {
    "BTC":  {(0,0): 0.9848, (0,1): 0.9497, (0,2): 0.8915, (1,0): 0.991,  (1,1): 0.9837, (1,2): 0.9585, (2,0): 1.0,    (2,1): None,   (2,2): None,   (3,0): None, (3,1): None, (3,2): None},
    "ETH":  {(0,0): 0.9722, (0,1): 0.9454, (0,2): 0.8936, (1,0): 0.9896, (1,1): 0.9715, (1,2): 0.9307, (2,0): 1.0,    (2,1): 0.9839, (2,2): None,   (3,0): None, (3,1): None, (3,2): None},
    "SOL":  {(0,0): None,   (0,1): None,   (0,2): None,   (1,0): 0.9874, (1,1): 0.9674, (1,2): 0.9269, (2,0): 0.9922, (2,1): 0.98,   (2,2): 0.9437, (3,0): None, (3,1): None, (3,2): None},
    "XRP":  {(0,0): 0.976,  (0,1): 0.9193, (0,2): 0.903,  (1,0): 0.9908, (1,1): 0.9794, (1,2): 0.9274, (2,0): 0.9945, (2,1): 1.0,    (2,2): None,   (3,0): None, (3,1): None, (3,2): None},
    "DOGE": {(0,0): None,   (0,1): None,   (0,2): None,   (1,0): 0.9937, (1,1): 0.9859, (1,2): 0.8974, (2,0): 1.0,    (2,1): 1.0,    (2,2): 0.9915, (3,0): None, (3,1): None, (3,2): None},
}

_S2_WIN_RATE: dict = {
    "BTC": {(0,0): 0.9331, (0,1): 0.8421, (0,2): 0.6447, (1,0): 0.9487, (1,1): 0.8952, (1,2): 0.7151, (2,0): 0.91, (2,1): 0.846, (2,2): 0.7672},
    "ETH": {(0,0): 0.9751, (0,1): 0.8293, (0,2): 0.6289, (1,0): 0.9749, (1,1): 0.8698, (1,2): 0.7425, (2,0): 0.9027, (2,1): 0.8488, (2,2): 0.768},
    "SOL": {(0,0): 0.9573, (0,1): 0.865, (0,2): 0.7252, (1,0): 0.9462, (1,1): 0.8504, (1,2): 0.7709, (2,0): 0.8883, (2,1): 0.8368, (2,2): 0.7674},
    "XRP": {(0,0): 0.9567, (0,1): 0.8095, (0,2): 0.7039, (1,0): 0.9382, (1,1): 0.8303, (1,2): 0.7221, (2,0): 0.903, (2,1): 0.8431, (2,2): 0.774},
    "DOGE": {(0,0): 0.9434, (0,1): 0.8924, (0,2): 0.7602, (1,0): 0.94, (1,1): 0.8724, (1,2): 0.7731, (2,0): 0.8893, (2,1): 0.8355, (2,2): 0.7849},
}

# S1 bucket boundaries (must match calibrate_winrates.py constants)
_S1_DIST_BOUNDS = [0.005, 0.010, 0.020]
_S1_TIME_BOUNDS = [6.0, 9.0]

# S2 bucket boundaries (must match calibrate_winrates.py constants)
_S2_VEL_MULTIPLIERS = [2.0, 4.0]
_S2_TIME_BOUNDS_S2  = [5.0, 8.0]


def _s1_lookup_win_rate(asset: str, abs_pct: float, mins_left: float, cfg: dict | None = None) -> float:
    """Look up empirical S1 win rate. Falls back to tanh when bucket is None or missing."""
    if cfg is None:
        cfg = _S1_ASSET_CONFIG.get(asset, _S1_ASSET_CONFIG["BTC"])
    min_dist = cfg["min_dist"]

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

    return 0.50 + 0.28 * math.tanh(abs_pct / max(min_dist, 1e-6))


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

    return 0.50 + 0.25 * math.tanh(vel_delta / max(min_vel, 1e-6))


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
        return None, None
    return ("yes" if delta > 0 else "no"), abs(delta)


def _s2_obi_gate(ticker: str, side: str, min_obi: float):
    """
    OBI confirmation gate for S2.
    Returns (confirmed, obi_val).
    Fails open (True) when no OBI data for this ticker — never block trades on missing data.
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

    # Gate 1: time window
    if mins_left < cfg["time_min"] or mins_left > cfg["time_max"]:
        return _make_skip("yes", f"s2_time_gate:{mins_left:.1f}min", abs_pct, mins_left, variant="strategy2")

    # Gate 2: minimum distance from strike
    if abs_pct < cfg["min_dist"]:
        return _make_skip("yes", f"s2_dist_gate:{abs_pct:.4f}<{cfg['min_dist']}", abs_pct, mins_left, variant="strategy2")

    # Direction pointer: contract price velocity
    direction, vel_delta = _s2_contract_direction(ticker, cfg["min_vel_delta"], cfg["vel_lookback"])
    if direction is None:
        return _make_skip("yes", "s2_no_velocity_data", abs_pct, mins_left, variant="strategy2")

    side = direction
    entry_price = yes_ask if side == "yes" else no_ask

    # Continuation-only: velocity direction must agree with price position relative to strike.
    if side == "yes" and current_price < strike:
        return _make_skip(side, "s2_reversal_gate:vel=yes_price_below", abs_pct, mins_left, variant="strategy2")
    if side == "no" and current_price > strike:
        return _make_skip(side, "s2_reversal_gate:vel=no_price_above", abs_pct, mins_left, variant="strategy2")

    # Gate 3: entry price range (per-asset via get_asset_config)
    _min_p = float(get_asset_config(config, asset, "min_entry_price_cents", 20.0))
    _max_p = float(get_asset_config(config, asset, "max_entry_price_cents", 76.0))
    if entry_price < _min_p or entry_price > _max_p:
        return _make_skip(side, f"s2_price_filter:{entry_price:.0f}c", abs_pct, mins_left,
                          variant="strategy2", price_filter=True)

    # Gate 4: OBI confirmation (required gate, not optional adjustment)
    obi_ok, obi_val = _s2_obi_gate(ticker, side, cfg["min_obi"])
    if not obi_ok:
        return _make_skip(
            side,
            f"s2_obi_gate:obi={obi_val:.2f}_side={side}_min={cfg['min_obi']}",
            abs_pct, mins_left, variant="strategy2",
        )

    # Win probability: empirical lookup (tanh fallback when bucket uncalibrated)
    base_p = _s2_lookup_win_rate(asset, vel_delta, mins_left, cfg)
    win_prob = min(0.99, base_p)

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
        "S2 TRADE %s %s | vel=%s delta=%.2f obi=%s dist=%.4f ev=%.3f wp=%.3f mins=%.1f",
        asset, ticker, direction, vel_delta or 0, _obi_str, abs_pct, ev, win_prob, mins_left,
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
