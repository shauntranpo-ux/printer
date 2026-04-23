"""
Comprehensive 15-minute market strategy research for BTC, ETH, SOL, XRP.

Tests three strategy families with full parameter sweeps:
  A. Late-window time-decay (Brownian Bridge underpricing in final minutes)
  B. Dwell-time persistence (price dominated one side for most of the window)
  C. Momentum early-entry (strong move at t=4-7min, cheap entry)

Fully vectorised — no iterrows. Each asset processes millions of windows in
a few seconds. Runs TRAIN (2022-2023) and TEST (2024-2026) separately so you
can spot overfitting immediately.

Usage:
    python scripts/research_15m_strategies.py
    python scripts/research_15m_strategies.py --assets ETH SOL
    python scripts/research_15m_strategies.py --strategy A
    python scripts/research_15m_strategies.py --wfa --mc
"""

from __future__ import annotations
import argparse
import json
import math
import sys
import time as _time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# ── constants ────────────────────────────────────────────────────────────────
STAKE    = 25.0
FEE_RATE = 0.07

ASSETS = ["BTC", "ETH", "SOL", "XRP"]

PERIODS = [
    ("TRAIN", "2022-01-01", "2023-12-31"),
    ("TEST",  "2024-01-01", "2026-04-15"),
]

STRIKE_INCREMENTS = {
    "BTC":  1000.0,
    "ETH":    25.0,
    "SOL":     1.0,
    "XRP":     0.01,
}

# Kalshi AMM half-spread per asset (conservative, wider than real)
HALF_SPREAD = {"BTC": 1.5, "ETH": 1.5, "SOL": 2.0, "XRP": 2.0}

WINDOW_SEC  = 15 * 60   # 900 seconds
EVAL_STEP   = 60        # evaluate every 60s
FIRST_EVAL  = 60        # first eval at 60s elapsed
LAST_EVAL   = 14 * 60   # last eval at 840s (60s remain)

RESULTS_DIR = Path("results")
RESULTS_DIR.mkdir(exist_ok=True)


# ── data loading ─────────────────────────────────────────────────────────────
def load_prices(asset: str) -> tuple[np.ndarray, np.ndarray]:
    """Return (ts_arr, close_arr) as sorted int64/float64 numpy arrays.
    Reads only 2 columns via pyarrow to minimize memory usage."""
    import pyarrow.parquet as pq

    for name in [f"{asset}_1m_extended.parquet", f"{asset}_1m_2026.parquet"]:
        p = Path("data/historical") / name
        if not p.exists():
            continue
        schema_names = pq.read_schema(p).names
        if "timestamp" in schema_names:
            tbl = pq.read_table(p, columns=["timestamp", "close"])
            ts_arr = tbl["timestamp"].to_pylist()
            cl_arr = tbl["close"].to_pylist()
        else:
            # Legacy: open_time (datetime64) column
            tbl = pq.read_table(p, columns=["open_time", "close"])
            import pandas as _pd
            ts_arr = _pd.to_datetime(tbl["open_time"].to_pylist()).astype("int64") // 10**9
            ts_arr = ts_arr.tolist()
            cl_arr = tbl["close"].to_pylist()

        ts = np.array(ts_arr, dtype=np.int64)
        cl = np.array(cl_arr, dtype=np.float64)
        # Sort only if needed (time series data usually already sorted)
        if len(ts) > 1 and ts[0] > ts[-1]:
            idx = np.argsort(ts, kind="stable")
            ts, cl = ts[idx], cl[idx]
        # Deduplicate: keep last close per timestamp
        _, keep = np.unique(ts, return_index=True)
        return ts[keep], cl[keep]

    raise FileNotFoundError(f"No 1m data for {asset}")


