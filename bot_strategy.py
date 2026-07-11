"""bot_strategy.py - S1 (momentum / continuation) and S2 (favorite-bias harvest) brains.

The two brains are deliberately OPPOSITE bets so their head-to-head net-$ is meaningful:
  S1 MOMENTUM   - a fresh, confirmed spot move continues through settlement; buy the
                  moving side while it is still mid-priced (room to run). Trades WITH
                  the move, does not shrink to the market mid.
  S2 FAVORITE   - the market underprices proven favorites late in the window (documented
                  longshot bias); buy the 70-88c favorite once the spot is decisively
                  past the strike. High hit-rate, low payout - the mirror risk profile.
In paper mode both evaluate and trade every market (the duel), so per-strategy P&L
answers the one question that matters: does 15-min crypto trend, or does it pay to
harvest near-certain favorites?
"""
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


def _live_sigma_15m(asset: str, window_minutes: float = _VOL_WINDOW_MIN,
                    with_valid: bool = False):
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

    with_valid=True returns (sigma, valid) where valid=False means the static
    fallback was used (thin/degenerate data). Callers that compare live vol against
    an independent anchor (the S7/S8 regime gate) must check validity: a fallback
    value divided by a differing anchor produces a fake regime ratio.
    """
    def _out(sigma: float, valid: bool):
        return (sigma, valid) if with_valid else sigma

    base = _ASSET_VOL_15M.get(asset, 0.0023)
    static = base * _time_of_day_vol_multiplier()
    raw = asset_manager._prices.get(asset)
    if not raw or len(raw) < 3:
        return _out(static, False)
    pts = [(ts, p) for ts, p in raw if p > 0]
    if len(pts) < 3:
        return _out(static, False)
    now = pts[-1][0]
    if (pts[-1][0] - pts[0][0]) < _MIN_SPAN_SEC:
        return _out(static, False)
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
        return _out(static, False)
    var_per_sec = sum_r2 / sum_dt
    if var_per_sec <= 0:
        return _out(static, False)
    sigma_15m = math.sqrt(var_per_sec * 900.0)
    return _out(max(_FLOOR_MULT * base, min(_CEIL_MULT * base, sigma_15m)), True)


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


# Per-asset config for S1 MOMENTUM. lookback = window over which the spot move is
# measured (s); min_sigma = the move must be >= this many 1-sigma window moves (a real
# move, not noise); drift_lambda = fraction of the observed move projected forward when
# pricing the continuation fair value; confirm_ticks = recent-sign confirmation prints;
# min_entry/max_entry = price band (want room to run - skip rich favorites and longshots);
# min_edge = required EV over the ask after fee; time_min/max in minutes-left.
_S1_MOM_CONFIG: dict = {
    "BTC":  dict(lookback=75.0, min_sigma=1.0, drift_lambda=0.5, confirm_ticks=2,
                 min_entry=30.0, max_entry=75.0, min_edge=0.03, time_min=3.0, time_max=10.0),
    "ETH":  dict(lookback=75.0, min_sigma=1.0, drift_lambda=0.5, confirm_ticks=2,
                 min_entry=30.0, max_entry=75.0, min_edge=0.03, time_min=3.0, time_max=10.0),
    "SOL":  dict(lookback=75.0, min_sigma=1.0, drift_lambda=0.5, confirm_ticks=2,
                 min_entry=30.0, max_entry=75.0, min_edge=0.03, time_min=3.0, time_max=10.0),
    "XRP":  dict(lookback=75.0, min_sigma=1.0, drift_lambda=0.5, confirm_ticks=2,
                 min_entry=30.0, max_entry=75.0, min_edge=0.03, time_min=3.0, time_max=10.0),
    "DOGE": dict(lookback=75.0, min_sigma=1.1, drift_lambda=0.5, confirm_ticks=2,
                 min_entry=30.0, max_entry=75.0, min_edge=0.03, time_min=3.0, time_max=10.0),
}

# Per-asset config for S2 FAVORITE-BIAS. min_z = spot must sit this many period-sigmas
# past the strike (a proven favorite, not a coin-flip); mid_lo/mid_hi = the favorite
# side's de-vigged mid must land in this band (the premium lives at 70-88c; below is a
# toss-up, above is fee-eaten certainty); confirm_ticks = spot-sign prints; max_spread =
# round-trip book width cap; max_shortfall = only skip if our own Bachelier prices the
# favorite this far BELOW its ask (a trap guard - we do NOT require a model edge, since
# the thesis is realized win-rate > price, not a fair-value disagreement); time_min/max
# minutes-left (fire LATE, when the favorite is proven).
_S2_FAV_CONFIG: dict = {
    "BTC":  dict(min_z=0.8, mid_lo=0.70, mid_hi=0.88, confirm_ticks=2, max_spread_cents=7.0,
                 max_shortfall=0.08, min_entry=65.0, max_entry=90.0, time_min=2.5, time_max=6.0),
    "ETH":  dict(min_z=0.8, mid_lo=0.70, mid_hi=0.88, confirm_ticks=2, max_spread_cents=7.0,
                 max_shortfall=0.08, min_entry=65.0, max_entry=90.0, time_min=2.5, time_max=6.0),
    "SOL":  dict(min_z=0.8, mid_lo=0.70, mid_hi=0.88, confirm_ticks=2, max_spread_cents=7.0,
                 max_shortfall=0.08, min_entry=65.0, max_entry=90.0, time_min=2.5, time_max=6.0),
    "XRP":  dict(min_z=0.8, mid_lo=0.70, mid_hi=0.88, confirm_ticks=2, max_spread_cents=7.0,
                 max_shortfall=0.08, min_entry=65.0, max_entry=90.0, time_min=2.5, time_max=6.0),
    "DOGE": dict(min_z=0.8, mid_lo=0.70, mid_hi=0.88, confirm_ticks=2, max_spread_cents=8.0,
                 max_shortfall=0.08, min_entry=65.0, max_entry=90.0, time_min=2.5, time_max=6.0),
}

# Entry-price band fallback (both brains override per-asset). sub-30c/over-90c handled
# by the per-strategy bands above; these remain for get_asset_config defaults elsewhere.
_FV_MIN_ENTRY_CENTS = 20.0
_FV_MAX_ENTRY_CENTS = 85.0


def _s1_cfg(asset: str, config: dict) -> dict:
    """
    Merged S1 momentum config: per-asset defaults <- config['s1_config'][asset] <- flat
    global keys (dashboard-tunable, applied to every asset when present).
    """
    cfg = {**_S1_MOM_CONFIG.get(asset, _S1_MOM_CONFIG["SOL"]),
           **(config.get("s1_config", {}) or {}).get(asset, {})}
    _flat = {
        "lookback": config.get("s1_momentum_lookback_secs"),
        "min_sigma": config.get("s1_momentum_min_sigma"),
        "drift_lambda": config.get("s1_momentum_drift_lambda"),
        "confirm_ticks": config.get("s1_confirm_ticks"),
        "time_min": config.get("s1_time_min"),
        "time_max": config.get("s1_time_max"),
        "min_entry": config.get("s1_min_entry_cents"),
        "max_entry": config.get("s1_max_entry_cents"),
        "min_edge": config.get("s1_min_edge"),
    }
    cfg.update({k: v for k, v in _flat.items() if v is not None})
    return cfg


def _s2_cfg(asset: str, config: dict) -> dict:
    """
    Merged S2 favorite-bias config: per-asset defaults <- config['s2_config'][asset] <-
    flat global keys (dashboard-tunable, applied to every asset when present).
    """
    cfg = {**_S2_FAV_CONFIG.get(asset, _S2_FAV_CONFIG["BTC"]),
           **(config.get("s2_config", {}) or {}).get(asset, {})}
    _flat = {
        "min_z": config.get("s2_fav_min_z"),
        "mid_lo": config.get("s2_fav_mid_lo"),
        "mid_hi": config.get("s2_fav_mid_hi"),
        "confirm_ticks": config.get("s2_fav_confirm_ticks"),
        "time_min": config.get("s2_fav_time_min"),
        "time_max": config.get("s2_fav_time_max"),
        "min_entry": config.get("s2_fav_min_entry_cents"),
        "max_entry": config.get("s2_fav_max_entry_cents"),
        "max_shortfall": config.get("s2_fav_max_model_shortfall"),
    }
    cfg.update({k: v for k, v in _flat.items() if v is not None})
    return cfg


def _momentum_signal(asset: str, sigma_eff: float, cfg: dict) -> dict:
    """
    Fresh-move momentum signal for S1. A move counts only when it is real (>= min_sigma
    window-sigmas) AND still underway (a shorter-horizon sub-window agrees in sign - not
    already reversing). `cfg` is the already-merged per-asset config (see _s1_cfg).
    Returns a dict:
      {"ok": bool, "reason": str, "side": "yes"/"no"/None, "r": float, "min_move": float,
       "lookback": float}
    Never raises; thin/degenerate data returns ok=False with a reason. `side` is the side
    the spot just moved toward (up -> yes, down -> no) - S1 buys continuation of it.
    """
    lb = float(cfg["lookback"])
    min_sig = float(cfg["min_sigma"])
    confirm_ticks = int(cfg["confirm_ticks"])
    out = {"ok": False, "reason": "s1_no_data", "side": None, "r": 0.0,
           "min_move": 0.0, "lookback": lb}
    try:
        dq = asset_manager._prices.get(asset)
        pts = [(ts, p) for ts, p in dq if p > 0] if dq else []
        if len(pts) < confirm_ticks + 2:
            return out
        now_ts = pts[-1][0]
        spot_now = pts[-1][1]
        anchor = _nearest_price(pts, now_ts - lb)
        if anchor is None or anchor[1] <= 0:
            out["reason"] = "s1_no_anchor"
            return out
        age = now_ts - anchor[0]
        if not (0.5 * lb <= age <= 1.5 * lb):
            out["reason"] = "s1_thin_window"
            return out
        r = math.log(spot_now / anchor[1])
        sigma_window = sigma_eff * math.sqrt(max(1e-6, lb / 900.0))
        min_move = min_sig * sigma_window
        out.update({"r": r, "min_move": min_move})
        if abs(r) < min_move:
            out["reason"] = f"s1_mom_flat:{r:+.4f}<{min_move:.4f}"
            return out
        side = "yes" if r > 0 else "no"
        out["side"] = side
        # Confirmation: a shorter sub-window (lookback/3) must move the SAME way - the
        # trend is still going, not already snapping back.
        micro = _nearest_price(pts, now_ts - lb / 3.0)
        if micro is None or micro[1] <= 0:
            out["reason"] = "s1_no_confirm"
            return out
        micro_r = math.log(spot_now / micro[1])
        confirm = (micro_r > 0) if side == "yes" else (micro_r < 0)
        if not confirm:
            out["reason"] = f"s1_no_confirm:micro={micro_r:+.4f}"
            return out
        out["ok"] = True
        out["reason"] = ""
        return out
    except Exception:
        out["reason"] = "s1_signal_error"
        return out


def strategy_brain_s1(
    btc_price, strike, yes_ask, no_ask,
    elapsed_seconds, secs_left, ticker,
    asset: str = "BTC",
) -> dict:
    """
    S1: MOMENTUM / CONTINUATION.

    A fresh, confirmed spot move continues through the 15-min settle more often than the
    book prices in. Measure the spot return over the lookback; if it is a real move
    (>= min_sigma window-sigmas) still underway, buy the side it moved toward, pricing a
    Bachelier fair value on the spot projected forward by drift_lambda * move. Trades WITH
    the move and does NOT shrink to the market mid - the opposite stance to a fade brain.
    BTC-lead is a logged confirming input, not the thesis. BTC/ETH off by default
    (re-enable via s1_btc_enabled / s1_eth_enabled).
    """
    config = read_config()
    cfg = _s1_cfg(asset, config)
    mins_left = secs_left / 60.0

    # Resolve the alt (traded-asset) spot - callers pass it as the first arg.
    if asset == "BTC":
        current_price = btc_price
    else:
        raw = asset_manager._prices.get(asset)
        current_price = raw[-1][1] if raw else btc_price
    abs_pct = abs(current_price - strike) / strike if strike > 0 else 0.0

    # Asset scope: BTC/ETH off by default (kept for parity with the loop's enabled set;
    # momentum itself is asset-agnostic, so these are pure enable flags now).
    if asset in ("BTC", "ETH"):
        _flag = "s1_btc_enabled" if asset == "BTC" else "s1_eth_enabled"
        if not config.get(_flag, False):
            return _make_skip("yes", f"s1_disabled:{asset}", abs_pct, mins_left, variant="strategy1")

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

    # Resolve spot + sigma, then the fresh-move momentum signal.
    if current_price <= 0 or strike <= 0:
        return _make_skip("yes", "s1_bad_price", abs_pct, mins_left, variant="strategy1")
    sigma_eff = _sigma_eff(asset, config)
    mom = _momentum_signal(asset, sigma_eff, cfg)
    if not mom["ok"]:
        return _make_skip("yes", mom["reason"], abs_pct, mins_left, variant="strategy1")
    side = mom["side"]
    r = float(mom["r"])

    # BTC-lead as a logged confirming input only (not the thesis, not a hard gate unless
    # s1_require_btc_confirm is set): does BTC's recent move agree with our direction?
    beta = _asset_beta(asset, config)
    btc_ret = 0.0
    try:
        _btc_dq = asset_manager._prices.get("BTC")
        _blist = list(_btc_dq) if _btc_dq else []
        if len(_blist) >= 2:
            _b_then = _nearest_price(_blist, _blist[-1][0] - float(mom["lookback"]))
            if _b_then and _b_then[1] > 0 and _blist[-1][1] > 0:
                btc_ret = math.log(_blist[-1][1] / _b_then[1])
    except Exception:
        btc_ret = 0.0
    btc_lead = beta * btc_ret
    btc_agree = (btc_lead > 0) if side == "yes" else (btc_lead < 0)

    # Continuation fair value: project the spot forward by drift_lambda * move and price a
    # Bachelier digital on it. Projecting toward the move makes S1 systematically more
    # confident on the moving side than the market -> it BUYS continuation.
    lam = float(cfg["drift_lambda"])
    eff_secs = _effective_secs(secs_left, config)
    predicted_spot = _basis_adjusted_spot(current_price * math.exp(lam * r), asset)
    fair_p_yes = _bachelier_p_above(predicted_spot, strike, eff_secs, sigma_eff)
    fair_p_yes = _calibrated_p(fair_p_yes, "strategy1", config)
    _ps = sigma_eff * math.sqrt(max(1.0 / 900.0, eff_secs / 900.0))
    z = math.log(predicted_spot / strike) / _ps if (strike > 0 and _ps > 0) else 0.0
    z_raw = z * _applied_sigma_scale(asset)

    # Direction is decided by momentum, NOT by the cheap-side picker: trade the moving
    # side against its own ask. EV is measured against the price actually paid.
    _raw_p_yes = float(fair_p_yes)
    model_p_side = _raw_p_yes if side == "yes" else 1.0 - _raw_p_yes
    entry_price = yes_ask if side == "yes" else no_ask
    _entry_p = float(entry_price) / 100.0
    _fee_rate = config.get("kalshi_fee_per_contract_cents", 7) / 100.0
    fee = _kalshi_fee_frac(_entry_p, _fee_rate)
    ev = model_p_side - _entry_p - fee
    mid_yes = _market_implied_p_yes(yes_ask, no_ask)
    mkt_p_side = (mid_yes if side == "yes" else 1.0 - mid_yes) if mid_yes is not None else None
    market_edge = (model_p_side - mkt_p_side) if mkt_p_side is not None else None

    def _signals():
        return {
            "win_prob": model_p_side, "ev": ev, "market_edge": market_edge,
            "mkt_p": mkt_p_side, "model_raw_p_yes": _raw_p_yes,
            "r": r, "min_move": mom.get("min_move"), "drift_lambda": lam,
            "btc_ret": btc_ret, "beta": beta, "btc_agree": btc_agree,
            "predicted_spot": predicted_spot,
            "sigma_eff": sigma_eff, "z": z, "z_raw": z_raw, "spot": current_price,
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
            "mom_pct": float(r), "vel_signal": "momentum",
            "raw_p_yes": _raw_p_yes,
            "mins_left": mins_left, "abs_pct": abs_pct, "above": side == "yes",
            "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
            "strategy_variant": "strategy1",
        }

    # Optional hard BTC-confirmation gate (default off): only trade when BTC's move agrees.
    if config.get("s1_require_btc_confirm", False) and not btc_agree:
        return _full_skip(
            f"s1_btc_disagree:btc_lead={btc_lead:+.4f}|side={side}",
            [f"btc_lead:{btc_lead:+.4f}", f"side:{side}"],
        )

    # Entry-price band: want room to run - skip already-rich favorites and longshots.
    _min_p = float(get_asset_config(config, asset, "s1_min_entry_cents", cfg["min_entry"]))
    _max_p = float(get_asset_config(config, asset, "s1_max_entry_cents", cfg["max_entry"]))
    if entry_price < _min_p or entry_price > _max_p:
        return _make_skip(side, f"s1_price_filter:{entry_price:.0f}c", abs_pct, mins_left,
                          variant="strategy1", price_filter=True)

    # EV gate: the continuation model must beat the ask by min_edge after fee.
    _min_edge = float(cfg.get("min_edge", config.get("s1_min_edge", 0.03)))
    if ev < _min_edge:
        return _full_skip(
            f"s1_ev_gate:ev={ev:.3f}<{_min_edge:.3f}",
            [f"ev:{ev:.3f}", f"fair:{fair_p_yes:.3f}", f"r:{r:+.4f}"],
        )

    brain_log.info(
        "S1 MOMENTUM %s %s | side=%s r=%+.4f fair=%.3f ev=%.3f btc_agree=%s mins=%.1f",
        asset, ticker, side, r, fair_p_yes, ev, btc_agree, mins_left,
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
            f"s1_momentum ev={ev:.3f} fair={fair_p_yes:.3f} side={side} "
            f"r={r:+.4f} btc_agree={btc_agree} mins={mins_left:.1f}"
        ),
        "key_signals": [
            f"ev:{ev:.3f}", f"fair:{fair_p_yes:.3f}", f"side:{side}",
            f"r:{r:+.4f}", f"btc_agree:{btc_agree}",
        ],
        "signals": _signals(),
        "win_prob": float(model_p_side), "mom_label": side,
        "mom_pct": float(r), "vel_signal": "momentum",
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
    S2: FAVORITE-BIAS HARVEST.

    The market underprices proven favorites late in the window (documented longshot bias,
    visible in this bot's own log). Once the spot sits decisively past the strike (|z| large)
    with the last prints confirming it, buy the FAVORITE side while its de-vigged mid is in
    the premium band (0.70-0.88). High hit-rate, low payout - the mirror of S1. The edge is
    realized win-rate > price, so there is NO fair-value-disagreement gate; a light guard
    only skips favorites our own model strongly rejects. ETH off by default (s2_eth_enabled).
    """
    config = read_config()
    cfg = _s2_cfg(asset, config)
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

    # Conviction: the spot must sit decisively past the strike (a proven favorite).
    sigma_eff = _sigma_eff(asset, config)
    eff_secs = _effective_secs(secs_left, config)
    adj_price = _basis_adjusted_spot(current_price, asset)
    period_sigma = sigma_eff * math.sqrt(max(1.0 / 900.0, eff_secs / 900.0))
    if period_sigma <= _SIGMA_MIN_PERIOD:
        return _make_skip("yes", "s2_degenerate_sigma", abs_pct, mins_left, variant="strategy2")
    z = math.log(adj_price / strike) / period_sigma
    z_raw = z * _applied_sigma_scale(asset)
    if abs(z) < float(cfg["min_z"]):
        return _make_skip("yes", f"s2_lowz:{z:+.3f}<{cfg['min_z']}", abs_pct, mins_left, variant="strategy2")

    # The favorite side is the side the spot is on; require the last prints to confirm it.
    side = "yes" if z > 0 else "no"
    want_above = z > 0
    if not _spot_confirm(spot_dq, strike, want_above, int(cfg["confirm_ticks"])):
        return _make_skip("yes", f"s2_flicker:want_above={want_above}", abs_pct, mins_left, variant="strategy2")

    # Round-trip spread cap (an edge cannot survive a wide book).
    spread_cents = yes_ask + no_ask - 100.0
    if spread_cents > float(cfg["max_spread_cents"]):
        return _make_skip("yes", f"s2_wide_spread:{spread_cents:.0f}c>{cfg['max_spread_cents']:.0f}c",
                          abs_pct, mins_left, variant="strategy2")

    mid_yes = _market_implied_p_yes(yes_ask, no_ask)
    if mid_yes is None:
        return _make_skip("yes", "s2_no_market_data", abs_pct, mins_left, variant="strategy2")
    p_fav = mid_yes if side == "yes" else 1.0 - mid_yes   # the favorite side's de-vigged mid

    fair_p_yes = _bachelier_p_above(adj_price, strike, eff_secs, sigma_eff)
    fair_p_yes = _calibrated_p(fair_p_yes, "strategy2", config)
    _raw_p_yes = float(fair_p_yes)
    model_p_side = _raw_p_yes if side == "yes" else 1.0 - _raw_p_yes
    entry_price = yes_ask if side == "yes" else no_ask
    _entry_p = float(entry_price) / 100.0
    _fee_rate = config.get("kalshi_fee_per_contract_cents", 7) / 100.0
    fee = _kalshi_fee_frac(_entry_p, _fee_rate)
    ev = model_p_side - _entry_p - fee
    market_edge = model_p_side - p_fav

    def _signals():
        return {
            "win_prob": model_p_side, "ev": ev, "market_edge": market_edge,
            "mkt_p": p_fav, "model_raw_p_yes": _raw_p_yes, "p_fav": p_fav,
            "z": z, "z_raw": z_raw, "sigma_eff": sigma_eff, "spot": adj_price,
            "spread_cents": spread_cents,
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
            "vel_signal": "favorite",
            "raw_p_yes": _raw_p_yes,
            "mins_left": mins_left, "abs_pct": abs_pct, "above": side == "yes",
            "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
            "strategy_variant": "strategy2", "strategy_version": bot_state._S2_VERSION,
        }

    # Premium band: the favorite's de-vigged mid must land in [mid_lo, mid_hi]. Below is a
    # toss-up (no reliable favorite); above is a fee-eaten near-certainty.
    mid_lo = float(cfg["mid_lo"]); mid_hi = float(cfg["mid_hi"])
    if p_fav < mid_lo:
        return _full_skip(f"s2_not_favorite:mid={p_fav:.2f}<{mid_lo:.2f}",
                          [f"p_fav:{p_fav:.2f}", f"lo:{mid_lo:.2f}"])
    if p_fav > mid_hi:
        return _full_skip(f"s2_too_certain:mid={p_fav:.2f}>{mid_hi:.2f}",
                          [f"p_fav:{p_fav:.2f}", f"hi:{mid_hi:.2f}"])

    # Entry-price band (per-asset configurable).
    _min_p = float(get_asset_config(config, asset, "s2_fav_min_entry_cents", cfg["min_entry"]))
    _max_p = float(get_asset_config(config, asset, "s2_fav_max_entry_cents", cfg["max_entry"]))
    if entry_price < _min_p or entry_price > _max_p:
        return _make_skip(side, f"s2_price_filter:{entry_price:.0f}c", abs_pct, mins_left,
                          variant="strategy2", price_filter=True)

    # Trap guard: skip only if our own model prices the favorite well BELOW its ask (we do
    # NOT require a positive model edge - the thesis is realized win-rate > price).
    _max_shortfall = float(cfg.get("max_shortfall", config.get("s2_fav_max_model_shortfall", 0.08)))
    if model_p_side < _entry_p - _max_shortfall:
        return _full_skip(
            f"s2_model_reject:model={model_p_side:.2f}<ask{_entry_p:.2f}-{_max_shortfall:.2f}",
            [f"model:{model_p_side:.2f}", f"ask:{_entry_p:.2f}", f"z:{z:+.3f}"],
        )

    brain_log.info(
        "S2 FAVORITE %s %s | side=%s z=%+.3f p_fav=%.2f fair=%.3f ev=%.3f spread=%.0fc mins=%.1f",
        asset, ticker, side, z, p_fav, fair_p_yes, ev, spread_cents, mins_left,
    )
    # Auto-gate (GATE-1 per bucket): checked last so the decision still reaches the harness
    # (logged with would_trade=0) while the bucket is blocked.
    if config.get("auto_gate_enabled", True) and ("strategy2", asset) in bot_state._auto_blocked_assets:
        return _full_skip(f"s2_auto_gate:{asset}",
                          [f"auto_gate:{asset}", f"p_fav:{p_fav:.2f}"])
    return {
        "action": "trade", "side": side,
        "confidence": int(model_p_side * 100),
        "reasoning": (
            f"s2_favorite p_fav={p_fav:.2f} fair={fair_p_yes:.3f} side={side} "
            f"z={z:+.3f} ev={ev:.3f} spread={spread_cents:.0f}c mins={mins_left:.1f}"
        ),
        "key_signals": [
            f"p_fav:{p_fav:.2f}", f"fair:{fair_p_yes:.3f}", f"side:{side}",
            f"z:{z:+.3f}", f"ev:{ev:.3f}",
        ],
        "signals": _signals(),
        "win_prob": float(model_p_side), "mom_label": side, "mom_pct": 0.0,
        "vel_signal": "favorite",
        "raw_p_yes": _raw_p_yes,
        "mins_left": mins_left, "abs_pct": abs_pct, "above": side == "yes",
        "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
        "strategy_variant": "strategy2", "strategy_version": bot_state._S2_VERSION,
    }


