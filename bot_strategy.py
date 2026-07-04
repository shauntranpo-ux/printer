"""bot_strategy.py - S1 (BTC-lead cross-asset dislocation) and S2 (spot fair-value dislocation) brains."""
import datetime
import json
import logging
import logging.handlers
import math
import os
import statistics
import time
from collections import deque
from zoneinfo import ZoneInfo

import bot_state
from bot_infra import read_config, get_asset_config
import asset_manager
import sessions

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


# Shared utilities

def track_contract_price(ticker: str, price: float) -> None:
    """Record the latest contract YES-ask price for S2 velocity signal."""
    if ticker not in bot_state._contract_price_history:
        bot_state._contract_price_history[ticker] = deque(maxlen=60)
    bot_state._contract_price_history[ticker].append((time.time(), price))


def track_contract_mid(ticker: str, yes_ask, no_ask) -> None:
    """
    Record the de-vigged YES mid (cents) for the staleness gate. Separate history from
    track_contract_price - that one stores raw yes_ask tuples with legacy consumers.
    Never raises.
    """
    try:
        mid = _market_implied_p_yes(yes_ask, no_ask)
        if mid is None:
            return
        if ticker not in bot_state._contract_mid_history:
            bot_state._contract_mid_history[ticker] = deque(maxlen=120)
        bot_state._contract_mid_history[ticker].append((time.time(), mid * 100.0))
    except Exception:
        pass


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


# Digital-option fair value (Bachelier/lognormal, zero-drift) + live realized vol

# Typical 15-min 1-sigma move as a FRACTION of price. Cold-start fallback only: once
# the market-implied EWMA is warm, _sigma_eff anchors to it instead. Values re-fit
# 2026-07 from realized daily vol (annual/sqrt(365)/sqrt(96)) and cross-checked against
# the sigma implied by Kalshi's own quotes in the trade log. The previous table
# (BTC .008 .. DOGE .015) was 2.5-4x too high and made every OTM contract look cheap.
_ASSET_VOL_15M = {
    "BTC": 0.0023, "ETH": 0.0041, "SOL": 0.0046, "XRP": 0.0043, "DOGE": 0.005,
}

# Frozen copy of the old table for the legacy dislocation fast-path and its tests.
# Not on the live decision path; do not re-fit.
_LEGACY_VOL_15M = {
    "BTC": 0.008, "ETH": 0.007, "SOL": 0.012, "XRP": 0.010, "DOGE": 0.015,
}

# Live-vol estimator tuning.
_SIGMA_MIN_FRAC   = 1e-4     # floor a fractional sigma so z stays finite
_SIGMA_MIN_PERIOD = 1e-6     # degenerate period-sigma -> hard 0/1 digital
_VOL_WINDOW_MIN   = 10.0     # lookback for live realized vol
_MIN_PAIRS        = 8        # need this many valid return pairs, else static
_MIN_SPAN_SEC     = 180.0    # need this much real time coverage, else static
_GRID_STEP_SEC    = 15.0     # resample the 1s feed onto this grid before differencing
_DT_MAX           = 120.0    # drop stale-gap/reconnect pairs
_FLOOR_MULT       = 0.5      # clamp live sigma to [0.5x, 2x] static
_CEIL_MULT        = 2.0
_WINSOR_K         = 9.0      # cap per-pair per-second variance at k*static^2/900 (3-sigma)


def _bachelier_p_above(spot: float, strike: float, secs_left: float,
                       sigma_15m_frac: float) -> float:
    """
    P(price > strike at expiry) for a short-horizon zero-drift digital, via the normal CDF.
    RAW and UNCAPPED, in (0,1); callers take p for the above-side (YES), 1-p for below (NO).
    Zero drift is justified: mu*T ~ 2.4e-5 over 15 min << sigma ~ 3e-3..1.5e-2.
    """
    if strike <= 0 or spot <= 0:
        return 0.5
    sig = max(float(sigma_15m_frac), _SIGMA_MIN_FRAC)
    time_frac = max(1.0 / 900.0, secs_left / 900.0)
    period_sigma = sig * math.sqrt(time_frac)
    if period_sigma <= _SIGMA_MIN_PERIOD:
        return 1.0 if spot >= strike else 0.0
    z = math.log(spot / strike) / period_sigma
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _grid_resample(pts, t_start: float, t_end: float, step: float) -> list:
    """
    Nearest price per grid point over [t_start, t_end]; None where no print lands
    within step/2 of the grid time. pts = chronological (ts, price) pairs.
    """
    if t_end <= t_start or step <= 0:
        return []
    grid = []
    j = 0
    n = len(pts)
    k = 0
    t = t_start
    while t <= t_end + 1e-9:
        while j < n and pts[j][0] < t - step / 2.0:
            j += 1
        best = None
        best_d = None
        m = j
        while m < n and pts[m][0] <= t + step / 2.0:
            d = abs(pts[m][0] - t)
            if pts[m][1] > 0 and (best_d is None or d < best_d):
                best, best_d = pts[m][1], d
            m += 1
        grid.append((t, best))
        k += 1
        t = t_start + k * step
    return grid


def _live_sigma_15m(asset: str, window_minutes: float = _VOL_WINDOW_MIN) -> float:
    """
    Live 15-minute 1-sigma fractional move from the price deque.

    The feed appends ~1 print/second, so the deque is resampled onto a _GRID_STEP_SEC
    grid FIRST and the quadratic-variation estimator sqrt( (sum r^2 / sum dt) * 900 )
    runs on consecutive valid grid points (gaps of missing cells allowed up to _DT_MAX).
    Differencing adjacent 1s prints directly would put every pair below any sane dt
    floor - that exact mistake kept this estimator returning its fallback for weeks.
    Winsorizes each pair's per-second variance so one fat-finger print can't inflate
    sigma. Falls back to static _ASSET_VOL_15M x ToD on thin data; the live estimate
    itself omits the ToD multiplier since realized vol already reflects it.
    """
    base = _ASSET_VOL_15M.get(asset, 0.0023)
    static = base * _time_of_day_vol_multiplier()
    raw = asset_manager._prices.get(asset)
    if not raw or len(raw) < 3:
        return static
    pts = [(ts, p) for ts, p in raw if p > 0]
    if len(pts) < 3:
        return static
    now = pts[-1][0]
    if (pts[-1][0] - pts[0][0]) < _MIN_SPAN_SEC:
        return static
    grid = _grid_resample(pts, now - window_minutes * 60.0, now, _GRID_STEP_SEC)
    var_cap = _WINSOR_K * (base ** 2) / 900.0   # per-second variance cap (3-sigma)
    sum_r2 = 0.0
    sum_dt = 0.0
    n = 0
    prev = None   # (ts, price) of the last valid grid cell
    for ts, p in grid:
        if p is None:
            continue
        if prev is not None:
            dt = ts - prev[0]
            if 0 < dt <= _DT_MAX:
                r = math.log(p / prev[1])
                per_sec = min((r * r) / dt, var_cap)    # winsorize per-pair
                sum_r2 += per_sec * dt
                sum_dt += dt
                n += 1
        prev = (ts, p)
    if n < _MIN_PAIRS or sum_dt < _MIN_SPAN_SEC:
        return static
    var_per_sec = sum_r2 / sum_dt
    if var_per_sec <= 0:
        return static
    sigma_15m = math.sqrt(var_per_sec * 900.0)
    return max(_FLOOR_MULT * base, min(_CEIL_MULT * base, sigma_15m))


# Market-anchored EV. Anchor the model probability to the de-vigged market mid and
# allow only a bounded deviation from it (shrinkage toward the market prior); EV is
# measured against the price paid, net of the half-spread and Kalshi fee.
#   min_market_edge=0.035  model must beat the de-vigged mid by >=3.5 prob-pts
#   min_ev_anchored=0.025  require >=2.5 pts of profit after spread+fee (one taker
#                          fee ~1.5-1.75c + half-spread, no exit fee)
# Cost-based defaults; refine per-asset from settled data (scripts/calibrate_from_csv.py).

def _kalshi_fee_frac(price_frac: float, fee_rate: float = 0.07) -> float:
    """Kalshi per-contract fee in dollars: rate * p * (1-p). price_frac in [0,1]."""
    p = max(0.0, min(1.0, price_frac))
    return fee_rate * p * (1.0 - p)