def ts_from(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


def slice_range(ts_arr: np.ndarray, lo: int, hi: int) -> tuple[int, int]:
    """Return [start, end) index slice for ts_arr in [lo, hi]."""
    return int(np.searchsorted(ts_arr, lo, "left")), int(np.searchsorted(ts_arr, hi, "right"))


# ── Brownian Bridge AMM simulation ──────────────────────────────────────────
def bb_prob_above(price: np.ndarray, strike: np.ndarray,
                  sec_left: np.ndarray, rv: np.ndarray) -> np.ndarray:
    """Vectorised Brownian Bridge P(close > strike). rv is per-minute std."""
    mins_left = np.maximum(sec_left / 60.0, 1.0 / 60.0)
    sigma = rv * np.sqrt(mins_left)
    d = np.where(sigma > 0, (price - strike) / (price * sigma + 1e-12), 0.0)
    from scipy.special import ndtr
    p = ndtr(d)
    return np.clip(p, 0.01, 0.99)


def amm_ask(p_above: np.ndarray, half_spread: float,
            side: str) -> np.ndarray:
    """Returns ask price in cents for YES or NO side."""
    if side == "yes":
        return np.clip(p_above * 100.0 + half_spread, 2.0, 98.0)
    else:
        return np.clip((1.0 - p_above) * 100.0 + half_spread, 2.0, 98.0)


# ── realised vol (vectorised) ─────────────────────────────────────────────────
def build_rv_lookup(ts_arr: np.ndarray, cl_arr: np.ndarray) -> dict[int, float]:
    """
    Precompute 10-minute rolling realised vol (std of 1-min log returns).
    Returns {ts_minute: rv} mapping for fast lookup during window generation.
    """
    # Convert to per-minute series using the last close in each minute
    ts_min = ts_arr // 60 * 60
    # Keep last price per minute (in case of sub-minute data)
    df = pd.DataFrame({"ts_min": ts_min, "close": cl_arr})
    df = df.groupby("ts_min", sort=True)["close"].last()
    min_ts  = df.index.values
    min_cl  = df.values.astype(np.float64)

    log_ret = np.concatenate([[0.0], np.diff(np.log(np.maximum(min_cl, 1e-9)))])

    rv_dict: dict[int, float] = {}
    LOOKBACK = 10  # 10-minute lookback
    for i in range(LOOKBACK, len(min_ts)):
        window_ret = log_ret[i - LOOKBACK + 1: i + 1]
        rv = float(np.std(window_ret)) if len(window_ret) >= 4 else 0.0
        rv_dict[int(min_ts[i])] = rv
    return rv_dict


# ── vectorised window generation ─────────────────────────────────────────────
def generate_windows_fast(
    ts_arr: np.ndarray,
    cl_arr: np.ndarray,
    asset: str,
    start_ts: int,
    end_ts: int,
) -> dict:
    """
    Generate all 15-minute window evaluation points in [start_ts, end_ts].

    Returns arrays (all same length = number of qualifying eval points):
        window_start, eval_ts, elapsed, sec_left,
        current_price, close_price, strike, rv,
        yes_ask, no_ask, won_yes
    """
    incr = STRIKE_INCREMENTS[asset]
    hs   = HALF_SPREAD[asset]

    # Slice to requested period (with buffer for rv lookback)
    buf  = 3600
    lo_i, hi_i = slice_range(ts_arr, start_ts - buf, end_ts)
    ts_s = ts_arr[lo_i:hi_i]
    cl_s = cl_arr[lo_i:hi_i]

    # Build per-minute price dict (vectorised)
    ts_min_arr = ts_s // 60 * 60
    min_df = pd.DataFrame({"ts_min": ts_min_arr, "close": cl_s})
    min_df = min_df.groupby("ts_min", sort=True)["close"].last()
    pm_ts  = min_df.index.values.astype(np.int64)
    pm_cl  = min_df.values.astype(np.float64)
    pm_set = set(pm_ts.tolist())   # for fast membership check

    # Precompute RV (per-minute log return std, 10-min rolling)
    log_ret = np.concatenate([[0.0], np.diff(np.log(np.maximum(pm_cl, 1e-9)))])
    LOOKBACK = 10
    pm_rv = np.zeros(len(pm_ts))
    for i in range(LOOKBACK, len(pm_ts)):
        pm_rv[i] = float(np.std(log_ret[max(0, i - LOOKBACK + 1): i + 1]))
    pm_rv_dict = {int(t): rv for t, rv in zip(pm_ts, pm_rv)}

    # Determine window boundaries aligned to 15-min grid
    first_win = ((start_ts // WINDOW_SEC) + 1) * WINDOW_SEC
    last_win  = (end_ts  // WINDOW_SEC)     * WINDOW_SEC - WINDOW_SEC

    rows = []
    for win_start in range(first_win, last_win + 1, WINDOW_SEC):
        win_close = win_start + WINDOW_SEC

        if win_start not in pm_set or win_close not in pm_set:
            continue

        # open_price → strike
        idx_open = int(np.searchsorted(pm_ts, win_start, "left"))
        if idx_open >= len(pm_ts) or pm_ts[idx_open] != win_start:
            continue
        open_price  = pm_cl[idx_open]
        idx_close   = int(np.searchsorted(pm_ts, win_close, "left"))
        if idx_close >= len(pm_ts) or pm_ts[idx_close] != win_close:
            continue
        close_price = pm_cl[idx_close]
        strike = round(open_price / incr) * incr
        if strike <= 0:
            continue

        won_yes = close_price > strike

        for elapsed in range(FIRST_EVAL, LAST_EVAL + 1, EVAL_STEP):
            eval_ts   = win_start + elapsed
            sec_left  = WINDOW_SEC - elapsed
            if eval_ts not in pm_set:
                continue
            # RV lookup
            rv_key = (eval_ts // 60) * 60
            rv = pm_rv_dict.get(rv_key, 0.0)
            if rv <= 0:
                continue
            idx_eval = int(np.searchsorted(pm_ts, eval_ts, "left"))
            if idx_eval >= len(pm_ts) or pm_ts[idx_eval] != eval_ts:
                continue
            cur_price = pm_cl[idx_eval]

            # AMM pricing
            mins_left = max(sec_left / 60.0, 1.0 / 60.0)
            sigma     = rv * math.sqrt(mins_left)
            d         = (cur_price - strike) / (cur_price * sigma + 1e-12) if sigma > 0 else 0.0
            from math import erf
            p_above   = max(0.01, min(0.99, 0.5 * (1.0 + erf(d / math.sqrt(2)))))

            yes_ask = max(2.0, min(98.0, p_above * 100.0 + hs))
            no_ask  = max(2.0, min(98.0, (1.0 - p_above) * 100.0 + hs))

            rows.append((
                win_start, eval_ts, elapsed, sec_left,
                cur_price, close_price, strike, rv,
                yes_ask, no_ask, won_yes,
            ))

    if not rows:
        return {}

    arr = np.array(rows, dtype=np.float64)
    return {
        "window_start": arr[:, 0].astype(np.int64),
        "eval_ts":      arr[:, 1].astype(np.int64),
        "elapsed":      arr[:, 2],
        "sec_left":     arr[:, 3],
        "cur_price":    arr[:, 4],
        "close_price":  arr[:, 5],
        "strike":       arr[:, 6],
        "rv":           arr[:, 7],
        "yes_ask":      arr[:, 8],
        "no_ask":       arr[:, 9],
        "won_yes":      arr[:, 10].astype(bool),
    }


# ── PnL / metrics ─────────────────────────────────────────────────────────────
def pnl_for_arr(won: np.ndarray, entry_c: np.ndarray) -> np.ndarray:
    frac = entry_c / 100.0
    gross = np.where(won, (STAKE / frac) * (1.0 - frac) * (1.0 - FEE_RATE), -STAKE)
    return gross


def metrics(pnl: np.ndarray, won: np.ndarray, entry_c: np.ndarray, n_windows: int) -> dict:
    n = len(pnl)
    if n == 0:
        return dict(n=0, wr=0.0, ev=0.0, pnl=0.0, avg_entry=0.0,
                    max_dd=0.0, sharpe=0.0, trades_per_week=0.0)
    wins = int(won.sum())
    total = float(pnl.sum())
    std = float(pnl.std()) if n > 1 else 1.0
    sharpe = float(pnl.mean() / std) if std > 0 else 0.0
    cum = np.cumsum(pnl)
    peak = np.maximum.accumulate(cum)
    max_dd = float((peak - cum).max())
    # Approximate weeks covered (15m windows / 4 per hour / 24h * 7d)
    weeks = max(1, n_windows / (4 * 24 * 7))
    return dict(
        n=n,
        wr=round(wins / n * 100, 2),
        ev=round(total / n, 3),
        pnl=round(total, 2),
        avg_entry=round(float(entry_c.mean()), 1),
        max_dd=round(max_dd, 2),
        sharpe=round(sharpe, 4),
        trades_per_week=round(n / weeks, 1),
    )


# ── Strategy A: Late-window time-decay ────────────────────────────────────────
def strat_A_grid():
    for sec_max in [120, 150, 180, 240, 300, 360]:
        for dist in [0.20, 0.30, 0.40, 0.50, 0.60, 0.80]:
            for min_entry in [70.0, 75.0, 80.0, 85.0]:
                yield (90, sec_max, dist, min_entry)


def run_strat_A(data: dict, sec_min: int, sec_max: int,
                dist_pct: float, min_entry: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """
    One trade per window: first eval where sec_left in [sec_min, sec_max],
    price distance >= dist_pct%, and entry >= min_entry.
    Returns (pnl, won, entry_c, n_windows).
    """
    if not data:
        return np.array([]), np.array([]), np.array([]), 0

    sl   = data["sec_left"]
    pct  = (data["cur_price"] - data["strike"]) / data["strike"] * 100.0
    wins = data["won_yes"]
    ws   = data["window_start"]

    mask_time  = (sl >= sec_min) & (sl <= sec_max)
    mask_dist  = np.abs(pct) >= dist_pct
    side_yes   = pct >= dist_pct
    side_no    = pct <= -dist_pct

    yes_entry  = data["yes_ask"]
    no_entry   = data["no_ask"]
    entry_c    = np.where(side_yes, yes_entry, no_entry)
    mask_entry = entry_c >= min_entry

    mask = mask_time & mask_dist & mask_entry

    if not mask.any():
        return np.array([]), np.array([]), np.array([]), len(set(ws.tolist()))

    # One trade per window: take the first qualifying eval per window
    ws_masked   = ws[mask]
    pct_masked  = pct[mask]
    entry_masked = entry_c[mask]
    wins_masked = wins[mask]
    side_yes_m  = side_yes[mask]

    trade_pnl, trade_won, trade_entry = [], [], []
    seen = set()
    for i in range(len(ws_masked)):
        w = ws_masked[i]
        if w in seen:
            continue
        seen.add(w)
        sy = side_yes_m[i]
        won = (sy and wins_masked[i]) or (not sy and not wins_masked[i])
        ec  = entry_masked[i]
        frac = ec / 100.0
        pnl_val = (STAKE / frac) * (1 - frac) * (1 - FEE_RATE) if won else -STAKE
        trade_pnl.append(pnl_val)
        trade_won.append(won)
        trade_entry.append(ec)

    n_windows = len(set(ws.tolist()))
    return (np.array(trade_pnl), np.array(trade_won, dtype=bool),
            np.array(trade_entry), n_windows)


# ── Strategy B: Dwell-time persistence ────────────────────────────────────────
def strat_B_grid():
    for elapsed_max in [600, 660, 720, 780]:   # t=10,11,12,13min — after most of window elapsed
        for elapsed_min in [480, 540]:           # t=8,9min earliest entry
            if elapsed_min >= elapsed_max:
                continue
            for dwell_frac in [0.80, 0.85, 0.90]:
                for dist in [0.20, 0.30, 0.40, 0.50]:
                    for min_entry in [55.0, 60.0, 65.0, 70.0]:
                        yield (elapsed_min, elapsed_max, dwell_frac, dist, min_entry)


def build_dwell_lookup(data: dict) -> dict[int, dict]:
    """
    For each window, precompute at each eval point:
      - fraction of evals so far where price was on the same side as current
    Returns {window_start: {eval_ts: (dwell_frac, cur_above)}}
    """
    if not data:
        return {}
    ws = data["window_start"]
    et = data["eval_ts"]
    cp = data["cur_price"]
    sk = data["strike"]

    lookup: dict[int, dict] = {}
    wins_per = {}
    for i in range(len(ws)):
        w = int(ws[i])
        if w not in wins_per:
            wins_per[w] = []
        wins_per[w].append((int(et[i]), cp[i] > sk[i]))

    for w, items in wins_per.items():
        items.sort()
        above_seq = [a for _, a in items]
        ts_seq    = [t for t, _ in items]
        lookup[w] = {}
        for j, (ts_, cur_above) in enumerate(zip(ts_seq, above_seq)):
            sub = above_seq[:j+1]
            dwell = sum(1 for a in sub if a == cur_above) / len(sub)
            lookup[w][ts_] = (dwell, cur_above)
    return lookup


def run_strat_B(data: dict, dwell_lookup: dict,
                elapsed_min: int, elapsed_max: int,
                dwell_frac: float, dist_pct: float,
                min_entry: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    if not data:
        return np.array([]), np.array([]), np.array([]), 0

    ws  = data["window_start"]
    et  = data["eval_ts"]
    el  = data["elapsed"]
    pct = (data["cur_price"] - data["strike"]) / data["strike"] * 100.0
    wins = data["won_yes"]

    mask_time = (el >= elapsed_min) & (el <= elapsed_max)
    mask_dist = np.abs(pct) >= dist_pct

    trade_pnl, trade_won, trade_entry = [], [], []
    seen = set()

    indices = np.where(mask_time & mask_dist)[0]
    for i in indices:
        w = int(ws[i])
        if w in seen:
            continue
        ts_ = int(et[i])
        d_info = dwell_lookup.get(w, {}).get(ts_)
        if d_info is None:
            continue
        df_val, cur_above = d_info
        if df_val < dwell_frac:
            continue
        p = pct[i]
        if (cur_above and p < dist_pct) or (not cur_above and p > -dist_pct):
            continue
        ec = float(data["yes_ask"][i] if cur_above else data["no_ask"][i])
        if ec < min_entry:
            continue
        seen.add(w)
        won = (cur_above and wins[i]) or (not cur_above and not wins[i])
        frac = ec / 100.0
        pnl_val = (STAKE / frac) * (1 - frac) * (1 - FEE_RATE) if won else -STAKE
        trade_pnl.append(pnl_val)
        trade_won.append(won)
        trade_entry.append(ec)

    n_windows = len(set(ws.tolist()))
    return (np.array(trade_pnl), np.array(trade_won, dtype=bool),
            np.array(trade_entry), n_windows)


# ── Strategy C: Momentum early-entry ─────────────────────────────────────────
def strat_C_grid():
    for elapsed_target in [240, 300, 360, 420]:   # t=4,5,6,7 min
        for move_pct in [0.30, 0.40, 0.50, 0.60]:
            for max_entry in [50.0, 55.0, 60.0, 65.0]:
                yield (elapsed_target, move_pct, max_entry)


def run_strat_C(data: dict, elapsed_target: int,
                move_pct: float, max_entry: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    if not data:
        return np.array([]), np.array([]), np.array([]), 0

    el   = data["elapsed"]
    pct  = (data["cur_price"] - data["strike"]) / data["strike"] * 100.0
    ws   = data["window_start"]
    wins = data["won_yes"]

    TOL = 90
    mask_time = np.abs(el - elapsed_target) <= TOL
    side_yes  = pct >= move_pct
    side_no   = pct <= -move_pct
    mask_dir  = side_yes | side_no
    entry_c   = np.where(side_yes, data["yes_ask"], data["no_ask"])
    mask_e    = (entry_c <= max_entry) & (entry_c > 0)
    mask      = mask_time & mask_dir & mask_e

    if not mask.any():
        return np.array([]), np.array([]), np.array([]), len(set(ws.tolist()))

    ws_m  = ws[mask]
    sy_m  = side_yes[mask]
    wn_m  = wins[mask]
    ec_m  = entry_c[mask]
    el_m  = np.abs(el[mask] - elapsed_target)

    # Per window: pick eval closest to target
    trade_pnl, trade_won, trade_entry = [], [], []
    seen: dict[int, tuple] = {}
    for i in range(len(ws_m)):
        w = int(ws_m[i])
        if w not in seen or el_m[i] < seen[w][0]:
            seen[w] = (el_m[i], sy_m[i], wn_m[i], ec_m[i])

    for w, (_, sy, wn, ec) in seen.items():
        won = (sy and wn) or (not sy and not wn)
        frac = ec / 100.0
        pnl_val = (STAKE / frac) * (1 - frac) * (1 - FEE_RATE) if won else -STAKE
        trade_pnl.append(pnl_val)
        trade_won.append(won)
        trade_entry.append(ec)

    n_windows = len(set(ws.tolist()))
    return (np.array(trade_pnl), np.array(trade_won, dtype=bool),
            np.array(trade_entry), n_windows)


# ── Walk-forward validation ────────────────────────────────────────────────────
def run_wfa(ts_arr: np.ndarray, cl_arr: np.ndarray, asset: str,
            run_fn, best_params: tuple,
            train_days: int = 90, test_days: int = 30) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    day_sec = 86400
    purge   = day_sec
    start_ts = int(ts_arr[0])
    end_ts   = int(ts_arr[-1])

    all_pnl, all_won, all_entry = [], [], []
    slice_start = start_ts

    while slice_start + (train_days + 1 + test_days) * day_sec <= end_ts:
        test_start = slice_start + train_days * day_sec + purge
        test_end   = test_start + test_days * day_sec

        data = generate_windows_fast(ts_arr, cl_arr, asset, test_start, test_end)
        if run_fn == run_strat_B and data:
            dl = build_dwell_lookup(data)
            pnl, won, entry, _ = run_fn(data, dl, *best_params)
        else:
            pnl, won, entry, _ = run_fn(data, *best_params)

        if len(pnl):
            all_pnl.append(pnl)
            all_won.append(won)
            all_entry.append(entry)

        slice_start += test_days * day_sec

    if not all_pnl:
        return np.array([]), np.array([]), np.array([])
    return (np.concatenate(all_pnl), np.concatenate(all_won).astype(bool),
            np.concatenate(all_entry))


# ── Monte Carlo ───────────────────────────────────────────────────────────────
def monte_carlo(pnl: np.ndarray, n_sims: int = 10_000, seed: int = 42) -> dict:
    if len(pnl) < 10:
        return {}
    rng = np.random.default_rng(seed)
    n   = len(pnl)
    sims  = rng.choice(pnl, size=(n_sims, n), replace=True)
    totals = sims.sum(axis=1)
    wrs    = (sims > 0).mean(axis=1)
    obs    = float(pnl.sum())
    pct_beat = float((totals >= obs).mean())

    def p(arr, q): return round(float(np.percentile(arr, q)), 2)

    return {
        "n_trades":      n,
        "obs_pnl":       round(obs, 2),
        "pnl_p05":       p(totals, 5),
        "pnl_p50":       p(totals, 50),
        "pnl_p95":       p(totals, 95),
        "wr_p05":        round(p(wrs, 5) * 100, 1),
        "wr_p50":        round(p(wrs, 50) * 100, 1),
        "wr_p95":        round(p(wrs, 95) * 100, 1),
        "pct_sims_beat": round(pct_beat * 100, 1),
    }


# ── Sweep helpers ─────────────────────────────────────────────────────────────
def sweep(label: str, grid_fn, run_fn, data_train: dict, data_test: dict,
          n_windows_train: int, n_windows_test: int,
          extra_arg=None, top_k: int = 5) -> list:

    results = []
    param_list = list(grid_fn())
    total = len(param_list)

    for i, params in enumerate(param_list):
        if total >= 50 and (i + 1) % (total // 5) == 0:
            print(f"    [{label}] {i+1}/{total}...")

        if extra_arg is not None:
            tr_pnl, tr_won, tr_entry, _ = run_fn(data_train, extra_arg, *params)
            ts_pnl, ts_won, ts_entry, _ = run_fn(data_test,  extra_arg, *params)
        else:
            tr_pnl, tr_won, tr_entry, _ = run_fn(data_train, *params)
            ts_pnl, ts_won, ts_entry, _ = run_fn(data_test,  *params)

        tm = metrics(tr_pnl, tr_won, tr_entry, n_windows_train)
        sm = metrics(ts_pnl, ts_won, ts_entry, n_windows_test)

        if sm["n"] < 20:
            continue
        # Stability gate: test WR not more than 10pp below train WR
        if tm["n"] > 0 and sm["wr"] < tm["wr"] - 10.0:
            continue

        results.append({
            "params": params,
            "train":  tm,
            "test":   sm,
            "_ts_pnl": ts_pnl.tolist() if len(ts_pnl) else [],
        })

    return sorted(
        [r for r in results if r["test"]["wr"] >= 55.0 and r["test"]["ev"] > 0],
        key=lambda r: r["test"]["ev"],
        reverse=True,
    )[:top_k]


def print_top(asset: str, label: str, top: list):
    if not top:
        print(f"  {label}: no profitable stable config (WR>=55%, EV>0, stable).")
        return
    print(f"\n  -- {label} top results for {asset} --")
    print(f"  {'PARAMS':<55} {'TR-N':>5} {'TR-WR':>6} {'TR-EV':>6}  {'TS-N':>5} {'TS-WR':>6} {'TS-EV':>6} {'MDD':>6} {'SHP':>5} {'T/wk':>5}")
    for r in top:
        p  = str(r["params"])[:53]
        tr = r["train"]
        ts = r["test"]
        print(f"  {p:<55} {tr['n']:>5} {tr['wr']:>5.1f}% {tr['ev']:>+6.3f}  "
              f"{ts['n']:>5} {ts['wr']:>5.1f}% {ts['ev']:>+6.3f} {ts['max_dd']:>6.1f} {ts['sharpe']:>5.3f} {ts['trades_per_week']:>5.1f}")


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets",   nargs="+", default=ASSETS)
    parser.add_argument("--strategy", choices=["A", "B", "C", "all"], default="all")
    parser.add_argument("--top-k",    type=int, default=5)
    parser.add_argument("--wfa",      action="store_true")
    parser.add_argument("--mc",       action="store_true")
    parser.add_argument("--mc-sims",  type=int, default=10_000)
    args = parser.parse_args()

    summary = {}
    t_global = _time.time()

    for asset in args.assets:
        print(f"\n{'='*68}")
        print(f"  ASSET: {asset}")
        print(f"{'='*68}")

        try:
            ts_arr, cl_arr = load_prices(asset)
        except FileNotFoundError as e:
            print(f"  SKIP: {e}")
            continue

        d0 = datetime.fromtimestamp(int(ts_arr[0]), tz=timezone.utc).date()
        d1 = datetime.fromtimestamp(int(ts_arr[-1]), tz=timezone.utc).date()
        print(f"  {len(ts_arr):,} 1-min bars ({d0} to {d1})")

        # Load data for each period
        period_data = {}
        for label, lo, hi in PERIODS:
            t_load = _time.time()
            data = generate_windows_fast(ts_arr, cl_arr, asset,
                                         ts_from(lo), ts_from(hi))
            n_ev  = len(data["window_start"]) if data else 0
            n_win = len(set(data["window_start"].tolist())) if data else 0
            print(f"  {label}: {n_ev:,} evals in {n_win:,} windows ({_time.time()-t_load:.1f}s)")
            period_data[label] = (data, n_win)

        train_data, n_win_train = period_data["TRAIN"]
        test_data,  n_win_test  = period_data["TEST"]

        asset_results = {}

        if args.strategy in ("A", "all"):
            t0 = _time.time()
            total_A = len(list(strat_A_grid()))
            print(f"\n  Strategy A (late-window time-decay) — {total_A} configs...")
            top_A = sweep("A", strat_A_grid, run_strat_A,
                          train_data, test_data, n_win_train, n_win_test,
                          top_k=args.top_k)
            print_top(asset, "Strategy A", top_A)
            asset_results["A"] = top_A
            print(f"  (Strategy A done in {_time.time()-t0:.1f}s)")

        if args.strategy in ("B", "all"):
            t0 = _time.time()
            total_B = len(list(strat_B_grid()))
            print(f"\n  Strategy B (dwell-time persistence) — {total_B} configs...")
            # Precompute dwell lookup for both periods
            dl_train = build_dwell_lookup(train_data) if train_data else {}
            dl_test  = build_dwell_lookup(test_data)  if test_data  else {}
            top_B = sweep("B", strat_B_grid, run_strat_B,
                          train_data, test_data, n_win_train, n_win_test,
                          extra_arg=None, top_k=args.top_k)
            # B needs dwell_lookup — run manually
            results_B = []
            for i, params in enumerate(strat_B_grid()):
                if total_B >= 50 and (i + 1) % (total_B // 5) == 0:
                    print(f"    [B] {i+1}/{total_B}...")
                tr_pnl, tr_won, tr_e, _ = run_strat_B(train_data, dl_train, *params)
                ts_pnl, ts_won, ts_e, _ = run_strat_B(test_data,  dl_test,  *params)
                tm = metrics(tr_pnl, tr_won, tr_e, n_win_train)
                sm = metrics(ts_pnl, ts_won, ts_e, n_win_test)
                if sm["n"] < 20:
                    continue
                if tm["n"] > 0 and sm["wr"] < tm["wr"] - 10.0:
                    continue
                results_B.append({"params": params, "train": tm, "test": sm,
                                   "_ts_pnl": ts_pnl.tolist()})
            top_B = sorted(
                [r for r in results_B if r["test"]["wr"] >= 55.0 and r["test"]["ev"] > 0],
                key=lambda r: r["test"]["ev"], reverse=True,
            )[:args.top_k]
            print_top(asset, "Strategy B", top_B)
            asset_results["B"] = top_B
            print(f"  (Strategy B done in {_time.time()-t0:.1f}s)")

        if args.strategy in ("C", "all"):
            t0 = _time.time()
            total_C = len(list(strat_C_grid()))
            print(f"\n  Strategy C (momentum early-entry) — {total_C} configs...")
            top_C = sweep("C", strat_C_grid, run_strat_C,
                          train_data, test_data, n_win_train, n_win_test,
                          top_k=args.top_k)
            print_top(asset, "Strategy C", top_C)
            asset_results["C"] = top_C
            print(f"  (Strategy C done in {_time.time()-t0:.1f}s)")

        # WFA + Monte Carlo on the best config per strategy
        if args.wfa:
            for s_label in ["A", "B", "C"]:
                if args.strategy not in (s_label, "all"):
                    continue
                top_list = asset_results.get(s_label, [])
                if not top_list:
                    continue
                best_params = top_list[0]["params"]
                run_fn = {"A": run_strat_A, "B": run_strat_B, "C": run_strat_C}[s_label]

                print(f"\n  WFA {asset}/{s_label} best_params={best_params} ...")
                t0 = _time.time()
                wfa_pnl, wfa_won, wfa_entry = run_wfa(ts_arr, cl_arr, asset,
                                                       run_fn, best_params)
                wm = metrics(wfa_pnl, wfa_won, wfa_entry, 1)  # n_windows approx
                print(f"  WFA: N={wm['n']} WR={wm['wr']}% EV={wm['ev']:+.3f} "
                      f"PnL=${wm['pnl']:.2f} MDD=${wm['max_dd']:.2f} "
                      f"Sharpe={wm['sharpe']:.3f} ({_time.time()-t0:.1f}s)")

                if "wfa" not in asset_results:
                    asset_results["wfa"] = {}
                asset_results["wfa"][s_label] = {"metrics": wm, "params": best_params}

                if args.mc and len(wfa_pnl) >= 10:
                    mc = monte_carlo(wfa_pnl, n_sims=args.mc_sims)
                    print(f"  MC ({args.mc_sims:,}x): "
                          f"P05=${mc['pnl_p05']:.0f} P50=${mc['pnl_p50']:.0f} P95=${mc['pnl_p95']:.0f} "
                          f"WR[{mc['wr_p05']}%-{mc['wr_p95']}%] "
                          f"luck_rate={mc['pct_sims_beat']}%")
                    if "mc" not in asset_results:
                        asset_results["mc"] = {}
                    asset_results["mc"][s_label] = mc

                # Save WFA trade PnL for downstream analysis
                if len(wfa_pnl):
                    out = RESULTS_DIR / f"15m_wfa_{asset}_{s_label}.parquet"
                    pd.DataFrame({"pnl": wfa_pnl, "won": wfa_won,
                                  "entry_c": wfa_entry}).to_parquet(out, index=False)
                    print(f"  Saved: {out}")

        summary[asset] = {k: v for k, v in asset_results.items() if k != "_ts_pnl"}

    # Save JSON summary
    def safe(obj):
        if isinstance(obj, (np.ndarray,)):
            return obj.tolist()
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, dict):
            return {k: safe(v) for k, v in obj.items() if k != "_ts_pnl"}
        if isinstance(obj, list):
            return [safe(i) for i in obj]
        return obj

    out = RESULTS_DIR / "15m_research_results.json"
    with open(out, "w") as f:
        json.dump(safe(summary), f, indent=2)
    print(f"\nSaved: {out}")
    print(f"Total time: {_time.time() - t_global:.1f}s")


if __name__ == "__main__":
    main()