# Test-slot strategies (S3-S6). Paper-only lab slots dispatched via the
# bot_strategies.STRATEGY_REGISTRY: each is a thesis this bot has never traded, run in
# parallel with S1/S2 so the per-strategy scoreboard can pick a winner from real settled
# data. All share the S1/S2 decision-dict contract; execution goes through the generic
# slot executor in bot_risk (hard-forced paper).


def _slot_trade(variant, version, side, model_p_side, raw_p_yes, ev, reasoning, keys,
                signals, mins_left, abs_pct, extra=None):
    """Standard trade-decision dict for a test-slot brain (S1/S2 contract shape)."""
    out = {
        "action": "trade", "side": side,
        "confidence": int(max(0.0, min(1.0, model_p_side)) * 100),
        "reasoning": reasoning, "key_signals": keys, "signals": signals,
        "win_prob": float(model_p_side), "mom_label": side, "mom_pct": 0.0,
        "vel_signal": variant, "raw_p_yes": raw_p_yes,
        "mins_left": mins_left, "abs_pct": abs_pct, "above": side == "yes",
        "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
        "strategy_variant": variant, "strategy_version": version,
    }
    if extra:
        out.update(extra)
    return out


def strategy_brain_s3_arb(
    btc_price, strike, yes_ask, no_ask,
    elapsed_seconds, secs_left, ticker,
    asset: str = "BTC",
) -> dict:
    """
    S3: STRUCTURAL ARBITRAGE (paper lab slot).

    When YES_ask + NO_ask < threshold, buying BOTH sides guarantees a profit: one side
    always settles at $1.00, so net = 100 - yes - no - fees > 0 regardless of direction.
    Zero directional risk - this measures how often Kalshi books dislocate that far and
    what a scanner would earn. The executor writes two paper trade rows (yes + no).
    """
    config = read_config()
    mins_left = secs_left / 60.0
    abs_pct = abs(btc_price - strike) / strike if strike > 0 else 0.0
    if not config.get("s3_arb_enabled", True):
        return _make_skip("yes", "s3_disabled", abs_pct, mins_left, variant="strategy3")
    # No quiet-hours/session gates: the bet is direction-free. Final 90s still blocked
    # (fill reliability collapses into the settlement auction).
    if secs_left < 90:
        return _make_skip("yes", f"s3_time_gate:{mins_left:.1f}min", abs_pct, mins_left, variant="strategy3")
    _fee_cents = float(config.get("kalshi_fee_per_contract_cents", 7))
    _threshold = float(config.get("s3_arb_max_combined_cents", 93.0))
    arb = check_dual_side_arb(yes_ask, no_ask, _fee_cents, _threshold)
    if not arb["arb"]:
        return _make_skip("yes", f"s3_no_arb:combined={arb['combined']:.0f}c", abs_pct,
                          mins_left, variant="strategy3")
    mid = _market_implied_p_yes(yes_ask, no_ask)
    signals = {
        "win_prob": 1.0, "ev": arb["net_edge_cents"] / 100.0, "market_edge": None,
        "mkt_p": mid, "model_raw_p_yes": mid if mid is not None else 0.5,
        "combined_cents": arb["combined"], "net_edge_cents": arb["net_edge_cents"],
        "abs_pct": abs_pct, "mins_left": mins_left, "strike": strike,
    }
    return _slot_trade(
        "strategy3", bot_state._SLOT_VERSIONS["strategy3"], "yes",
        1.0, mid if mid is not None else 0.5, arb["net_edge_cents"] / 100.0,
        f"s3_arb combined={arb['combined']:.0f}c net={arb['net_edge_cents']:.1f}c mins={mins_left:.1f}",
        [f"combined:{arb['combined']:.0f}c", f"net:{arb['net_edge_cents']:.1f}c"],
        signals, mins_left, abs_pct,
        extra={"arb_both_sides": True, "confidence": 99},
    )