def _market_implied_p_yes(yes_ask, no_ask):
    """
    De-vigged market-implied P(YES) as a fraction in [0,1], from both asks (cents).

    On a two-sided Kalshi book yes_bid = 100 - no_ask, so the de-vigged YES mid is
    avg(yes_ask, 100 - no_ask). Using both asks removes the bid/ask vig. Returns
    None when either ask is missing/non-positive (book not usable).
    """
    try:
        ya = float(yes_ask)
        na = float(no_ask)
    except (TypeError, ValueError):
        return None
    if ya <= 0 or na <= 0 or ya >= 100 or na >= 100:
        return None
    yes_bid = 100.0 - na
    mid = (ya + yes_bid) / 2.0
    return max(0.0, min(1.0, mid / 100.0))


def _anchored_ev(side, yes_ask, no_ask, raw_model_p_yes,
                 max_edge_cap, fee_rate=0.07):
    """
    Shrink the model's P(YES) toward the de-vigged market mid (cap absolute
    deviation at max_edge_cap), then compute EV against the price actually paid.

    Returns (ev, model_p_side, mkt_p_side, market_edge) or None when the book is
    unusable. market_edge = the (capped) model edge over the market mid for the
    traded side, net of spread+fee.
    """
    mid_yes = _market_implied_p_yes(yes_ask, no_ask)
    if mid_yes is None:
        return None
    # Shrinkage toward the market prior: the model may deviate from consensus by
    # at most max_edge_cap. This is what kills the "0.58 model vs 0.29 market" runaway.
    capped_p_yes = max(mid_yes - max_edge_cap,
                       min(mid_yes + max_edge_cap, float(raw_model_p_yes)))
    if side == "yes":
        entry = float(yes_ask) / 100.0
        model_p_side = capped_p_yes
        mkt_p_side = mid_yes
    else:
        entry = float(no_ask) / 100.0
        model_p_side = 1.0 - capped_p_yes
        mkt_p_side = 1.0 - mid_yes
    fee = _kalshi_fee_frac(entry, fee_rate)
    ev = model_p_side - entry - fee
    market_edge = model_p_side - mkt_p_side
    return ev, model_p_side, mkt_p_side, market_edge


def shadow_fav_candidate(asset, spot, strike, yes_ask, no_ask, secs_left, config):
    """
    Measurement-only favorite-bias candidate: would we buy the FAVORITE side here?

    Kalshi's longshot bias (documented market-wide, and visible in this bot's own
    trade log) implies the 70-88c favorite is slightly underpriced late in the window
    when the spot is decisively past the strike. This never trades - it returns a
    decision_log payload (strategy 's_fav', would_trade=0) so the settlement backfill
    scores the idea; promotion to capital is a later, data-gated decision.

    Fires only when: 3-6 min left, |z| >= 0.8, the favorite side (de-vigged mid
    0.70-0.88) agrees with the z sign, and the last 2 spot prints confirm the side.
    Returns None otherwise. Never raises.
    """
    try:
        if not config.get("shadow_fav_enabled", True):
            return None
        mins_left = float(secs_left) / 60.0
        if not (3.0 <= mins_left <= 6.0):
            return None
        if spot is None or strike is None or float(spot) <= 0 or float(strike) <= 0:
            return None
        mid = _market_implied_p_yes(yes_ask, no_ask)
        if mid is None:
            return None
        sigma_eff = _sigma_eff(asset, config)
        eff = _effective_secs(float(secs_left), config)
        ps = sigma_eff * math.sqrt(max(1.0 / 900.0, eff / 900.0))
        if ps <= _SIGMA_MIN_PERIOD:
            return None
        adj = _basis_adjusted_spot(float(spot), asset)
        z = math.log(adj / float(strike)) / ps
        if abs(z) < 0.8:
            return None
        side = "yes" if z > 0 else "no"
        p_side = mid if side == "yes" else 1.0 - mid
        if not (0.70 <= p_side <= 0.88):
            return None
        dq = asset_manager._prices.get(asset)
        if not _spot_confirm(list(dq) if dq else [], float(strike), z > 0, 2):
            return None
        model_p_yes = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        model_p_side = model_p_yes if side == "yes" else 1.0 - model_p_yes
        return {
            "ticker": None,  # caller fills ticker/ts/mode
            "asset": asset, "strategy": "s_fav", "side": side,
            "model_p_yes": model_p_yes,
            "market_mid_p_yes": mid,
            "market_edge": model_p_side - p_side,
            "entry_price_cents": yes_ask if side == "yes" else no_ask,
            "secs_left": float(secs_left), "would_trade": False,
            "spot": adj, "strike": float(strike), "sigma_eff": sigma_eff, "z": z,
        }
    except Exception:
        return None


def _kelly_stake(model_p_side: float, entry_price_cents: float, config: dict) -> float:
    """
    Stake for one entry: quarter-Kelly, scaled DOWN from the configured clip and never
    above it. Thin edges get small stakes instead of the full clip; a rich edge tops
    out at the clip (the $25 user mandate - scaling size up is a user decision, not a
    code path). Floored at min_stake_dollars so a passed-gates trade stays meaningful.
    """
    try:
        raw_clip = config.get("trade_amount_dollars", 25.0)
        clip = 25.0 if raw_clip is None else min(25.0, max(0.0, float(raw_clip)))
    except (TypeError, ValueError):
        clip = 25.0
    if clip <= 0:
        # A configured $0 stake means "do not trade"; it must never inflate to the cap.
        return 0.0
    if not config.get("kelly_sizing_enabled", True):
        return clip
    try:
        p = float(model_p_side)
        c = float(entry_price_cents) / 100.0
        cap = float(config.get("kelly_cap", 0.05) or 0.05)
        floor = float(config.get("min_stake_dollars", 5.0) or 5.0)
    except (TypeError, ValueError):
        return clip
    if not (0.0 < c < 1.0) or not (0.0 < p < 1.0) or cap <= 0:
        return clip
    # Kelly fraction for a binary costing c paying 1: f = p - (1-p) * c / (1-c).
    kelly = p - (1.0 - p) * c / (1.0 - c)
    quarter = 0.25 * kelly
    stake = clip * quarter / cap
    return max(min(floor, clip), min(clip, stake))


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


# Older EMA-momentum config and helpers. Not on the trading decision path anymore
# (S1/S2 are now the fair-value brains), but still used to compute the dashboard
# tests and the offline backtest script, so they stay for now.

_S1_ASSET_CONFIG: dict = {
    #           min_dist  max_rv  min_momentum  min_ev  t_min  t_max
    # min_dist raised: only trade when price is meaningfully far from strike.
    # min_momentum: 60-second price change required to confirm recent directional move.
    # time_min=1.0: skip final minute (wide spreads, AMM settlement chaos).
    # time_max=12.0: skip very early (AMM hasn't had time to anchor contract price).
    "BTC":  dict(min_dist=0.0030, max_rv=1.0, min_momentum=0.0030, min_ev=0.15, time_min=1.0, time_max=12.0),
    "ETH":  dict(min_dist=0.0030, max_rv=1.0, min_momentum=0.0025, min_ev=0.15, time_min=1.0, time_max=12.0),
    "SOL":  dict(min_dist=0.0050, max_rv=1.0, min_momentum=0.0040, min_ev=0.15, time_min=1.0, time_max=12.0),
    "XRP":  dict(min_dist=0.0030, max_rv=1.0, min_momentum=0.0025, min_ev=0.15, time_min=1.0, time_max=12.0),
    "DOGE": dict(min_dist=0.0070, max_rv=1.0, min_momentum=0.0050, min_ev=0.15, time_min=1.0, time_max=12.0),
}


def _is_quiet_hours(config: dict) -> bool:
    """
    True when current ET time is in the overnight quiet window.
    Default: 10pm-9am ET (22:00-09:00). Configurable via:
      quiet_hours_enabled (bool, default True)
      quiet_start_et      (int hour 0-23, default 22)
      quiet_end_et        (int hour 0-23, default 9)
    Returns False when disabled or on any clock error.
    """
    if not config.get("quiet_hours_enabled", True):
        return False
    try:
        now_et = datetime.datetime.now(ZoneInfo("America/New_York"))
        hour = now_et.hour
        start = int(config.get("quiet_start_et", 22))
        end   = int(config.get("quiet_end_et", 9))
        if start > end:
            return hour >= start or hour < end
        else:
            return start <= hour < end
    except Exception:
        return False