def strategy_brain_s4_revert(
    btc_price, strike, yes_ask, no_ask,
    elapsed_seconds, secs_left, ticker,
    asset: str = "BTC",
) -> dict:
    """
    S4: MEAN-REVERSION (paper lab slot) - the exact opposite bet to S1 momentum.

    When the spot ran >= s4_min_sigma window-sigmas over the lookback AND the most
    recent sub-window shows the move stalling or turning, buy the OPPOSITE side,
    betting the move snaps back before settlement. S1-vs-S4 on the same tape is the
    definitive does-15-min-crypto-trend-or-revert experiment.
    """
    config = read_config()
    mins_left = secs_left / 60.0
    dq = asset_manager._prices.get(asset)
    pts = [(ts, p) for ts, p in dq if p > 0] if dq else []
    spot = pts[-1][1] if pts else btc_price
    abs_pct = abs(spot - strike) / strike if strike > 0 else 0.0
    if not config.get("s4_revert_enabled", True):
        return _make_skip("yes", "s4_disabled", abs_pct, mins_left, variant="strategy4")
    if _is_quiet_hours(config):
        return _make_skip("yes", "s4_quiet_hours", abs_pct, mins_left, variant="strategy4")
    _t_min = float(config.get("s4_time_min", 3.0)); _t_max = float(config.get("s4_time_max", 10.0))
    if mins_left < _t_min or mins_left > _t_max:
        return _make_skip("yes", f"s4_time_gate:{mins_left:.1f}min", abs_pct, mins_left, variant="strategy4")
    if spot <= 0 or strike <= 0 or len(pts) < 5:
        return _make_skip("yes", "s4_no_data", abs_pct, mins_left, variant="strategy4")

    lb = float(config.get("s4_lookback_secs", 120.0))
    now_ts = pts[-1][0]
    anchor = _nearest_price(pts, now_ts - lb)
    if anchor is None or anchor[1] <= 0:
        return _make_skip("yes", "s4_no_anchor", abs_pct, mins_left, variant="strategy4")
    age = now_ts - anchor[0]
    if not (0.5 * lb <= age <= 1.5 * lb):
        return _make_skip("yes", "s4_thin_window", abs_pct, mins_left, variant="strategy4")
    r = math.log(spot / anchor[1])
    sigma_eff = _sigma_eff(asset, config)
    sigma_window = sigma_eff * math.sqrt(max(1e-6, lb / 900.0))
    min_move = float(config.get("s4_min_sigma", 2.0)) * sigma_window
    if abs(r) < min_move:
        return _make_skip("yes", f"s4_not_extended:{r:+.4f}<{min_move:.4f}", abs_pct,
                          mins_left, variant="strategy4")
    # Stall filter: the last third of the window must NOT still be running with the move
    # (that is S1's setup, not ours). Stalled = micro move opposite or < 20% of the move's
    # pro-rata pace.
    micro = _nearest_price(pts, now_ts - lb / 3.0)
    if micro is None or micro[1] <= 0:
        return _make_skip("yes", "s4_no_micro", abs_pct, mins_left, variant="strategy4")
    micro_r = math.log(spot / micro[1])
    still_running = (micro_r * r > 0) and (abs(micro_r) > 0.2 * abs(r) / 3.0)
    if still_running:
        return _make_skip("yes", f"s4_still_running:micro={micro_r:+.4f}", abs_pct,
                          mins_left, variant="strategy4")

    # Fade the move: side is OPPOSITE the run direction; fair value prices the spot
    # snapping back by s4_revert_lambda of the move.
    side = "no" if r > 0 else "yes"
    lam = float(config.get("s4_revert_lambda", 0.5))
    eff_secs = _effective_secs(secs_left, config)
    predicted_spot = _basis_adjusted_spot(spot * math.exp(-lam * r), asset)
    fair_p_yes = _bachelier_p_above(predicted_spot, strike, eff_secs, sigma_eff)
    _raw_p_yes = float(fair_p_yes)
    model_p_side = _raw_p_yes if side == "yes" else 1.0 - _raw_p_yes
    entry_price = yes_ask if side == "yes" else no_ask
    _entry_p = float(entry_price) / 100.0
    _fee_rate = config.get("kalshi_fee_per_contract_cents", 7) / 100.0
    ev = model_p_side - _entry_p - _kalshi_fee_frac(_entry_p, _fee_rate)

    _min_p = float(config.get("s4_min_entry_cents", 25.0))
    _max_p = float(config.get("s4_max_entry_cents", 70.0))
    if entry_price < _min_p or entry_price > _max_p:
        return _make_skip(side, f"s4_price_filter:{entry_price:.0f}c", abs_pct, mins_left,
                          variant="strategy4", price_filter=True)
    signals = {
        "win_prob": model_p_side, "ev": ev, "market_edge": None,
        "mkt_p": _market_implied_p_yes(yes_ask, no_ask),
        "model_raw_p_yes": _raw_p_yes, "r": r, "micro_r": micro_r,
        "min_move": min_move, "sigma_eff": sigma_eff, "spot": spot,
        "abs_pct": abs_pct, "mins_left": mins_left, "strike": strike,
    }
    _min_edge = float(config.get("s4_min_edge", 0.03))
    if ev < _min_edge:
        return {
            "action": "skip", "side": side, "confidence": int(model_p_side * 100),
            "reasoning": f"s4_ev_gate:ev={ev:.3f}<{_min_edge:.3f}",
            "key_signals": [f"ev:{ev:.3f}", f"r:{r:+.4f}"], "signals": signals,
            "win_prob": float(model_p_side), "mom_label": side, "mom_pct": float(r),
            "vel_signal": "strategy4", "raw_p_yes": _raw_p_yes,
            "mins_left": mins_left, "abs_pct": abs_pct, "above": side == "yes",
            "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
            "strategy_variant": "strategy4",
        }
    brain_log.info("S4 REVERT %s %s | side=%s r=%+.4f ev=%.3f mins=%.1f",
                   asset, ticker, side, r, ev, mins_left)
    return _slot_trade(
        "strategy4", bot_state._SLOT_VERSIONS["strategy4"], side,
        model_p_side, _raw_p_yes, ev,
        f"s4_revert r={r:+.4f} fair={fair_p_yes:.3f} side={side} ev={ev:.3f} mins={mins_left:.1f}",
        [f"r:{r:+.4f}", f"fair:{fair_p_yes:.3f}", f"side:{side}", f"ev:{ev:.3f}"],
        signals, mins_left, abs_pct,
    )


def strategy_brain_s5_maker(
    btc_price, strike, yes_ask, no_ask,
    elapsed_seconds, secs_left, ticker,
    asset: str = "BTC",
) -> dict:
    """
    S5: MAKER SPREAD-CAPTURE (paper lab slot) - profit from execution, not prediction.

    On a proven favorite (mid in the s5 band, spot confirming), post a passive quote
    s5_improve_cents inside the entry-side ask instead of paying it. Settlement uses the
    held-book path (bot_state._maker_track) to decide whether the quote filled: filled ->
    maker price + maker fee against the real outcome (adverse selection captured);
    unfilled -> $0 no-trade. Fill rate is half the experiment.
    """
    config = read_config()
    mins_left = secs_left / 60.0
    dq = asset_manager._prices.get(asset)
    spot_dq = list(dq) if dq else []
    spot = spot_dq[-1][1] if spot_dq else btc_price
    abs_pct = abs(spot - strike) / strike if strike > 0 else 0.0
    if not config.get("s5_maker_enabled", True):
        return _make_skip("yes", "s5_disabled", abs_pct, mins_left, variant="strategy5")
    if _is_quiet_hours(config):
        return _make_skip("yes", "s5_quiet_hours", abs_pct, mins_left, variant="strategy5")
    _t_min = float(config.get("s5_time_min", 3.0)); _t_max = float(config.get("s5_time_max", 9.0))
    if mins_left < _t_min or mins_left > _t_max:
        return _make_skip("yes", f"s5_time_gate:{mins_left:.1f}min", abs_pct, mins_left, variant="strategy5")
    if spot <= 0 or strike <= 0:
        return _make_skip("yes", "s5_bad_price", abs_pct, mins_left, variant="strategy5")
    mid_yes = _market_implied_p_yes(yes_ask, no_ask)
    if mid_yes is None:
        return _make_skip("yes", "s5_no_market_data", abs_pct, mins_left, variant="strategy5")
    side = "yes" if mid_yes >= 0.5 else "no"        # quote on the favorite side
    p_fav = mid_yes if side == "yes" else 1.0 - mid_yes
    lo = float(config.get("s5_mid_lo", 0.60)); hi = float(config.get("s5_mid_hi", 0.90))
    if not (lo <= p_fav <= hi):
        return _make_skip("yes", f"s5_band:mid={p_fav:.2f}", abs_pct, mins_left, variant="strategy5")
    want_above = side == "yes"
    if not _spot_confirm(spot_dq, strike, want_above, 2):
        return _make_skip("yes", f"s5_flicker:want_above={want_above}", abs_pct, mins_left, variant="strategy5")
    ask = yes_ask if side == "yes" else no_ask
    improve = float(config.get("s5_improve_cents", 1.0))
    maker_price = max(1.0, float(ask) - improve)
    sigma_eff = _sigma_eff(asset, config)
    eff_secs = _effective_secs(secs_left, config)
    fair_p_yes = _bachelier_p_above(_basis_adjusted_spot(spot, asset), strike, eff_secs, sigma_eff)
    _raw_p_yes = float(fair_p_yes)
    model_p_side = _raw_p_yes if side == "yes" else 1.0 - _raw_p_yes
    signals = {
        "win_prob": model_p_side, "ev": None, "market_edge": None,
        "mkt_p": p_fav, "model_raw_p_yes": _raw_p_yes,
        "maker_price_cents": maker_price, "ask_cents": float(ask),
        "sigma_eff": sigma_eff, "spot": spot,
        "abs_pct": abs_pct, "mins_left": mins_left, "strike": strike,
    }
    brain_log.info("S5 MAKER %s %s | side=%s quote=%dc (ask %dc) p_fav=%.2f mins=%.1f",
                   asset, ticker, side, int(maker_price), int(ask), p_fav, mins_left)
    return _slot_trade(
        "strategy5", bot_state._SLOT_VERSIONS["strategy5"], side,
        model_p_side, _raw_p_yes, 0.0,
        f"s5_maker quote={maker_price:.0f}c ask={ask:.0f}c side={side} p_fav={p_fav:.2f} mins={mins_left:.1f}",
        [f"quote:{maker_price:.0f}c", f"ask:{ask:.0f}c", f"side:{side}", f"p_fav:{p_fav:.2f}"],
        signals, mins_left, abs_pct,
        extra={"maker_quote_cents": maker_price},
    )