def _session_allowed(config: dict) -> tuple:
    """
    Manual time-of-day / day-of-week filter (default OFF -> always allowed).

    Lets the user skip specific ET sessions or weekends once the Edge dashboard shows
    which times lose. Returns (allowed, session_label). Blocks when the current ET session
    is in config['blocked_sessions'] or when config['block_weekends'] is set and it is a
    weekend in ET. Fail-open: any clock/config error -> allowed (never block on a fault).
    """
    try:
        now_et = datetime.datetime.now(ZoneInfo("America/New_York"))
        session = sessions.session_for_dt(now_et)
        blocked = set(config.get("blocked_sessions") or [])
        if config.get("auto_gate_enabled", True):
            blocked |= bot_state._auto_blocked_sessions
        if session in blocked:
            return False, session
        if config.get("block_weekends", False) and now_et.weekday() >= 5:
            return False, "weekend"
        return True, session
    except Exception:
        return True, ""


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
    Legacy normal-certainty model: P(price stays on current side of strike until
    settlement), capped 0.50-0.85. No live callers (tests/offline scripts only);
    frozen on the old static vol table so re-fitting the live table does not move it.
    """
    vol_15m = _LEGACY_VOL_15M.get(asset, 0.008) * _time_of_day_vol_multiplier()
    time_frac  = max(0.01, secs_left / 900.0)
    period_vol = max(vol_15m * math.sqrt(time_frac), _SIGMA_MIN_PERIOD)
    z    = dist_pct / period_vol
    cert = 0.5 * (1.0 + math.erf(z / math.sqrt(2)))
    return max(0.50, min(0.85, cert))


def _time_of_day_vol_multiplier() -> float:
    """
    Adjusts realized vol by time of day (ET).
    US open (9:30-11:30 ET): 1.30x - high vol period, be conservative.
    US close (15:00-16:30 ET): 1.20x - elevated vol.
    Overnight (0:00-7:00 ET): 0.80x - thin market, lower vol.
    Otherwise: 1.00x baseline.
    Fail-open: returns 1.00 on any clock error.
    """
    try:
        now_et = datetime.datetime.now(ZoneInfo("America/New_York"))
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

    Research: Polymarket 5-min BTC study core alpha signal - fires when contract
    price lags the asset move by >0.05%.
    """
    if dist_pct < _DISLOCATION_THRESHOLD:
        return 0.0, 0.5

    # Dislocation stays on the FROZEN legacy vol dict (the fast-path bypasses momentum
    # gates and is off the live decision path; re-fitting the live table must not move it).
    vol = _LEGACY_VOL_15M.get(asset, 0.008)
    time_frac  = max(0.05, secs_left / 900.0)
    time_decay = vol * math.sqrt(time_frac)

    scale = 5.0
    fair_p = 0.5 + (dist_pct / max(time_decay, 1e-6)) * scale / 20.0
    fair_p = max(0.45, min(0.80, fair_p))

    contract_price_frac = yes_ask / 100.0
    edge = fair_p - contract_price_frac
    return edge, fair_p


# Fair-value framework (NEW S1/S2)
#
# Both new brains stop predicting direction from recent noise (momentum / contract
# velocity) and instead compute a principled Bachelier fair value for P(YES), then
# trade only when the de-vigged market mid is stale-cheap relative to it (a real
# AMM/book lag is the one place an edge can live). Direction is decided by the
# anchored-EV gate, not by momentum. See the plan's ACTIVE TASK section.

# BTC-lead cross-asset betas: BTC leads the alts intraday, so a BTC move predicts a
# lagged alt move of beta * btc_ret. Loaded from data/betas.json (mtime-cached),
# with a hardcoded fallback so a missing/broken file never breaks trading.
_BETA_DEFAULTS = {"BTC": 1.0, "ETH": 0.565, "SOL": 0.455, "XRP": 0.404, "DOGE": 0.341}
_BETA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "betas.json")
_BETA_CACHE: dict = {}
_BETA_MTIME: float = -1.0

_SIGMA_BLEND_W = 0.45   # weight on live sigma in the blended effective vol