def strategy_brain_s6_carry(
    btc_price, strike, yes_ask, no_ask,
    elapsed_seconds, secs_left, ticker,
    asset: str = "BTC",
) -> dict:
    """
    S6: WINDOW-FADE (paper lab slot) - cross-window mean reversion at the open.

    In the first s6_window_secs of a new window (book still near 50c), buy the side
    OPPOSITE the previous window's resolved direction, provided that window moved
    decisively. Backed by scripts/backtest_carry.py + scripts/tune_fade.py over 25k
    real Kalshi settlements (Feb-Apr 2026, 4 assets): windows anti-persist, and the
    fade rate is monotonic in previous-move size AND streak length. The tuned gate
    (move >= 15bp and streak >= 2) fades 56.4% (Wilson-LB 0.552 vs ~0.517 breakeven
    at 50c, ~+4.6c/contract after fees, n=5275). Gates mirror the backtest exactly:
    early entry, decisive previous move, streak >= 2, near-coin-flip price band -
    no spot-side conditioning (the measured edge is unconditional at the open).
    """
    config = read_config()
    mins_left = secs_left / 60.0
    dq = asset_manager._prices.get(asset)
    spot_dq = list(dq) if dq else []
    spot = spot_dq[-1][1] if spot_dq else btc_price
    abs_pct = abs(spot - strike) / strike if strike > 0 else 0.0
    if not config.get("s6_carry_enabled", True):
        return _make_skip("yes", "s6_disabled", abs_pct, mins_left, variant="strategy6")
    if _is_quiet_hours(config):
        return _make_skip("yes", "s6_quiet_hours", abs_pct, mins_left, variant="strategy6")
    if elapsed_seconds > float(config.get("s6_window_secs", 120.0)):
        return _make_skip("yes", f"s6_too_late:{elapsed_seconds:.0f}s", abs_pct, mins_left, variant="strategy6")
    # 1100s bound = one window + grace. The rollover estimate refreshes this every
    # window, so anything older means a missed write - never fade a 2-window-old move.
    prev = bot_state._prev_window_outcome.get(asset)
    if not prev or (time.time() - float(prev.get("ts", 0))) > 1100.0:
        return _make_skip("yes", "s6_no_prev_window", abs_pct, mins_left, variant="strategy6")
    if prev.get("result") not in ("yes", "no"):
        return _make_skip("yes", "s6_prev_unresolved", abs_pct, mins_left, variant="strategy6")
    # Conditional gates from scripts/tune_fade.py (25k settlement pairs): the fade rate
    # is monotonic in BOTH previous-move size and streak length. move>=15bp AND
    # streak>=2 -> fade 56.4% (Wilson-LB 0.552, +4.6c/ct at 50c) vs 53.4% unconditional,
    # while still passing ~30 live trades/day across three assets.
    _prev_strike = float(prev.get("strike") or 0.0)
    _prev_close = float(prev.get("spot_at_close") or 0.0)
    _min_prev = float(config.get("s6_min_prev_move", 0.0015))
    if _prev_strike <= 0 or _prev_close <= 0 or abs(_prev_close / _prev_strike - 1.0) < _min_prev:
        return _make_skip("yes", "s6_prev_too_close", abs_pct, mins_left, variant="strategy6")
    _min_streak = int(config.get("s6_min_streak", 2))
    if int(prev.get("streak", 1)) < _min_streak:
        return _make_skip("yes", f"s6_short_streak:{prev.get('streak', 1)}<{_min_streak}",
                          abs_pct, mins_left, variant="strategy6")
    side = "no" if prev["result"] == "yes" else "yes"   # FADE the resolved direction
    entry_price = yes_ask if side == "yes" else no_ask
    _min_p = float(config.get("s6_min_entry_cents", 40.0))
    _max_p = float(config.get("s6_max_entry_cents", 60.0))
    if entry_price < _min_p or entry_price > _max_p:
        return _make_skip(side, f"s6_price_filter:{entry_price:.0f}c", abs_pct, mins_left,
                          variant="strategy6", price_filter=True)
    mid_yes = _market_implied_p_yes(yes_ask, no_ask)
    mkt_p_side = (mid_yes if side == "yes" else 1.0 - mid_yes) if mid_yes is not None else 0.5
    # Fade prior: the historically measured premium over the market mid on the fade
    # side (0.534 - 0.5 from the settlement backtest). Live data proves or buries it.
    fade_premium = float(config.get("s6_fade_premium", 0.064))
    model_p_side = min(0.95, mkt_p_side + fade_premium)
    _raw_p_yes = model_p_side if side == "yes" else 1.0 - model_p_side
    _entry_p = float(entry_price) / 100.0
    _fee_rate = config.get("kalshi_fee_per_contract_cents", 7) / 100.0
    ev = model_p_side - _entry_p - _kalshi_fee_frac(_entry_p, _fee_rate)
    signals = {
        "win_prob": model_p_side, "ev": ev, "market_edge": None,
        "mkt_p": mkt_p_side, "model_raw_p_yes": _raw_p_yes,
        "prev_result": prev["result"], "prev_move": abs(_prev_close / _prev_strike - 1.0),
        "elapsed": elapsed_seconds, "spot": spot,
        "abs_pct": abs_pct, "mins_left": mins_left, "strike": strike,
    }
    if ev < float(config.get("s6_min_edge", 0.01)):
        return {
            "action": "skip", "side": side, "confidence": int(model_p_side * 100),
            "reasoning": f"s6_ev_gate:ev={ev:.3f}",
            "key_signals": [f"ev:{ev:.3f}"], "signals": signals,
            "win_prob": float(model_p_side), "mom_label": side, "mom_pct": 0.0,
            "vel_signal": "strategy6", "raw_p_yes": _raw_p_yes,
            "mins_left": mins_left, "abs_pct": abs_pct, "above": side == "yes",
            "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
            "strategy_variant": "strategy6",
        }
    brain_log.info("S6 FADE %s %s | side=%s prev=%s entry=%dc mins=%.1f",
                   asset, ticker, side, prev["result"], int(entry_price), mins_left)
    return _slot_trade(
        "strategy6", bot_state._SLOT_VERSIONS["strategy6"], side,
        model_p_side, _raw_p_yes, ev,
        f"s6_fade prev={prev['result']} side={side} entry={entry_price:.0f}c ev={ev:.3f} mins={mins_left:.1f}",
        [f"prev:{prev['result']}", f"side:{side}", f"entry:{entry_price:.0f}c", f"ev:{ev:.3f}"],
        signals, mins_left, abs_pct,
    )


def _vol_regime(asset: str, config: dict) -> tuple:
    """
    (live_sigma, anchor_sigma, ratio) for the S7/S8 regime gates. The anchor is the
    market-implied EWMA when fresh (the market's own opinion of normal), else the
    static table x time-of-day - deliberately NOT _sigma_eff, whose clamps would hide
    exactly the live-vs-anchor divergence these strategies trade. Never raises;
    degenerate data returns ratio 1.0 (no regime signal -> both brains skip).

    Requires a GENUINE live estimate: when _live_sigma_15m falls back to its static
    table (thin price deque), ratio is forced to 1.0 - otherwise a static fallback
    divided by a differing implied-EWMA anchor would cross the spike/calm thresholds
    with zero actual vol information behind it. Note the estimator's 0.5x-base floor
    clamp means S8's calm gate (ratio <= 0.6) in practice triggers on live vol pegged
    at/near half of normal - that is intended: "calm" = the floor of what we can
    measure, not a finer gradation below it.
    """
    try:
        live, live_ok = _live_sigma_15m(asset, with_valid=True)
        imp = bot_state._implied_sigma.get(asset)
        max_age = float(config.get("sigma_implied_max_age_secs", 900) or 900)
        fresh = (isinstance(imp, dict) and imp.get("sigma")
                 and int(imp.get("n", 0)) >= 3
                 and (time.time() - float(imp.get("ts", 0.0))) <= max_age)
        anchor = float(imp["sigma"]) if fresh else (
            _ASSET_VOL_15M.get(asset, 0.0023) * _time_of_day_vol_multiplier())
        if not live_ok or anchor <= 0 or live <= 0:
            return live, anchor, 1.0
        return live, anchor, live / anchor
    except Exception:
        return 0.0, 0.0, 1.0