def _load_betas() -> dict:
    """
    Return {asset: beta}, refreshed whenever data/betas.json mtime changes.
    Falls back to _BETA_DEFAULTS on any error (missing / invalid / NaN). Never raises.
    """
    global _BETA_CACHE, _BETA_MTIME
    try:
        mtime = os.path.getmtime(_BETA_PATH)
    except OSError:
        if not _BETA_CACHE:
            _BETA_CACHE = dict(_BETA_DEFAULTS)
        return _BETA_CACHE
    if mtime != _BETA_MTIME or not _BETA_CACHE:
        try:
            with open(_BETA_PATH, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            parsed = {}
            for asset, entry in raw.items():
                val = entry.get("beta") if isinstance(entry, dict) else entry
                val = float(val)
                if val == val:  # drop NaN
                    parsed[asset] = val
            merged = dict(_BETA_DEFAULTS)
            merged.update(parsed)
            _BETA_CACHE = merged
            _BETA_MTIME = mtime
        except Exception:
            if not _BETA_CACHE:
                _BETA_CACHE = dict(_BETA_DEFAULTS)
    return _BETA_CACHE


def _asset_beta(asset: str, config: dict | None = None) -> float:
    """
    BTC-LEAD beta for an asset: how much the alt is expected to move AFTER a BTC move.

    The static betas file holds the long-sample lead estimate (~0.34-0.46 for the
    alts). A live refit is accepted only within [0.5x, 1.5x] of the static value and
    then SHRUNK halfway toward it - the old absolute [0.05, 1.5] accept range let a
    contemporaneous 30s-return slope (~1.5) overwrite the lead beta and inflate every
    S1 prediction by 3-4x.
    """
    static = float(_load_betas().get(asset, _BETA_DEFAULTS.get(asset, 0.4)))
    if static <= 0:
        static = 0.4
    lo_mult, hi_mult = 0.5, 1.5
    if config:
        try:
            lo_mult = float(config.get("s1_beta_clamp_lo", 0.5))
            hi_mult = float(config.get("s1_beta_clamp_hi", 1.5))
        except (TypeError, ValueError):
            lo_mult, hi_mult = 0.5, 1.5
    live = bot_state._live_betas.get(asset)
    if isinstance(live, (int, float)) and lo_mult * static <= float(live) <= hi_mult * static:
        return 0.5 * static + 0.5 * float(live)
    return static


def _implied_sigma_from_quote(asset, spot, strike, yes_ask, no_ask, secs_left, config) -> float:
    """
    Back out the 15-min sigma implied by a fresh two-sided quote, or None if the quote
    is unusable as a vol observation. From the de-vigged mid p: z = Phi^-1(p), then
    period_sigma = ln(spot/strike)/z and sigma15 = period_sigma / sqrt(eff_secs/900).

    Acceptance filters keep dislocated/degenerate quotes out of the anchor:
    tight book (spread <= 3c), mid away from both certainty and the coin-flip
    singularity (0.10..0.90 and |z| >= 0.2), mid-window timing (3..12 min), and a
    sign match between mid and spot-vs-strike (a mismatch IS the dislocation the
    brains trade - it must not pollute the vol estimate). Result sanity-bounded to
    [0.2x, 5x] the static base.
    """
    try:
        if spot is None or strike is None or spot <= 0 or strike <= 0:
            return None
        mins_left = float(secs_left) / 60.0
        if not (3.0 <= mins_left <= 12.0):
            return None
        spread = float(yes_ask) + float(no_ask) - 100.0
        if spread > 3.0:
            return None
        mid = _market_implied_p_yes(yes_ask, no_ask)
        if mid is None or not (0.10 <= mid <= 0.90):
            return None
        z_mkt = statistics.NormalDist().inv_cdf(mid)
        if abs(z_mkt) < 0.2:
            return None
        period_sigma = math.log(_basis_adjusted_spot(float(spot), asset) / float(strike)) / z_mkt
        if period_sigma <= 0:
            return None
        eff = _effective_secs(float(secs_left), config or {})
        sigma15 = period_sigma / math.sqrt(max(1.0 / 900.0, eff / 900.0))
        base = _ASSET_VOL_15M.get(asset, 0.0023)
        if not (0.2 * base <= sigma15 <= 5.0 * base):
            return None
        return sigma15
    except Exception:
        return None


def update_implied_sigma(asset, spot, strike, yes_ask, no_ask, secs_left, config) -> None:
    """
    Fold one quote observation into the per-asset implied-sigma EWMA
    (bot_state._implied_sigma). Elapsed-time weighting: alpha = 1 - 0.5^(dt/halflife).
    Piggybacks on orderbook fetches the loop already makes - never fetches, never raises.
    """
    try:
        obs = _implied_sigma_from_quote(asset, spot, strike, yes_ask, no_ask, secs_left, config)
        if obs is None:
            return
        now = time.time()
        cur = bot_state._implied_sigma.get(asset)
        halflife = float((config or {}).get("sigma_implied_halflife_secs", 2700) or 2700)
        if not cur or not cur.get("sigma"):
            bot_state._implied_sigma[asset] = {"sigma": obs, "ts": now, "n": 1}
            return
        dt = max(1.0, now - float(cur.get("ts", now)))
        alpha = 1.0 - 0.5 ** (dt / max(1.0, halflife))
        # Cap so a single observation never dominates a warm anchor; the tiny floor
        # only guards degenerate dt, it must stay below the ~10s-cadence raw alpha
        # or it would override the configured halflife.
        alpha = min(0.25, max(0.001, alpha))
        ewma = (1.0 - alpha) * float(cur["sigma"]) + alpha * obs
        bot_state._implied_sigma[asset] = {
            "sigma": ewma, "ts": now, "n": int(cur.get("n", 0)) + 1,
        }
    except Exception:
        pass


def _sigma_eff(asset: str, config: dict | None = None) -> float:
    """
    Effective 15-min fractional sigma for pricing.

    Primary path (implied anchor fresh): blend the market-implied EWMA with the live
    realized vol and clamp the result to a band around the implied anchor. Anchoring
    to the market's own vol means the fair value can disagree with the market only
    through spot freshness (a stale book), never through a vol opinion - the trade
    log showed our vol opinion loses. No ToD multiplier here: the quotes already
    embed intraday vol.

    Cold-start path (no fresh implied anchor): blend live realized vol with the
    static table x ToD, clamped to [0.5x, 2x] the static base.

    Finally scaled by the per-asset sigma_scale fitted from settled decisions.
    """
    if config is None:
        try:
            config = read_config()
        except Exception:
            config = {}
    base = _ASSET_VOL_15M.get(asset, 0.0023)
    live = _live_sigma_15m(asset)
    imp = bot_state._implied_sigma.get(asset)
    max_age = float(config.get("sigma_implied_max_age_secs", 900) or 900)
    fresh = (
        isinstance(imp, dict) and imp.get("sigma")
        and int(imp.get("n", 0)) >= 3
        and (time.time() - float(imp.get("ts", 0.0))) <= max_age
    )
    if fresh:
        anchor = float(imp["sigma"])
        w_imp = float(config.get("sigma_implied_weight", 0.6))
        w_live = float(config.get("sigma_live_weight", 0.4))
        tot = max(1e-9, w_imp + w_live)
        blended = (w_imp * anchor + w_live * live) / tot
        lo = float(config.get("sigma_clamp_lo", 0.6)) * anchor
        hi = float(config.get("sigma_clamp_hi", 1.7)) * anchor
        out = max(lo, min(hi, blended))
    else:
        static = base * _time_of_day_vol_multiplier()
        blended = _SIGMA_BLEND_W * live + (1.0 - _SIGMA_BLEND_W) * static
        out = max(_FLOOR_MULT * base, min(_CEIL_MULT * base, blended))
    return out * _applied_sigma_scale(asset)


def _applied_sigma_scale(asset: str) -> float:
    """The fitted sigma_scale multiplier currently in force (the clamp _sigma_eff uses)."""
    try:
        return min(2.0, max(0.5, float(bot_state._sigma_scale.get(asset, 1.0) or 1.0)))
    except (TypeError, ValueError, AttributeError):
        return 1.0


def _calibrated_p(p_yes: float, strategy: str, config: dict) -> float:
    """
    Apply the fitted probability calibration to a raw fair value:
    p_cal = 0.5 + prob_scale * (p - 0.5).

    prob_scale is fitted from the bot's own settled decision_log by the periodic
    recalibration job (scripts/calibration.py) and stored in bot_state._brain_cal_*;
    1.0 (default / thin data / calibration_enabled=false) is a no-op. Clamped so a
    bad fit can never push the model to extremes.
    """
    if not config.get("calibration_enabled", True):
        return p_yes
    cal = bot_state._brain_cal_s1 if strategy == "strategy1" else bot_state._brain_cal_s2
    try:
        w = float(cal.get("prob_scale", 1.0) or 1.0)
    except (TypeError, ValueError):
        w = 1.0
    w = min(1.2, max(0.5, w))
    return min(0.999, max(0.001, 0.5 + w * (p_yes - 0.5)))


def _basis_adjusted_spot(spot: float, asset: str) -> float:
    """
    Shift the spot by the fitted per-asset settlement basis offset (fractional):
    spot' = spot * exp(-offset), equivalent to z = (log(spot/strike) - offset)/ps.

    Offsets come from the settlement_basis fit (bot_state._basis_offsets); 0/absent
    = no correction. Clamped to +/-10bp at fit time.
    """
    try:
        offset = float(bot_state._basis_offsets.get(asset, 0.0) or 0.0)
    except (TypeError, ValueError):
        return spot
    if offset == 0.0 or spot <= 0:
        return spot
    return spot * math.exp(-min(0.0010, max(-0.0010, offset)))


def _effective_secs(secs_left: float, config: dict) -> float:
    """
    Time to the settlement VALUE rather than the clock close: Kalshi settles 15-min
    crypto on a ~settlement_avg_seconds average, so variance accrues to the center
    of that window, secs_left - avg/2. Floored at 1s.
    """
    try:
        avg = float(config.get("settlement_avg_seconds", 60) or 0.0)
    except (TypeError, ValueError):
        avg = 60.0
    avg = min(300.0, max(0.0, avg))
    return max(1.0, secs_left - avg / 2.0)


def _nearest_price(deq, target_ts):
    """Nearest (ts, price) tuple to target_ts from a (ts, price) deque; None if empty."""
    best = None
    for ts, p in deq:
        if p <= 0:
            continue
        if best is None or abs(ts - target_ts) < abs(best[0] - target_ts):
            best = (ts, p)
    return best


def _fair_value_decision(fair_p_yes, yes_ask, no_ask, max_edge, fee_rate):
    """
    Pick the cheap side from a model P(YES) and run the shared anchored-EV gate.

    Direction is decided by which side the de-vigged mid under-prices vs the model
    (NOT by momentum). Returns (side, ev, model_p_side, mkt_p_side, market_edge) or
    None when the book is unusable.
    """
    mid = _market_implied_p_yes(yes_ask, no_ask)
    if mid is None:
        return None
    side = "yes" if fair_p_yes >= mid else "no"
    anch = _anchored_ev(side, yes_ask, no_ask, fair_p_yes, max_edge, fee_rate)
    if anch is None:
        return None
    ev, model_p_side, mkt_p_side, market_edge = anch
    return side, ev, model_p_side, mkt_p_side, market_edge


def _spot_confirm(prices, strike: float, want_above: bool, n: int) -> bool:
    """True iff the last n price prints all sit strictly on the want_above side of strike."""
    if not prices or len(prices) < n or strike <= 0:
        return False
    tail = [p for _, p in list(prices)[-n:]]
    if want_above:
        return all(p > strike for p in tail)
    return all(p < strike for p in tail)


def _staleness_check(asset, ticker, side, sigma_eff, config):
    """
    Evidence that the book is STALE relative to a fresh spot move - the only
    dislocation the fair-value brains are allowed to trade. Persistent model-vs-market
    disagreement without a recent move is model error, not edge (37-trade autopsy).

    Returns (status, info). status:
      "stale_ok"   - spot moved toward the traded side over the last window AND the
                     contract mid has not repriced (the book lagged): tradeable.
      "fresh_book" - no qualifying spot move, or the mid already moved with it: skip.
      "unknown"    - not enough spot/mid history to judge: caller passes fail-open
                     and records mid_hist_n so the gate's value is measurable.
    info: {"mid_hist_n", "spot_move", "mid_move"}. Never raises.
    """
    info = {"mid_hist_n": 0, "spot_move": 0.0, "mid_move": 0.0}
    try:
        if not config.get("staleness_gate_enabled", True):
            return "unknown", info
        win = float(config.get("staleness_window_secs", 60.0) or 60.0)
        now = time.time()
        dq = asset_manager._prices.get(asset)
        pts = [(ts, p) for ts, p in dq] if dq else []
        if not pts or pts[-1][1] <= 0:
            return "unknown", info
        anchor = _nearest_price(pts, now - win)
        if anchor is None or anchor[1] <= 0:
            return "unknown", info
        age = now - anchor[0]
        if not (0.5 * win <= age <= 1.5 * win):
            return "unknown", info
        spot_move = math.log(pts[-1][1] / anchor[1])
        info["spot_move"] = spot_move
        period_sigma_win = sigma_eff * math.sqrt(max(1e-6, win / 900.0))
        min_move = float(config.get("staleness_min_spot_sigma", 0.35)) * period_sigma_win
        moved_toward = (spot_move >= min_move) if side == "yes" else (spot_move <= -min_move)

        hist = bot_state._contract_mid_history.get(ticker)
        mids = list(hist) if hist else []
        info["mid_hist_n"] = len(mids)
        if len(mids) < 2:
            return "unknown", info
        m_anchor = _nearest_price(mids, now - win)
        if m_anchor is None:
            return "unknown", info
        m_age = now - m_anchor[0]
        if not (0.5 * win <= m_age <= 1.5 * win):
            return "unknown", info
        mid_move = abs(mids[-1][1] - m_anchor[1])
        info["mid_move"] = mid_move
        max_mid = float(config.get("staleness_max_mid_move_cents", 3.0))
        if moved_toward and mid_move < max_mid:
            return "stale_ok", info
        return "fresh_book", info
    except Exception:
        return "unknown", info


# Per-asset config for NEW S1 (CA-LEAD-SLOW). lookback in seconds (must exceed the
# ~<=2s sequential co-sampling skew); min_btc_ret = BTC must actually move; min_residual
# = absolute floor on the lag signal; time_min/max in minutes-left. Window tightened to
# 2.5-9.0 min 2026-07: 10+min entries lost $349 over 4 days while 6-10min was positive,
# and books are widest in the first 5 minutes of a window.
_S1_CA_CONFIG: dict = {
    "SOL":  dict(lookback=60.0, min_btc_ret=0.0010, min_residual=0.0004, time_min=2.5, time_max=9.0),
    "XRP":  dict(lookback=60.0, min_btc_ret=0.0010, min_residual=0.0004, time_min=2.5, time_max=9.0),
    "DOGE": dict(lookback=60.0, min_btc_ret=0.0010, min_residual=0.0005, time_min=2.5, time_max=9.0),
}

# Per-asset config for NEW S2 (spot_fv_disloc). min_z = required |ln(spot/strike)/period_sigma|
# (conviction); max_spread_cents = round-trip book width cap; confirm_ticks = spot-sign prints.
_S2_FV_CONFIG: dict = {
    "BTC":  dict(min_z=0.35, max_spread_cents=7.0, confirm_ticks=2, time_min=2.5, time_max=9.0),
    "SOL":  dict(min_z=0.35, max_spread_cents=7.0, confirm_ticks=2, time_min=2.5, time_max=9.0),
    "XRP":  dict(min_z=0.35, max_spread_cents=7.0, confirm_ticks=2, time_min=2.5, time_max=9.0),
    "DOGE": dict(min_z=0.35, max_spread_cents=8.0, confirm_ticks=2, time_min=2.5, time_max=9.0),
}

# Entry-price band for the fair-value brains. 20-85c: sub-20c asks are the longshot
# tail (1W-28L in the trade log; the Kalshi-wide study shows sub-10c contracts lose
# most of their stake), and above 85c fee rounding + fill reliability dominate.
_FV_MIN_ENTRY_CENTS = 20.0
_FV_MAX_ENTRY_CENTS = 85.0


def strategy_brain_s1(
    btc_price, strike, yes_ask, no_ask,
    elapsed_seconds, secs_left, ticker,
    asset: str = "BTC",
) -> dict:
    """
    S1: BTC-lead cross-asset dislocation for SOL/XRP/DOGE.

    BTC leads the alts intraday. When BTC moves over the last ~60s and the alt hasn't
    caught up, the alt's expected spot is alt_now * exp(beta*btc_ret - alt_ret). Price
    a Bachelier digital on that predicted spot and trade only when the de-vigged market
    mid is stale-cheap relative to it (the anchored-EV gate picks the side). BTC/ETH are
    off by default (re-enable via s1_ca_btc_enabled / s1_ca_eth_enabled).
    """
    config = read_config()
    cfg = {**_S1_CA_CONFIG.get(asset, _S1_CA_CONFIG["SOL"]),
           **config.get("s1_config", {}).get(asset, {})}
    mins_left = secs_left / 60.0

    # Resolve the alt (traded-asset) spot - callers pass it as the first arg.
    if asset == "BTC":
        current_price = btc_price
    else:
        raw = asset_manager._prices.get(asset)
        current_price = raw[-1][1] if raw else btc_price
    abs_pct = abs(current_price - strike) / strike if strike > 0 else 0.0

    # Asset scope: CA-LEAD-SLOW predicts the SLOW alt from the FAST BTC lead. BTC has
    # no faster leader; ETH is too efficient. Both disabled by default (re-enableable).
    if asset in ("BTC", "ETH"):
        _flag = "s1_ca_btc_enabled" if asset == "BTC" else "s1_ca_eth_enabled"
        if not config.get(_flag, False):
            return _make_skip("yes", f"s1_ca_disabled:{asset}", abs_pct, mins_left, variant="strategy1")

    # Quiet hours gate - block overnight to avoid thin-market losses
    if _is_quiet_hours(config):
        return _make_skip("yes", "s1_quiet_hours", abs_pct, mins_left, variant="strategy1")

    # Manual session filter (default off) - skip ET sessions / weekends the user has turned
    # off from the Edge dashboard once their per-session net-$ shows red.
    _sess_ok, _sess = _session_allowed(config)
    if not _sess_ok:
        return _make_skip("yes", f"s1_session_gate:{_sess}", abs_pct, mins_left, variant="strategy1")

    # Per-asset regime cooldown: block after N consecutive losses on this asset.
    _cooldown_until = bot_state._s1_cooldown_until.get(asset, 0.0)
    if time.time() < _cooldown_until:
        _remaining = _cooldown_until - time.time()
        return _make_skip(
            "yes", f"s1_cooldown:{_remaining:.0f}s_remaining",
            abs_pct, mins_left, variant="strategy1",
        )

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

    # S1 fire rate guard: max N S1 trades per asset per 60 minutes.
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

    # Cross-asset S1 window guard: block non-BTC assets for 300s after any other non-BTC fires.
    if asset != "BTC":
        _xwin_sec = float(config.get("s1_cross_asset_window_seconds", 300.0))
        for _other, _other_times in bot_state._s1_asset_trade_times.items():
            if _other == asset or _other == "BTC":
                continue
            if any(_now_ts - t < _xwin_sec for t in _other_times):
                return _make_skip(
                    "yes", f"s1_window_guard:{_other}",
                    abs_pct, mins_left, variant="strategy1",
                )

    # Time window - skip the settlement-auction tail (final 90s) and the first ~2 min
    # where the AMM contract price has not yet anchored.
    if mins_left < cfg["time_min"] or mins_left > cfg["time_max"]:
        return _make_skip("yes", f"s1_time_gate:{mins_left:.1f}min", abs_pct, mins_left, variant="strategy1")

    # Cross-asset residual: how far the alt has lagged BTC's lead over the lookback window.
    L = float(cfg["lookback"])
    btc_dq = asset_manager._prices.get("BTC")
    alt_dq = asset_manager._prices.get(asset)
    if not btc_dq or not alt_dq or len(btc_dq) < 3 or len(alt_dq) < 3:
        return _make_skip("yes", "s1_ca_no_data", abs_pct, mins_left, variant="strategy1")
    btc_list = list(btc_dq)
    alt_list = list(alt_dq)
    now_ts = alt_list[-1][0]
    btc_now = btc_list[-1][1]
    alt_now = alt_list[-1][1]
    target = now_ts - L
    b_then = _nearest_price(btc_list, target)
    a_then = _nearest_price(alt_list, target)
    if b_then is None or a_then is None:
        return _make_skip("yes", "s1_ca_no_anchor", abs_pct, mins_left, variant="strategy1")
    btc_age = now_ts - b_then[0]
    alt_age = now_ts - a_then[0]
    # The anchor point must genuinely span ~L (guards a thin/one-sided deque).
    if not (0.5 * L <= btc_age <= 1.5 * L) or not (0.5 * L <= alt_age <= 1.5 * L):
        return _make_skip("yes", "s1_ca_thin_window", abs_pct, mins_left, variant="strategy1")
    if btc_now <= 0 or alt_now <= 0 or b_then[1] <= 0 or a_then[1] <= 0:
        return _make_skip("yes", "s1_ca_bad_price", abs_pct, mins_left, variant="strategy1")

    beta = _asset_beta(asset, config)
    btc_ret = math.log(btc_now / b_then[1])
    alt_ret = math.log(alt_now / a_then[1])
    residual = beta * btc_ret - alt_ret   # >0: alt expected to rise to catch BTC's lead

    sigma_eff = _sigma_eff(asset, config)
    # Noise floor on the residual: half the alt's 1-sigma move over the lookback window.
    sigma_window = sigma_eff * math.sqrt(max(1e-6, L / 900.0))
    min_resid = max(float(cfg["min_residual"]), 0.5 * sigma_window)

    if abs(btc_ret) < float(cfg["min_btc_ret"]):
        return _make_skip("yes", f"s1_ca_btc_flat:{btc_ret:+.4f}", abs_pct, mins_left, variant="strategy1")
    if abs(residual) < min_resid:
        return _make_skip("yes", f"s1_ca_resid_flat:{residual:+.4f}<{min_resid:.4f}",
                          abs_pct, mins_left, variant="strategy1")

    predicted_spot = _basis_adjusted_spot(alt_now * math.exp(residual), asset)
    eff_secs = _effective_secs(secs_left, config)
    fair_p_yes = _bachelier_p_above(predicted_spot, strike, eff_secs, sigma_eff)
    fair_p_yes = _calibrated_p(fair_p_yes, "strategy1", config)
    _ps = sigma_eff * math.sqrt(max(1.0 / 900.0, eff_secs / 900.0))
    z = math.log(predicted_spot / strike) / _ps if (strike > 0 and _ps > 0) else 0.0
    # De-scaled z for the harness: dividing sigma_scale back out makes the logged value
    # independent of the correction in force, so the periodic refit has a stationary
    # target (refitting from post-scale z returns only the residual and oscillates).
    z_raw = z * _applied_sigma_scale(asset)

    _fee_rate = config.get("kalshi_fee_per_contract_cents", 7) / 100.0
    _max_edge = float(cfg.get("max_model_edge", config.get("max_model_edge", 0.08)))
    decision = _fair_value_decision(fair_p_yes, yes_ask, no_ask, _max_edge, _fee_rate)
    if decision is None:
        return _make_skip("yes", "s1_no_market_data", abs_pct, mins_left, variant="strategy1")
    side, ev, model_p_side, mkt_p_side, market_edge = decision
    _raw_p_yes = float(fair_p_yes)
    entry_price = yes_ask if side == "yes" else no_ask
    _raw_p_side = _raw_p_yes if side == "yes" else 1.0 - _raw_p_yes
    gap = abs(_raw_p_side - mkt_p_side)
    fresh_status, fresh_info = _staleness_check(asset, ticker, side, sigma_eff, config)

    def _signals():
        return {
            "win_prob": model_p_side, "ev": ev, "market_edge": market_edge,
            "mkt_p": mkt_p_side, "model_raw_p_yes": _raw_p_yes, "gap": gap,
            "residual": residual, "btc_ret": btc_ret, "beta": beta,
            "predicted_spot": predicted_spot,
            "sigma_eff": sigma_eff, "z": z, "z_raw": z_raw, "spot": predicted_spot,
            "freshness": fresh_status, "mid_hist_n": fresh_info.get("mid_hist_n", 0),
            "abs_pct": abs_pct, "mins_left": mins_left, "strike": strike,
        }

    def _full_skip(reason, keys):
        # Skip that still carries the full model signals so the decision harness
        # records it (this is how the gated modes stay measurable at zero capital).
        return {
            "action": "skip", "side": side,
            "confidence": int(model_p_side * 100),
            "reasoning": reason, "key_signals": keys, "signals": _signals(),
            "win_prob": float(model_p_side), "mom_label": side,
            "mom_pct": float(residual), "vel_signal": "ca_lead",
            "raw_p_yes": _raw_p_yes,
            "mins_left": mins_left, "abs_pct": abs_pct, "above": side == "yes",
            "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
            "strategy_variant": "strategy1",
        }

    # Lag-only sign gate: trade the alt CATCHING UP to BTC's lead, never the fade of an
    # alt that overshot (residual sign opposite the BTC move). The fade mode went 2W-7L;
    # it keeps logging here as a shadow so the harness can prove or bury it with real n.
    if residual * btc_ret <= 0:
        return _full_skip(
            f"s1_ca_fade:resid={residual:+.4f}|btc={btc_ret:+.4f}",
            [f"fade resid:{residual:+.4f}", f"btc_ret:{btc_ret:+.4f}"],
        )

    # Entry-price band (per-asset configurable via get_asset_config).
    _min_p = float(get_asset_config(config, asset, "fv_min_entry_price_cents", _FV_MIN_ENTRY_CENTS))
    _max_p = float(get_asset_config(config, asset, "fv_max_entry_price_cents", _FV_MAX_ENTRY_CENTS))
    if entry_price < _min_p or entry_price > _max_p:
        return _make_skip(side, f"s1_price_filter:{entry_price:.0f}c", abs_pct, mins_left,
                          variant="strategy1", price_filter=True)

    # Tail ban on the de-vigged mid: never buy the longshot side. Sides the market
    # priced 12-40c realized 6-13% in the trade log; the bias premium is on favorites.
    _min_side = float(config.get("s1_min_side_price_cents", 25.0)) / 100.0
    if mkt_p_side < _min_side:
        return _full_skip(
            f"s1_tail_ban:mkt={mkt_p_side:.2f}<{_min_side:.2f}",
            [f"tail mkt_p:{mkt_p_side:.2f}", f"floor:{_min_side:.2f}"],
        )

    # Too-good-to-be-true: REJECT extreme model-vs-market disagreement instead of
    # clamping it into a tradable edge. Win rate fell monotonically with this gap
    # (50% under 0.10 -> 0% above 0.35); past the cap the model is wrong, not the market.
    _max_gap = float(config.get("max_model_market_gap", 0.15))
    if gap > _max_gap:
        return _full_skip(
            f"s1_tgtbt:gap={gap:.3f}>{_max_gap:.2f}",
            [f"gap:{gap:.3f}", f"fair:{fair_p_yes:.3f}", f"mkt:{mkt_p_side:.3f}"],
        )

    # Staleness: only trade when the spot moved toward our side recently and the book
    # has not repriced. "unknown" (thin mid history) passes and is tagged in signals.
    if fresh_status == "fresh_book":
        return _full_skip(
            f"s1_fresh_book:spot={fresh_info.get('spot_move', 0.0):+.4f}|mid={fresh_info.get('mid_move', 0.0):.1f}c",
            [f"spot_move:{fresh_info.get('spot_move', 0.0):+.4f}",
             f"mid_move:{fresh_info.get('mid_move', 0.0):.1f}c"],
        )

    _min_market_edge = float(cfg.get("min_market_edge", config.get("min_market_edge", 0.04)))
    _min_ev = float(cfg.get("min_ev_anchored", config.get("min_ev_anchored", 0.025)))
    if ev < _min_ev or market_edge < _min_market_edge:
        return _full_skip(
            f"s1_ev_gate:ev={ev:.3f}<{_min_ev:.3f}|mkt_edge={market_edge:.3f}<{_min_market_edge:.3f}",
            [f"ev:{ev:.3f}", f"fair:{fair_p_yes:.3f}", f"mkt_edge:{market_edge:.3f}",
             f"resid:{residual:+.4f}"],
        )

    brain_log.info(
        "S1 CA-LEAD %s %s | side=%s resid=%+.4f btc_ret=%+.4f beta=%.2f fair=%.3f ev=%.3f mkt_edge=%.3f gap=%.3f fresh=%s mins=%.1f",
        asset, ticker, side, residual, btc_ret, beta, fair_p_yes, ev, market_edge, gap, fresh_status, mins_left,
    )
    # Auto-gate (GATE-1 per bucket): checked after the EV gate so the decision still
    # reaches the harness (logged with would_trade=0) while the bucket is blocked.
    if config.get("auto_gate_enabled", True) and ("strategy1", asset) in bot_state._auto_blocked_assets:
        return _full_skip(f"s1_auto_gate:{asset}",
                          [f"auto_gate:{asset}", f"ev:{ev:.3f}"])
    return {
        "action": "trade", "side": side,
        "confidence": int(model_p_side * 100),
        "reasoning": (
            f"s1_ca_lead ev={ev:.3f} fair={fair_p_yes:.3f} side={side} "
            f"resid={residual:+.4f} btc_ret={btc_ret:+.4f} fresh={fresh_status} mins={mins_left:.1f}"
        ),
        "key_signals": [
            f"ev:{ev:.3f}", f"fair:{fair_p_yes:.3f}", f"side:{side}",
            f"resid:{residual:+.4f}", f"mkt_edge:{market_edge:.3f}",
        ],
        "signals": _signals(),
        "win_prob": float(model_p_side), "mom_label": side,
        "mom_pct": float(residual), "vel_signal": "ca_lead",
        "raw_p_yes": _raw_p_yes,
        "mins_left": mins_left, "abs_pct": abs_pct, "above": side == "yes",
        "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
        "strategy_variant": "strategy1",
    }


# Older contract-velocity config and helpers. Not on the trading decision path anymore
# (S2 is now the fair-value brain); kept for the tests and offline analysis scripts.

_S2_ASSET_CONFIG: dict = {
    #           min_dist  min_obi  min_vel_delta  vel_lookback  min_ev  t_min  t_max
    # min_vel_delta raised ~40%: require stronger velocity signal to avoid chasing weak moves.
    # min_ev raised to 0.04: require clear positive-EV before entry.
    "BTC":  dict(min_dist=0.0015, min_obi=0.02, min_vel_delta=0.30, vel_lookback=4, min_ev=0.03, time_min=2.0, time_max=12.5),
    "ETH":  dict(min_dist=0.0015, min_obi=0.02, min_vel_delta=0.26, vel_lookback=4, min_ev=0.03, time_min=2.0, time_max=12.5),
    "SOL":  dict(min_dist=0.0025, min_obi=0.02, min_vel_delta=0.42, vel_lookback=3, min_ev=0.03, time_min=2.0, time_max=12.5),
    "XRP":  dict(min_dist=0.0020, min_obi=0.02, min_vel_delta=0.32, vel_lookback=4, min_ev=0.03, time_min=2.0, time_max=12.5),
    "DOGE": dict(min_dist=0.0040, min_obi=0.02, min_vel_delta=0.50, vel_lookback=3, min_ev=0.03, time_min=2.0, time_max=12.5),
}

# Empirical win-rate tables - populated by scripts/calibrate_winrates.py
# Run that script, copy the printed dicts here.
# None entries -> tanh formula fallback (insufficient calibration data).

# Last calibrated: 2026-06-23 via scripts/calibrate_from_csv.py.
# Source: 398 paper trades 2026-06-05 to 2026-06-23. All trades in dist_bucket=0.
# All WLBs <= 0.40 - no bucket has statistically proven edge above breakeven.
# Re-run calibration after 2 more weeks with new gates active.
_S1_WIN_RATE: dict = {
    # All None - forces GBM certainty fallback.
    # 398-trade calibration: all buckets WLB <= 0.40. No proven edge worth hardcoding.
    # GBM floor (0.52) + min_ev=0.15 gate now filters entries lacking real signal.
    "BTC":  {(0,0): None, (0,1): None, (0,2): None, (1,0): None, (1,1): None, (1,2): None, (2,0): None, (2,1): None, (2,2): None, (3,0): None, (3,1): None, (3,2): None},
    "ETH":  {(0,0): None, (0,1): None, (0,2): None, (1,0): None, (1,1): None, (1,2): None, (2,0): None, (2,1): None, (2,2): None, (3,0): None, (3,1): None, (3,2): None},
    "SOL":  {(0,0): None, (0,1): None, (0,2): None, (1,0): None, (1,1): None, (1,2): None, (2,0): None, (2,1): None, (2,2): None, (3,0): None, (3,1): None, (3,2): None},
    "XRP":  {(0,0): None, (0,1): None, (0,2): None, (1,0): None, (1,1): None, (1,2): None, (2,0): None, (2,1): None, (2,2): None, (3,0): None, (3,1): None, (3,2): None},
    "DOGE": {(0,0): None, (0,1): None, (0,2): None, (1,0): None, (1,1): None, (1,2): None, (2,0): None, (2,1): None, (2,2): None, (3,0): None, (3,1): None, (3,2): None},
}

_S2_WIN_RATE: dict = {
    # All set to None - forces tanh fallback with realistic baseline.
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
    3. GBM certainty model (dist + time -> probability).
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

    return 0.52 + 0.10 * math.tanh(vel_delta / max(min_vel, 1e-6))


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


def check_dual_side_arb(
    yes_ask: float,
    no_ask: float,
    fee_per_contract_cents: float = 7,
    threshold: float = 93.0,
) -> dict:
    """
    Structural arbitrage check: YES + NO < threshold -> guaranteed profit.

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
    S2: spot-anchored Bachelier fair-value dislocation for BTC/SOL/XRP/DOGE.

    Price a Bachelier digital on the current spot vs strike and trade only when the
    de-vigged market mid disagrees with it by more than cost (the book is slow to reprice
    a real spot move). No momentum, velocity, or OBI; the anchored-EV gate picks the side.
    ETH is off by default (re-enable via s2_eth_enabled).
    """
    config = read_config()
    cfg = {**_S2_FV_CONFIG.get(asset, _S2_FV_CONFIG["BTC"]),
           **config.get("s2_config", {}).get(asset, {})}
    mins_left = secs_left / 60.0

    # Resolve spot + the recent spot-print deque (for the sign-confirmation gate).
    if asset == "BTC":
        spot_dq = list(bot_state.btc_prices)
        current_price = btc_price
    else:
        raw = asset_manager._prices.get(asset)
        spot_dq = list(raw) if raw else []
        current_price = spot_dq[-1][1] if spot_dq else btc_price
    abs_pct = abs(current_price - strike) / strike if strike > 0 else 0.0

    # ETH disabled by default (re-enableable).
    if asset == "ETH" and not config.get("s2_eth_enabled", False):
        return _make_skip("yes", "s2_fv_disabled:ETH", abs_pct, mins_left, variant="strategy2")

    # Quiet hours gate - block overnight to avoid thin-market losses
    if _is_quiet_hours(config):
        return _make_skip("yes", "s2_quiet_hours", abs_pct, mins_left, variant="strategy2")

    # Manual session filter (default off) - skip ET sessions / weekends the user has turned
    # off from the Edge dashboard once their per-session net-$ shows red.
    _sess_ok, _sess = _session_allowed(config)
    if not _sess_ok:
        return _make_skip("yes", f"s2_session_gate:{_sess}", abs_pct, mins_left, variant="strategy2")

    # Time window - skip the settlement-auction tail (final 90s) and the first ~2 min.
    if mins_left < cfg["time_min"] or mins_left > cfg["time_max"]:
        return _make_skip("yes", f"s2_time_gate:{mins_left:.1f}min", abs_pct, mins_left, variant="strategy2")

    if current_price <= 0 or strike <= 0:
        return _make_skip("yes", "s2_fv_bad_price", abs_pct, mins_left, variant="strategy2")

    # Bachelier fair value on the current spot, basis-adjusted, priced to the
    # effective settlement time (Kalshi settles on a ~60s average, not the close tick).
    sigma_eff = _sigma_eff(asset, config)
    eff_secs = _effective_secs(secs_left, config)
    adj_price = _basis_adjusted_spot(current_price, asset)
    period_sigma = sigma_eff * math.sqrt(max(1.0 / 900.0, eff_secs / 900.0))
    if period_sigma <= _SIGMA_MIN_PERIOD:
        return _make_skip("yes", "s2_fv_degenerate_sigma", abs_pct, mins_left, variant="strategy2")
    z = math.log(adj_price / strike) / period_sigma
    # De-scaled z for the harness (see the S1 note: keeps the sigma_scale refit stationary).
    z_raw = z * _applied_sigma_scale(asset)

    # Conviction gate: need a real distance past the strike - near z=0 the model ~0.5
    # and is indistinguishable from noise.
    if abs(z) < float(cfg["min_z"]):
        return _make_skip("yes", f"s2_fv_lowz:{z:+.3f}<{cfg['min_z']}", abs_pct, mins_left, variant="strategy2")

    # Spot-sign confirmation: the last N prints must all sit on the model's side of the
    # strike (avoid trading a spot that is flickering across the strike).
    want_above = z > 0
    if not _spot_confirm(spot_dq, strike, want_above, int(cfg["confirm_ticks"])):
        return _make_skip("yes", f"s2_fv_flicker:want_above={want_above}", abs_pct, mins_left, variant="strategy2")

    # Round-trip spread gate: skip wide / high-vig books (an edge cannot survive them).
    spread_cents = yes_ask + no_ask - 100.0
    if spread_cents > float(cfg["max_spread_cents"]):
        return _make_skip("yes", f"s2_fv_wide_spread:{spread_cents:.0f}c>{cfg['max_spread_cents']:.0f}c",
                          abs_pct, mins_left, variant="strategy2")

    fair_p_yes = _bachelier_p_above(adj_price, strike, eff_secs, sigma_eff)
    fair_p_yes = _calibrated_p(fair_p_yes, "strategy2", config)

    _fee_rate = config.get("kalshi_fee_per_contract_cents", 7) / 100.0
    _max_edge = float(cfg.get("max_model_edge", config.get("max_model_edge", 0.08)))
    decision = _fair_value_decision(fair_p_yes, yes_ask, no_ask, _max_edge, _fee_rate)
    if decision is None:
        return _make_skip("yes", "s2_no_market_data", abs_pct, mins_left, variant="strategy2")
    side, ev, model_p_side, mkt_p_side, market_edge = decision
    _raw_p_yes = float(fair_p_yes)
    entry_price = yes_ask if side == "yes" else no_ask
    _raw_p_side = _raw_p_yes if side == "yes" else 1.0 - _raw_p_yes
    gap = abs(_raw_p_side - mkt_p_side)
    fresh_status, fresh_info = _staleness_check(asset, ticker, side, sigma_eff, config)

    def _signals():
        return {
            "win_prob": model_p_side, "ev": ev, "market_edge": market_edge,
            "mkt_p": mkt_p_side, "model_raw_p_yes": _raw_p_yes, "gap": gap,
            "z": z, "z_raw": z_raw, "sigma_eff": sigma_eff, "spot": adj_price,
            "spread_cents": spread_cents,
            "freshness": fresh_status, "mid_hist_n": fresh_info.get("mid_hist_n", 0),
            "abs_pct": abs_pct, "mins_left": mins_left, "strike": strike,
        }

    def _full_skip(reason, keys):
        # Skip that still carries the full model signals so the decision harness
        # records it (this is how the gated modes stay measurable at zero capital).
        return {
            "action": "skip", "side": side,
            "confidence": int(model_p_side * 100),
            "reasoning": reason, "key_signals": keys, "signals": _signals(),
            "win_prob": float(model_p_side), "mom_label": side, "mom_pct": 0.0,
            "vel_signal": "fv_disloc",
            "raw_p_yes": _raw_p_yes,
            "mins_left": mins_left, "abs_pct": abs_pct, "above": side == "yes",
            "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
            "strategy_variant": "strategy2", "strategy_version": bot_state._S2_VERSION,
        }

    # Entry-price band (per-asset configurable via get_asset_config).
    _min_p = float(get_asset_config(config, asset, "fv_min_entry_price_cents", _FV_MIN_ENTRY_CENTS))
    _max_p = float(get_asset_config(config, asset, "fv_max_entry_price_cents", _FV_MAX_ENTRY_CENTS))
    if entry_price < _min_p or entry_price > _max_p:
        return _make_skip(side, f"s2_price_filter:{entry_price:.0f}c", abs_pct, mins_left,
                          variant="strategy2", price_filter=True)

    # Tail ban on the de-vigged mid: never buy the longshot side. S2's sub-25c entries
    # went 1W-28L in the trade log; the Kalshi-wide study says cheap tails stay -EV.
    _min_side = float(config.get("s2_min_side_price_cents", 20.0)) / 100.0
    if mkt_p_side < _min_side:
        return _full_skip(
            f"s2_tail_ban:mkt={mkt_p_side:.2f}<{_min_side:.2f}",
            [f"tail mkt_p:{mkt_p_side:.2f}", f"floor:{_min_side:.2f}"],
        )

    # Too-good-to-be-true: REJECT extreme model-vs-market disagreement instead of
    # clamping it into a tradable edge (the clamp was binding on 36/37 logged trades
    # and win rate fell monotonically with the gap).
    _max_gap = float(config.get("max_model_market_gap", 0.15))
    if gap > _max_gap:
        return _full_skip(
            f"s2_tgtbt:gap={gap:.3f}>{_max_gap:.2f}",
            [f"gap:{gap:.3f}", f"fair:{fair_p_yes:.3f}", f"mkt:{mkt_p_side:.3f}"],
        )

    # Staleness: only trade when the spot moved toward our side recently and the book
    # has not repriced. "unknown" (thin mid history) passes and is tagged in signals.
    if fresh_status == "fresh_book":
        return _full_skip(
            f"s2_fresh_book:spot={fresh_info.get('spot_move', 0.0):+.4f}|mid={fresh_info.get('mid_move', 0.0):.1f}c",
            [f"spot_move:{fresh_info.get('spot_move', 0.0):+.4f}",
             f"mid_move:{fresh_info.get('mid_move', 0.0):.1f}c"],
        )

    _min_market_edge = float(cfg.get("min_market_edge", config.get("min_market_edge", 0.04)))
    _min_ev = float(cfg.get("min_ev_anchored", config.get("min_ev_anchored", 0.025)))
    if ev < _min_ev or market_edge < _min_market_edge:
        return _full_skip(
            f"s2_ev_gate:ev={ev:.3f}<{_min_ev:.3f}|mkt_edge={market_edge:.3f}<{_min_market_edge:.3f}",
            [f"ev:{ev:.3f}", f"fair:{fair_p_yes:.3f}", f"mkt_edge:{market_edge:.3f}",
             f"z:{z:+.3f}"],
        )

    brain_log.info(
        "S2 FV-DISLOC %s %s | side=%s z=%+.3f fair=%.3f ev=%.3f mkt_edge=%.3f gap=%.3f fresh=%s spread=%.0fc mins=%.1f",
        asset, ticker, side, z, fair_p_yes, ev, market_edge, gap, fresh_status, spread_cents, mins_left,
    )
    # Auto-gate (GATE-1 per bucket): checked after the EV gate so the decision still
    # reaches the harness (logged with would_trade=0) while the bucket is blocked.
    if config.get("auto_gate_enabled", True) and ("strategy2", asset) in bot_state._auto_blocked_assets:
        return _full_skip(f"s2_auto_gate:{asset}",
                          [f"auto_gate:{asset}", f"ev:{ev:.3f}"])
    return {
        "action": "trade", "side": side,
        "confidence": int(model_p_side * 100),
        "reasoning": (
            f"s2_fv_disloc ev={ev:.3f} fair={fair_p_yes:.3f} side={side} "
            f"z={z:+.3f} fresh={fresh_status} spread={spread_cents:.0f}c mins={mins_left:.1f}"
        ),
        "key_signals": [
            f"ev:{ev:.3f}", f"fair:{fair_p_yes:.3f}", f"side:{side}",
            f"z:{z:+.3f}", f"mkt_edge:{market_edge:.3f}",
        ],
        "signals": _signals(),
        "win_prob": float(model_p_side), "mom_label": side, "mom_pct": 0.0,
        "vel_signal": "fv_disloc",
        "raw_p_yes": _raw_p_yes,
        "mins_left": mins_left, "abs_pct": abs_pct, "above": side == "yes",
        "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
        "strategy_variant": "strategy2", "strategy_version": bot_state._S2_VERSION,
    }