def strategy_brain_s7_volspike(
    btc_price, strike, yes_ask, no_ask,
    elapsed_seconds, secs_left, ticker,
    asset: str = "BTC",
) -> dict:
    """
    S7: VOL-SPIKE BREAKOUT (paper lab slot) - trade only when volatility is abnormal.

    Every other strategy here is vol-regime-blind. S7 fires ONLY when live realized
    vol runs >= s7_spike_ratio x its own anchor (the market-implied EWMA / static
    normal): in a spike, far strikes are genuinely reachable while the book reprices
    with a lag, so the moving side is underpriced. Buy the direction of the fresh move,
    fair-valued with the LIVE (spiked) sigma. S8 is the mirror (calm regime).
    """
    config = read_config()
    mins_left = secs_left / 60.0
    dq = asset_manager._prices.get(asset)
    pts = [(ts, p) for ts, p in dq if p > 0] if dq else []
    spot = pts[-1][1] if pts else btc_price
    abs_pct = abs(spot - strike) / strike if strike > 0 else 0.0
    if not config.get("s7_volspike_enabled", True):
        return _make_skip("yes", "s7_disabled", abs_pct, mins_left, variant="strategy7")
    if _is_quiet_hours(config):
        return _make_skip("yes", "s7_quiet_hours", abs_pct, mins_left, variant="strategy7")
    _t_min = float(config.get("s7_time_min", 4.0)); _t_max = float(config.get("s7_time_max", 10.0))
    if mins_left < _t_min or mins_left > _t_max:
        return _make_skip("yes", f"s7_time_gate:{mins_left:.1f}min", abs_pct, mins_left, variant="strategy7")
    if spot <= 0 or strike <= 0 or len(pts) < 5:
        return _make_skip("yes", "s7_no_data", abs_pct, mins_left, variant="strategy7")

    live, anchor, ratio = _vol_regime(asset, config)
    _spike = float(config.get("s7_spike_ratio", 1.6))
    if ratio < _spike:
        return _make_skip("yes", f"s7_no_spike:ratio={ratio:.2f}<{_spike:.2f}", abs_pct,
                          mins_left, variant="strategy7")

    # Direction of the fresh move inside the spike.
    lb = float(config.get("s7_lookback_secs", 60.0))
    anchor_pt = _nearest_price(pts, pts[-1][0] - lb)
    if anchor_pt is None or anchor_pt[1] <= 0:
        return _make_skip("yes", "s7_no_anchor", abs_pct, mins_left, variant="strategy7")
    r = math.log(spot / anchor_pt[1])
    sigma_window = live * math.sqrt(max(1e-6, lb / 900.0))
    if abs(r) < 0.25 * sigma_window:
        return _make_skip("yes", f"s7_no_direction:{r:+.4f}", abs_pct, mins_left, variant="strategy7")
    side = "yes" if r > 0 else "no"

    entry_price = yes_ask if side == "yes" else no_ask
    _min_p = float(config.get("s7_min_entry_cents", 30.0))
    _max_p = float(config.get("s7_max_entry_cents", 70.0))
    if entry_price < _min_p or entry_price > _max_p:
        return _make_skip(side, f"s7_price_filter:{entry_price:.0f}c", abs_pct, mins_left,
                          variant="strategy7", price_filter=True)

    # Fair value with the LIVE sigma - the whole thesis is that the book still prices
    # the calm anchor while realized vol has left it behind.
    eff_secs = _effective_secs(secs_left, config)
    fair_p_yes = _bachelier_p_above(_basis_adjusted_spot(spot, asset), strike, eff_secs, live)
    _raw_p_yes = float(fair_p_yes)
    model_p_side = _raw_p_yes if side == "yes" else 1.0 - _raw_p_yes
    _entry_p = float(entry_price) / 100.0
    _fee_rate = config.get("kalshi_fee_per_contract_cents", 7) / 100.0
    ev = model_p_side - _entry_p - _kalshi_fee_frac(_entry_p, _fee_rate)
    mid_yes = _market_implied_p_yes(yes_ask, no_ask)
    signals = {
        "win_prob": model_p_side, "ev": ev, "market_edge": None,
        "mkt_p": (mid_yes if side == "yes" else 1.0 - mid_yes) if mid_yes is not None else None,
        "model_raw_p_yes": _raw_p_yes, "vol_ratio": ratio, "live_sigma": live,
        "anchor_sigma": anchor, "r": r, "spot": spot,
        "abs_pct": abs_pct, "mins_left": mins_left, "strike": strike,
    }
    if ev < float(config.get("s7_min_edge", 0.03)):
        return {
            "action": "skip", "side": side, "confidence": int(model_p_side * 100),
            "reasoning": f"s7_ev_gate:ev={ev:.3f}",
            "key_signals": [f"ev:{ev:.3f}", f"ratio:{ratio:.2f}"], "signals": signals,
            "win_prob": float(model_p_side), "mom_label": side, "mom_pct": float(r),
            "vel_signal": "strategy7", "raw_p_yes": _raw_p_yes,
            "mins_left": mins_left, "abs_pct": abs_pct, "above": side == "yes",
            "_rv": None, "_vol_ratio": ratio, "price_filter_skip": False,
            "strategy_variant": "strategy7",
        }
    brain_log.info("S7 VOLSPIKE %s %s | side=%s ratio=%.2f r=%+.4f ev=%.3f mins=%.1f",
                   asset, ticker, side, ratio, r, ev, mins_left)
    return _slot_trade(
        "strategy7", bot_state._SLOT_VERSIONS["strategy7"], side,
        model_p_side, _raw_p_yes, ev,
        f"s7_volspike ratio={ratio:.2f} side={side} r={r:+.4f} ev={ev:.3f} mins={mins_left:.1f}",
        [f"ratio:{ratio:.2f}", f"side:{side}", f"r:{r:+.4f}", f"ev:{ev:.3f}"],
        signals, mins_left, abs_pct,
    )


def strategy_brain_s8_calm(
    btc_price, strike, yes_ask, no_ask,
    elapsed_seconds, secs_left, ticker,
    asset: str = "BTC",
) -> dict:
    """
    S8: CALM-MARKET FAVORITE (paper lab slot) - the mirror regime to S7.

    Fires ONLY when realized vol has COLLAPSED below s8_calm_ratio x its anchor: on a
    dead tape the current side of the strike holds more often than a normal-vol book
    implies, so the favorite is underpriced. Buys the favorite (mid 0.55-0.85), fair-
    valued with the LIVE (collapsed) sigma. Overlaps S2's favorite-bias thesis by
    construction, but is regime-conditional, earlier (4-10 min vs 2.5-6), and needs
    far less strike distance (min_z 0.4 vs 0.8) - the edge claim is the vol regime,
    not the favorite premium.
    """
    config = read_config()
    mins_left = secs_left / 60.0
    dq = asset_manager._prices.get(asset)
    pts = [(ts, p) for ts, p in dq if p > 0] if dq else []
    spot = pts[-1][1] if pts else btc_price
    abs_pct = abs(spot - strike) / strike if strike > 0 else 0.0
    if not config.get("s8_calm_enabled", True):
        return _make_skip("yes", "s8_disabled", abs_pct, mins_left, variant="strategy8")
    if _is_quiet_hours(config):
        return _make_skip("yes", "s8_quiet_hours", abs_pct, mins_left, variant="strategy8")
    _t_min = float(config.get("s8_time_min", 4.0)); _t_max = float(config.get("s8_time_max", 10.0))
    if mins_left < _t_min or mins_left > _t_max:
        return _make_skip("yes", f"s8_time_gate:{mins_left:.1f}min", abs_pct, mins_left, variant="strategy8")
    if spot <= 0 or strike <= 0 or len(pts) < 5:
        return _make_skip("yes", "s8_no_data", abs_pct, mins_left, variant="strategy8")

    live, anchor, ratio = _vol_regime(asset, config)
    _calm = float(config.get("s8_calm_ratio", 0.6))
    if ratio > _calm:
        return _make_skip("yes", f"s8_not_calm:ratio={ratio:.2f}>{_calm:.2f}", abs_pct,
                          mins_left, variant="strategy8")

    mid_yes = _market_implied_p_yes(yes_ask, no_ask)
    if mid_yes is None:
        return _make_skip("yes", "s8_no_market_data", abs_pct, mins_left, variant="strategy8")
    side = "yes" if mid_yes >= 0.5 else "no"
    p_fav = mid_yes if side == "yes" else 1.0 - mid_yes
    lo = float(config.get("s8_mid_lo", 0.55)); hi = float(config.get("s8_mid_hi", 0.85))
    if not (lo <= p_fav <= hi):
        return _make_skip("yes", f"s8_band:mid={p_fav:.2f}", abs_pct, mins_left, variant="strategy8")

    # Modest conviction with the COLLAPSED sigma - the regime does the heavy lifting.
    eff_secs = _effective_secs(secs_left, config)
    period_sigma = live * math.sqrt(max(1.0 / 900.0, eff_secs / 900.0))
    if period_sigma <= _SIGMA_MIN_PERIOD:
        return _make_skip("yes", "s8_degenerate_sigma", abs_pct, mins_left, variant="strategy8")
    adj = _basis_adjusted_spot(spot, asset)
    z = math.log(adj / strike) / period_sigma
    want_yes = z > 0
    if (side == "yes") != want_yes or abs(z) < float(config.get("s8_min_z", 0.4)):
        return _make_skip("yes", f"s8_lowz:{z:+.3f}", abs_pct, mins_left, variant="strategy8")

    entry_price = yes_ask if side == "yes" else no_ask
    _min_p = float(config.get("s8_min_entry_cents", 50.0))
    _max_p = float(config.get("s8_max_entry_cents", 88.0))
    if entry_price < _min_p or entry_price > _max_p:
        return _make_skip(side, f"s8_price_filter:{entry_price:.0f}c", abs_pct, mins_left,
                          variant="strategy8", price_filter=True)

    fair_p_yes = _bachelier_p_above(adj, strike, eff_secs, live)
    _raw_p_yes = float(fair_p_yes)
    model_p_side = _raw_p_yes if side == "yes" else 1.0 - _raw_p_yes
    _entry_p = float(entry_price) / 100.0
    _fee_rate = config.get("kalshi_fee_per_contract_cents", 7) / 100.0
    ev = model_p_side - _entry_p - _kalshi_fee_frac(_entry_p, _fee_rate)
    signals = {
        "win_prob": model_p_side, "ev": ev, "market_edge": None,
        "mkt_p": p_fav, "model_raw_p_yes": _raw_p_yes, "vol_ratio": ratio,
        "live_sigma": live, "anchor_sigma": anchor, "z": z, "spot": spot,
        "abs_pct": abs_pct, "mins_left": mins_left, "strike": strike,
    }
    if ev < float(config.get("s8_min_edge", 0.03)):
        return {
            "action": "skip", "side": side, "confidence": int(model_p_side * 100),
            "reasoning": f"s8_ev_gate:ev={ev:.3f}",
            "key_signals": [f"ev:{ev:.3f}", f"ratio:{ratio:.2f}"], "signals": signals,
            "win_prob": float(model_p_side), "mom_label": side, "mom_pct": 0.0,
            "vel_signal": "strategy8", "raw_p_yes": _raw_p_yes,
            "mins_left": mins_left, "abs_pct": abs_pct, "above": side == "yes",
            "_rv": None, "_vol_ratio": ratio, "price_filter_skip": False,
            "strategy_variant": "strategy8",
        }
    brain_log.info("S8 CALM %s %s | side=%s ratio=%.2f z=%+.2f ev=%.3f mins=%.1f",
                   asset, ticker, side, ratio, z, ev, mins_left)
    return _slot_trade(
        "strategy8", bot_state._SLOT_VERSIONS["strategy8"], side,
        model_p_side, _raw_p_yes, ev,
        f"s8_calm ratio={ratio:.2f} side={side} z={z:+.2f} ev={ev:.3f} mins={mins_left:.1f}",
        [f"ratio:{ratio:.2f}", f"side:{side}", f"z:{z:+.2f}", f"ev:{ev:.3f}"],
        signals, mins_left, abs_pct,
    )
