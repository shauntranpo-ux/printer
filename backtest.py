#!/usr/bin/env python3
"""
backtest.py - Strategy backtest for KXBTC15M printer_brain trading logic.

Loads 7.4M rows of BTC 1-min data, segments into 15-minute windows matching
Kalshi market structure, runs the full printer_brain decision loop (entry,
stop-loss monitoring, expiry), and writes results to stress_test_results.

Usage:
    python backtest.py
    python backtest.py --start-year 2023 --ev 0.20 --amount 5
    python backtest.py --sweep          # runs multiple EV thresholds
"""

import argparse
import itertools
import json
import math
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
CSV_PATH  = r'C:\Users\alxnt\Downloads\d5ae29c4-33c6-11f1-b1e7-6dda37cfa7b9\binance_api_BTCUSDT_1m.csv'
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH   = os.path.join(_BASE_DIR, "kalshi_bot.db")
_SPLIT_CFG_PATH   = os.path.join(_BASE_DIR, "data", "split_config.json")
_RESULTS_DIR      = os.path.join(_BASE_DIR, "results")
OOS_REPORT_PATH   = os.path.join(_RESULTS_DIR, "oos_report.json")
_DATA_DIR  = os.path.join(_BASE_DIR, "data")
_BV3_DIR   = os.path.join(_BASE_DIR, "bv3_tables")

# Strike increment per asset (matches bot.py / generate_bv3_table.py)
STRIKE_INCREMENTS = {
    "BTC":  1000.0,
    "ETH":    25.0,
    "SOL":     1.0,
    "XRP":     0.01,
    "DOGE":    0.001,
}
ALL_ASSETS = list(STRIKE_INCREMENTS.keys())


def _get_csv_path(asset: str) -> str:
    """Return path to 1-minute CSV for an asset (standard location first, BTC legacy fallback)."""
    standard = os.path.join(_DATA_DIR, f"{asset}_1m.csv")
    if os.path.exists(standard):
        return standard
    if asset == "BTC" and os.path.exists(CSV_PATH):
        return CSV_PATH
    return standard  # caller will error clearly if missing


# -----------------------------------------------------------------------------
# Split config helpers (reads data/split_config.json written by data/splitter.py)
# -----------------------------------------------------------------------------

def _load_split_cfg() -> dict | None:
    """Return the train/OOS split config, or None if not yet generated."""
    if not os.path.exists(_SPLIT_CFG_PATH):
        return None
    with open(_SPLIT_CFG_PATH) as fh:
        return json.load(fh)


def _ensure_split(start_year: int) -> dict:
    """
    Return the split config, auto-generating it via data/splitter.py if absent.
    """
    cfg = _load_split_cfg()
    if cfg:
        return cfg
    print("[OOS] No data/split_config.json found - generating split now ...")
    import importlib.util
    _sfile = os.path.join(_BASE_DIR, "data", "splitter.py")
    if not os.path.exists(_sfile):
        raise FileNotFoundError(
            f"data/splitter.py not found at {_sfile}.\n"
            "Create it or run: python data/splitter.py"
        )
    spec = importlib.util.spec_from_file_location("splitter", _sfile)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.generate_split(start_year=start_year, force=False)


def _filter_to(windows, price_lookup, mode: str, cfg: dict):
    """Filter windows/price_lookup to 'train' or 'oos' slice."""
    if mode == "train":
        ts_lo, ts_hi = cfg["train_start_ts"], cfg["train_end_ts"]
    else:
        ts_lo, ts_hi = cfg["oos_start_ts"],   cfg["oos_end_ts"]
    mask   = (windows.index >= ts_lo) & (windows.index <= ts_hi)
    w_out  = windows[mask]
    pl_out = price_lookup[price_lookup.index.isin(w_out.index)]
    return w_out, pl_out


def _load_best_params() -> dict:
    """
    Return the best params from monte_carlo_results.json (rank-1 by Sharpe).
    Falls back to config.json values if MC results are not available.
    """
    mc_path = os.path.join(_BASE_DIR, "monte_carlo_results.json")
    if os.path.exists(mc_path):
        with open(mc_path) as fh:
            mc = json.load(fh)
        top = mc.get("top_20", [])
        if top:
            return top[0]["params"]
    # Fallback: read live config
    cfg_path = os.path.join(_BASE_DIR, "config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path) as fh:
            cfg = json.load(fh)
        return {
            "min_ev":         0.30,
            "min_confidence": int(cfg.get("confidence_threshold", 80)),
        }
    return {"min_ev": 0.30, "min_confidence": 80}

# -----------------------------------------------------------------------------
# Empirical win-probability tables -- loaded from bv3_tables/*.json
# Pre-2023 tables are used to avoid data leakage in backtesting.
# Falls back to the hardcoded BTC table when no JSON file is present.
# -----------------------------------------------------------------------------
_BV3_TABLE = [
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
_BV3_DIST_BOUNDS = [0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.0075, 0.010, 0.0125]

# Per-asset BV3 cache populated on first use: {asset: (table, dist_bounds)}
_bv3_by_asset: dict[str, tuple] = {}


def _load_bv3_for_asset(asset: str) -> tuple[list, list]:
    """
    Load pre-2023 BV3 probability table for an asset from bv3_tables/.
    Uses pre-2023 data to avoid training-set leakage in backtesting.
    Falls back to the hardcoded BTC table.
    """
    path = os.path.join(_BV3_DIR, f"{asset}_bv3_pre2023.json")
    if os.path.exists(path):
        try:
            with open(path) as fh:
                data = json.load(fh)
            print(f"  [BV3] {asset}: loaded pre-2023 table from {os.path.basename(path)} "
                  f"({data.get('metadata', {}).get('total_windows', '?'):,} windows)")
            return data["table"], data["dist_bounds"]
        except Exception as exc:
            print(f"  [BV3] {asset}: failed to load {path}: {exc} -- using BTC fallback")
    else:
        if asset != "BTC":
            print(f"  [BV3] {asset}: no pre-2023 table at {path} -- using BTC fallback")
    return _BV3_TABLE, _BV3_DIST_BOUNDS


def _empirical_win_prob(abs_pct: float, mins_left: float, asset: str = "BTC") -> float:
    """Look up empirical win probability from the per-asset pre-2023 BV3 table."""
    if asset not in _bv3_by_asset:
        _bv3_by_asset[asset] = _load_bv3_for_asset(asset)
    table, dist_bounds = _bv3_by_asset[asset]
    bidx = len(dist_bounds)
    for i, bound in enumerate(dist_bounds):
        if abs_pct < bound:
            bidx = i
            break
    bidx = min(bidx, len(table) - 1)
    row  = table[bidx]
    if mins_left < 1.0:
        return min(0.997, row[0] + 0.005)
    n_cols = len(row)
    if mins_left >= n_cols:
        return row[-1]
    t_low  = int(mins_left) - 1
    t_high = t_low + 1
    frac   = mins_left - int(mins_left)
    if t_high >= n_cols:
        return row[-1]
    return row[t_low] + (row[t_high] - row[t_low]) * frac


# -----------------------------------------------------------------------------
# AMM price simulator
# -----------------------------------------------------------------------------

def simulate_amm_prices(btc_price: float, strike: float, rng: random.Random) -> tuple[float, float]:
    """
    Simulate realistic Kalshi AMM yes_ask / no_ask prices based on BTC
    distance from strike.  Matches observed live pricing behaviour:

        < 0.1%  distance -> yes_ask 45-55c  (near 50/50)
        0.1-0.3% distance -> favoured side 62-75c
        0.3%+   distance -> favoured side 77-92c

    Returns (yes_ask_cents, no_ask_cents).  Sum is always > 100 (AMM spread).
    """
    pct    = (btc_price - strike) / strike
    ap     = abs(pct) * 100   # distance as a percentage
    above  = pct > 0
    spread = rng.uniform(3.0, 6.0)

    if ap < 0.10:
        # Coin-flip zone - tiny edge to whichever side BTC is on
        base = rng.uniform(49.0, 54.0) if above else rng.uniform(46.0, 51.0)
        yes_ask = base
    elif ap < 0.30:
        if above:
            yes_ask = rng.uniform(62.0, 75.0)
        else:
            yes_ask = 100 - rng.uniform(62.0, 75.0)  # NO favoured -> YES is cheap
    else:
        if above:
            yes_ask = rng.uniform(77.0, 92.0)
        else:
            yes_ask = 100 - rng.uniform(77.0, 92.0)

    yes_ask = max(3.0, min(97.0, yes_ask))
    no_ask  = max(3.0, min(97.0, 100.0 + spread - yes_ask))
    return yes_ask, no_ask



# -----------------------------------------------------------------------------
# Brain decision (stateless replica of printer_brain from bot.py)
# -----------------------------------------------------------------------------

def brain_decide(
    btc_price: float,
    strike: float,
    yes_ask: float,
    no_ask: float,
    mins_left: float,
    mom_label: str,
    prob_scale: float = 1.0,
    min_ev: float = 0.15,
    bullish_wr: float = 0.5,
    bearish_wr: float = 0.5,
    fee: float = 0.07,
    asset: str = "BTC",
    max_entry_price_cents: float = 100.0,
    min_reward_cents: float = 0.0,
    max_risk_reward_ratio: float = 999.0,
    accel_adj: float = 0.0,
) -> dict:
    pct_above = (btc_price - strike) / strike
    abs_pct   = abs(pct_above)
    above     = pct_above > 0

    win_prob_raw = _empirical_win_prob(abs_pct, mins_left, asset=asset)

    if mom_label == "bullish":
        mom_adj = +0.05 if above else -0.05
    elif mom_label == "bearish":
        mom_adj = +0.05 if not above else -0.05
    else:
        mom_adj = 0.0

    win_prob = win_prob_raw + mom_adj + accel_adj
    win_prob = 0.50 + (win_prob - 0.50) * prob_scale
    win_prob = max(0.10, min(0.997, win_prob))

    prob_yes = win_prob if above else (1.0 - win_prob)
    prob_no  = 1.0 - prob_yes

    yes_ev = prob_yes - (yes_ask / 100) - fee
    no_ev  = prob_no  - (no_ask  / 100) - fee

    if bullish_wr < 0.35: yes_ev -= 0.04
    if bearish_wr < 0.35: no_ev  -= 0.04

    if above:
        side, best_ev, entry_c, true_p = "yes", yes_ev, yes_ask, prob_yes
    else:
        side, best_ev, entry_c, true_p = "no",  no_ev,  no_ask,  prob_no

    # Entry price hard filters (mirrors bot.py printer_brain logic)
    price_filter_skip = False
    if entry_c > max_entry_price_cents:
        price_filter_skip = True
    else:
        _reward_c = 100 - entry_c
        if _reward_c < min_reward_cents:
            price_filter_skip = True
        elif _reward_c > 0 and (entry_c / _reward_c) > max_risk_reward_ratio:
            price_filter_skip = True

    if price_filter_skip:
        action = "skip"
    else:
        action = "trade" if best_ev >= min_ev else "skip"
    confidence = min(99, max(0, int(true_p * 100)))

    return {
        "action":     action,
        "side":       side,
        "confidence": confidence,
        "win_prob":   float(true_p),
        "ev":         float(best_ev),
        "entry_c":    float(entry_c),
    }


def compute_momentum(recent_closes: list) -> tuple[float, str]:
    """3-min momentum pct and label from a list of recent close prices."""
    if len(recent_closes) < 2:
        return 0.0, "neutral"
    pct = (recent_closes[-1] - recent_closes[0]) / recent_closes[0]
    if pct > 0.005:  return pct, "bullish"
    if pct < -0.005: return pct, "bearish"
    return pct, "neutral"


def compute_momentum_acceleration(p_prior: float, p_mid: float, p_now: float,
                                  mom_accel_scale: float = 3.0) -> float:
    """
    Second derivative of momentum: (recent 3-min mom) - (prior 3-min mom).
    Returns clamped accel value (not adjusted by direction yet).
    Mirrors skip_layer._momentum_acceleration().
    """
    if p_prior <= 0 or p_mid <= 0:
        return 0.0
    mom_recent = (p_now - p_mid)   / p_mid
    mom_prior  = (p_mid - p_prior) / p_prior
    accel = mom_recent - mom_prior
    THRESHOLD = 0.002
    if abs(accel) < THRESHOLD:
        return 0.0
    return float(max(-0.03, min(0.03, accel * mom_accel_scale)))


# -----------------------------------------------------------------------------
# Main backtest
# -----------------------------------------------------------------------------

def load_data(start_year: int = 2020, end_year: int = 9999, verbose: bool = True,
              mode: str = "train", asset: str = "BTC"):
    """
    Load and preprocess the 1-minute CSV for the given asset. Returns (windows, price_lookup).

    Handles two CSV formats:
      Legacy (BTC only): "time" column with Unix seconds integers.
      Standard:          "open_time" column with ISO-8601 strings.

    mode
    ----
    'train'  - only the 70% training partition (default).
               If data/split_config.json does not exist yet, all data is
               returned with a warning - run  python data/splitter.py  first.
    'oos'    - only the 30% OOS holdout (use only via --oos-eval).
    'full'   - all windows, no split applied (internal use).
    """
    csv_path = _get_csv_path(asset)
    if not os.path.exists(csv_path):
        raise FileNotFoundError(
            f"[{asset}] CSV not found: {csv_path}\n"
            f"  Run: python download_data.py --asset {asset}"
        )

    if verbose:
        print(f"\nLoading [{asset}] {csv_path}...")
    t0 = time.time()

    # Detect format from column names via first read
    _cols = pd.read_csv(csv_path, nrows=0).columns.tolist()
    _has_legacy = "time" in _cols and "open_time" not in _cols

    if _has_legacy:
        # Legacy BTC CSV: "time" (Unix seconds), all numeric
        df = pd.read_csv(
            csv_path,
            dtype={"time": "float64", "open": "float64", "high": "float64",
                   "low": "float64", "close": "float64", "volume": "float64"},
            engine="c",
        )
        df.rename(columns={"time": "Timestamp", "open": "Open", "high": "High",
                            "low": "Low", "close": "Close", "volume": "Volume"}, inplace=True)
        df["ts"] = df["Timestamp"].astype("int64")   # already Unix seconds
        df["_dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    else:
        # Standard CSV: "open_time" (ISO string), lowercase column names
        df = pd.read_csv(
            csv_path,
            dtype={"open": "float64", "high": "float64",
                   "low": "float64", "close": "float64", "volume": "float64"},
            engine="c",
        )
        df.rename(columns={"open_time": "Timestamp", "open": "Open", "high": "High",
                            "low": "Low", "close": "Close", "volume": "Volume"}, inplace=True)
        # Parse ISO timestamps -> UTC datetime, then Unix seconds
        df["_dt"] = pd.to_datetime(df["Timestamp"], utc=True)
        # Use total_seconds() -- avoids pandas version differences with astype("int64")
        _epoch = pd.Timestamp("1970-01-01", tz="UTC")
        df["ts"] = (df["_dt"] - _epoch).dt.total_seconds().astype("int64")

    df = df.dropna(subset=["Open", "Close"])
    df = df[df["Close"] > 0]
    if verbose:
        print(f"  [{asset}] Loaded {len(df):,} rows in {time.time()-t0:.1f}s")

    # Use the parsed datetime column for year filtering (avoids unit confusion)
    df["year"] = df["_dt"].dt.year
    df = df[(df["year"] >= start_year) & (df["year"] <= end_year)].copy()
    if verbose:
        yr_range = f"{start_year}-{end_year}" if end_year < 9999 else f"{start_year}+"
        print(f"  Filtered to {yr_range}: {len(df):,} rows")

    df["window_start"]     = (df["ts"] // 900) * 900
    df["minute_in_window"] = (df["ts"] - df["window_start"]) // 60
    df = df.sort_values(["window_start", "ts"])

    strikes = (
        df[df["minute_in_window"] == 0]
        .groupby("window_start")["Open"]
        .first()
        .rename("strike")
    )
    finals = (
        df.groupby("window_start")
        .apply(lambda g: g.loc[g["minute_in_window"].idxmax(), "Close"],
               include_groups=False)
    )
    finals.name = "final_close"
    windows = strikes.to_frame().join(finals).dropna()
    windows = windows[(windows["strike"] > 0) & (windows["final_close"] > 0)]

    price_lookup = (
        df.groupby("window_start")
        .apply(lambda g: dict(zip(g["minute_in_window"].tolist(),
                                   g["Close"].tolist())),
               include_groups=False)
    )

    # -- Apply train / OOS split -----------------------------------------------
    if mode != "full":
        cfg = _load_split_cfg()
        _split_valid = cfg is not None and "train_start_ts" in cfg and "oos_start_ts" in cfg
        if not _split_valid:
            if verbose:
                print("  [warn] data/split_config.json not found or outdated - using all data.")
                print("         Run  python data/splitter.py  to enforce train/OOS split.")
            cfg = None
        if cfg is not None:
            ts_lo = cfg["train_start_ts"] if mode == "train" else cfg["oos_start_ts"]
            ts_hi = cfg["train_end_ts"]   if mode == "train" else cfg["oos_end_ts"]
            mask         = (windows.index >= ts_lo) & (windows.index <= ts_hi)
            windows      = windows[mask]
            price_lookup = price_lookup[price_lookup.index.isin(windows.index)]
            if verbose:
                lbl = "TRAIN" if mode == "train" else "OOS HOLDOUT [!]"
                d0  = cfg["train_start_date" if mode == "train" else "oos_start_date"]
                d1  = cfg["train_end_date"   if mode == "train" else "oos_end_date"]
                print(f"  [{lbl}] {len(windows):,} windows  [{d0} -> {d1}]")

    if verbose:
        print(f"  Windows: {len(windows):,}")
    return windows, price_lookup


# Read fee from config.json if present; fall back to 7 (matches bot.py default).
# To change the fee, edit kalshi_fee_per_contract_cents in config.json.
def _load_fee_from_config() -> int:
    _cfg_path = os.path.join(_BASE_DIR, "config.json")
    try:
        with open(_cfg_path) as _fh:
            return int(json.load(_fh).get("kalshi_fee_per_contract_cents", 7))
    except Exception:
        return 7

KALSHI_FEE_CENTS: int = _load_fee_from_config()


def _load_asset_overrides(asset: str) -> dict:
    """Load per-asset signal config from config.json asset_overrides."""
    try:
        _cfg_path = os.path.join(_BASE_DIR, "config.json")
        with open(_cfg_path) as _fh:
            cfg = json.load(_fh)
        return cfg.get("asset_overrides", {}).get(asset, {})
    except Exception:
        return {}


def run_backtest(
    start_year: int     = 2020,
    end_year: int       = 9999,
    min_ev: float       = 0.05,
    trade_amount: float = 25.0,
    watch_minutes: int  = 1,
    seed: int = 42,
    verbose: bool = True,
    min_confidence: int = 65,
    vol_threshold: float = 1.50,   # skip if expected_move / abs_pct >= this
    asset: str          = "BTC",   # which asset to backtest
    max_entry_price_cents: float = 100.0,
    min_reward_cents: float = 0.0,
    max_risk_reward_ratio: float = 999.0,
    # Pre-loaded data (skips CSV load when provided)
    _windows=None,
    _price_lookup=None,
    # If provided, individual trade dicts are appended here (for stress testing)
    _trades_out: list | None = None,
) -> dict:
    rng = random.Random(seed)

    # Load per-asset P1/P2/P4 signal config from config.json
    _overrides = _load_asset_overrides(asset)
    vol_confirm_mult        = _overrides.get("vol_confirm_mult",        1.0)
    vol_oppose_mult         = _overrides.get("vol_oppose_mult",         1.0)
    mom_lock_enabled        = _overrides.get("mom_lock_enabled",        True)
    mom_lock_neutral_tighten = _overrides.get("mom_lock_neutral_tighten", 1.0)
    mom_accel_scale         = _overrides.get("mom_accel_scale",         3.0)
    # Per-asset overrides for vol_gate_thresh and min_ev_base (match live bot behaviour)
    if "vol_gate_thresh" in _overrides:
        vol_threshold = _overrides["vol_gate_thresh"]
    if "min_ev_base" in _overrides:
        min_ev = float(_overrides["min_ev_base"]) / 100.0
    if verbose:
        print(f"  P1/P2/P4 overrides for {asset}: "
              f"vol_confirm={vol_confirm_mult}, vol_oppose={vol_oppose_mult}, "
              f"mom_lock={mom_lock_enabled}, neutral_tighten={mom_lock_neutral_tighten}, "
              f"accel_scale={mom_accel_scale}")

    # Pre-warm the BV3 table for this asset (prints load message once)
    if asset not in _bv3_by_asset:
        _bv3_by_asset[asset] = _load_bv3_for_asset(asset)

    # -- Load data (or use pre-loaded) -----------------------------------------
    if _windows is None or _price_lookup is None:
        windows, price_lookup = load_data(start_year, end_year=end_year, verbose=verbose, asset=asset)
    else:
        windows      = _windows
        price_lookup = _price_lookup

    # -- Simulation loop -------------------------------------------------------
    trades         = []
    skipped        = 0
    total_windows  = len(windows)

    if verbose:
        print(f"\nSimulating {total_windows:,} windows "
              f"(min_ev={min_ev:.0%}, trade=${trade_amount:.0f})...")

    last_print = time.time()

    for i, (window_start, row) in enumerate(windows.iterrows()):
        if verbose and time.time() - last_print > 5:
            pct_done = i / total_windows * 100
            print(f"  {pct_done:.0f}%  ({i:,}/{total_windows:,} windows, "
                  f"{len(trades):,} trades so far)...")
            last_print = time.time()

        strike      = row["strike"]
        final_close = row["final_close"]
        prices      = price_lookup.get(window_start, {})

        if not prices or len(prices) < 3:
            skipped += 1
            continue

        # Try each minute after WATCH phase
        trade_placed = False
        for minute in range(watch_minutes, 14):
            btc = prices.get(minute)
            if btc is None:
                continue

            mins_left = float(15 - minute)

            # 3-minute momentum (P1/P2/P4)
            recent = [prices[m] for m in range(max(0, minute - 3), minute + 1)
                      if m in prices]
            _mom_pct, mom = compute_momentum(recent)

            # P2: Direction lock -- skip when momentum opposes trade side.
            # above=True -> would bet YES; bearish momentum opposes YES.
            # above=False -> would bet NO; bullish momentum opposes NO.
            _above = btc > strike
            if mom_lock_enabled and mom != "neutral":
                _mom_opposes = (mom == "bearish" and _above) or \
                               (mom == "bullish" and not _above)
                if _mom_opposes:
                    continue

            # P4: Momentum acceleration -- second derivative, confirming direction only.
            _accel_adj = 0.0
            _p_mid   = prices.get(minute - 3)
            _p_prior = prices.get(minute - 6)
            if _p_mid is not None and _p_prior is not None and mom != "neutral":
                _mom_confirms = (mom == "bullish" and _above) or \
                                (mom == "bearish" and not _above)
                if _mom_confirms:
                    _raw_accel = compute_momentum_acceleration(
                        _p_prior, _p_mid, btc, mom_accel_scale
                    )
                    _mom_sign  = 1.0 if _above else -1.0
                    _accel_adj = float(max(-0.03, min(0.03, _mom_sign * _raw_accel)))

            # -- Vol gate (mirrors bot.py realized vol gate) ------------------
            # FIX: Original code used only prices from minute 0..`minute`
            # within the current window. At minute 2, that's 3 data points --
            # too few for a stable std estimate and ignores prior-window vol.
            #
            # The live bot uses btc_prices deque (500 entries), always giving
            # 10+ minutes of lookback across window boundaries. The mismatch
            # made the backtest vol gate inconsistent with live behavior,
            # slightly inflating simulated results.
            #
            # Fix: collect the last 10 minutes crossing the 15-min boundary.
            # If prior-window data is unavailable, skip the vol gate for that
            # evaluation rather than using a <=3-point estimate.
            abs_pct = abs((btc - strike) / strike)
            vol_skip = False
            if abs_pct > 0:
                within_prices = [prices[m] for m in range(0, minute + 1) if m in prices]

                if len(within_prices) >= 10:
                    vol_prices = within_prices[-10:]
                else:
                    prev_ws = window_start - 900
                    prev_dict = price_lookup.get(prev_ws, {})
                    if prev_dict:
                        need = 10 - len(within_prices)
                        prev_tail = [prev_dict[m]
                                     for m in sorted(prev_dict.keys())
                                     if m >= 15 - need]
                        vol_prices = prev_tail + within_prices
                    else:
                        # No prior-window data -- skip vol gate rather than
                        # using an unreliable sparse estimate.
                        vol_prices = []

                if len(vol_prices) >= 4:
                    vol_rets = [(vol_prices[j] - vol_prices[j-1]) / vol_prices[j-1]
                                for j in range(1, len(vol_prices))]
                    rv = float(np.std(vol_rets))
                    expected_move = rv * (mins_left ** 0.5)
                    # P1: Momentum-adaptive vol threshold.
                    # Confirming momentum -> relax threshold (require stronger vol to skip).
                    # Opposing momentum already blocked by P2 direction lock above.
                    # Neutral -> tighten slightly.
                    if mom == "neutral":
                        _eff_thresh = vol_threshold * mom_lock_neutral_tighten
                    elif (mom == "bullish" and _above) or (mom == "bearish" and not _above):
                        _eff_thresh = vol_threshold * vol_confirm_mult
                    else:
                        _eff_thresh = vol_threshold * vol_oppose_mult
                    if expected_move / abs_pct >= _eff_thresh:
                        vol_skip = True
            if vol_skip:
                continue

            # Simulate AMM prices
            yes_ask, no_ask = simulate_amm_prices(btc, strike, rng)

            # Brain decision
            brain = brain_decide(btc, strike, yes_ask, no_ask, mins_left,
                                  mom, min_ev=min_ev, fee=KALSHI_FEE_CENTS / 100.0,
                                  asset=asset,
                                  max_entry_price_cents=max_entry_price_cents,
                                  min_reward_cents=min_reward_cents,
                                  max_risk_reward_ratio=max_risk_reward_ratio,
                                  accel_adj=_accel_adj)

            if brain["action"] != "trade":
                continue

            # -- Confidence filter ---------------------------------------------
            if brain["confidence"] < min_confidence:
                continue

            # -- Entry ---------------------------------------------------------
            side       = brain["side"]
            entry_c    = brain["entry_c"]
            confidence = brain["confidence"]
            win_prob   = brain["win_prob"]
            ev         = brain["ev"]

            # Fixed position sizing (matches bot.py calculate_contracts)
            contracts = max(1, int(trade_amount * 100 / entry_c))

            exit_reason = "expiry"

            # -- Expiry outcome - hold to settlement, no stop loss -------------
            above_at_close = final_close > strike
            won = (side == "yes" and above_at_close) or \
                  (side == "no"  and not above_at_close)
            exit_price = 100.0 if won else 0.0

            # Fee model: $0.07/contract deducted from every trade (conservative --
            # Kalshi only charges on wins, but flat deduction simplifies accounting).
            fee_total  = contracts * KALSHI_FEE_CENTS / 100.0
            pnl        = (exit_price - entry_c) * contracts / 100.0 - fee_total
            profit_pct = (exit_price - entry_c) / entry_c * 100.0 if entry_c else 0.0

            trades.append({
                "window_start": int(window_start),
                "minute_in":    minute,
                "side":         side,
                "entry_c":      round(entry_c,    1),
                "exit_price":   round(exit_price, 1),
                "exit_reason":  exit_reason,
                "outcome":      "win" if won else "loss",
                "pnl":          round(pnl,        4),
                "fee":          round(fee_total,  4),
                "profit_pct":   round(profit_pct, 2),
                "confidence":   confidence,
                "win_prob":     round(win_prob,   4),
                "ev":           round(ev,         4),
                "mom":          mom,
            })

            trade_placed = True
            break  # one trade per window

        if not trade_placed:
            skipped += 1

    # -- Expose trades for stress testing -------------------------------------
    if _trades_out is not None:
        _trades_out.extend(trades)

    # -- Statistics ------------------------------------------------------------
    if not trades:
        print("No trades placed. Try reducing --ev threshold.")
        return {}

    total  = len(trades)
    wins   = sum(1 for t in trades if t["outcome"] == "win")
    losses = total - wins
    wr     = wins / total

    pnls       = [t["pnl"] for t in trades]
    total_pnl  = sum(pnls)

    # Fee totals (for reality-check no-fee comparison)
    total_fees         = sum(t["fee"] for t in trades)
    total_pnl_no_fees  = round(total_pnl + total_fees, 2)

    # Daily Sharpe: group trade PnL by calendar day, then scale by sqrt252
    _daily_pnl: dict[int, float] = {}
    for _t in trades:
        _day = _t["window_start"] // 86400
        _daily_pnl[_day] = _daily_pnl.get(_day, 0.0) + _t["pnl"]
    _dpnls = list(_daily_pnl.values())
    if len(_dpnls) > 1:
        _mu_d  = sum(_dpnls) / len(_dpnls)
        _var_d = sum((p - _mu_d) ** 2 for p in _dpnls) / (len(_dpnls) - 1)
        _sd_d  = math.sqrt(_var_d)
        sharpe_daily = round((_mu_d / _sd_d * math.sqrt(252)) if _sd_d > 0 else 0.0, 3)
    else:
        sharpe_daily = 0.0

    # Max drawdown (on cumulative PnL curve)
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        if peak > 0:
            dd = (peak - cum) / peak * 100
            if dd > max_dd:
                max_dd = dd

    # Sharpe ratios
    if len(pnls) > 1:
        mu  = sum(pnls) / len(pnls)
        var = sum((p - mu) ** 2 for p in pnls) / (len(pnls) - 1)
        sd  = math.sqrt(var)
        # Per-trade Sharpe: no annualization multiplier -- honest baseline
        sharpe_per_trade = (mu / sd) if sd > 0 else 0.0
        # Annualised Sharpe: inflated by sqrt35040 (each 15-min slot/year).
        # WARNING: this number is misleading -- use sharpe_per_trade for realistic assessment.
        sharpe = (mu / sd * math.sqrt(35040)) if sd > 0 else 0.0
    else:
        sharpe = 0.0
        sharpe_per_trade = 0.0

    # Max consecutive losses
    max_cl, cur_cl = 0, 0
    for t in trades:
        if t["outcome"] == "loss":
            cur_cl += 1
            max_cl = max(max_cl, cur_cl)
        else:
            cur_cl = 0

    avg_conf       = sum(t["confidence"]  for t in trades) / total
    avg_profit_pct = sum(t["profit_pct"]  for t in trades) / total

    # Entry minute distribution
    minute_dist = {}
    for t in trades:
        m = t["minute_in"]
        minute_dist[m] = minute_dist.get(m, 0) + 1

    # Side distribution + per-side win rates
    yes_trade_list = [t for t in trades if t["side"] == "yes"]
    no_trade_list  = [t for t in trades if t["side"] == "no"]
    yes_trades = len(yes_trade_list)
    no_trades  = len(no_trade_list)
    yes_wr = (sum(1 for t in yes_trade_list if t["outcome"] == "win") / yes_trades
              if yes_trades else 0.0)
    no_wr  = (sum(1 for t in no_trade_list  if t["outcome"] == "win") / no_trades
              if no_trades  else 0.0)

    # Exit reason breakdown
    exit_dist = {}
    for t in trades:
        r = t["exit_reason"]
        exit_dist[r] = exit_dist.get(r, 0) + 1

    # Stop-loss win rate vs expiry win rate
    exp_exits = [t for t in trades if t["exit_reason"] == "expiry"]
    exp_wr = sum(1 for t in exp_exits if t["outcome"] == "win") / len(exp_exits) if exp_exits else 0.0

    result = {
        "start_year":             start_year,
        "asset":                  asset,
        "min_ev":                 min_ev,
        "trade_amount_dollars":   trade_amount,
        "total_windows":          total_windows,
        "windows_skipped":        skipped,
        "total_trades":           total,
        "wins":                   wins,
        "losses":                 losses,
        "win_rate":               round(wr,         4),
        "total_pnl_dollars":      round(total_pnl,  2),
        "max_drawdown_percent":   round(max_dd,     2),
        "sharpe_ratio":           round(sharpe,          3),
        "sharpe_per_trade":       round(sharpe_per_trade, 4),
        "sharpe_daily":           sharpe_daily,
        "total_fees":             round(total_fees,       2),
        "total_pnl_no_fees":      total_pnl_no_fees,
        "fee_model":              f"${KALSHI_FEE_CENTS / 100:.2f}/contract included",
        "max_consecutive_losses": max_cl,
        "avg_confidence":         round(avg_conf,       1),
        "avg_profit_percent":     round(avg_profit_pct, 2),
        "yes_trades":             yes_trades,
        "no_trades":              no_trades,
        "yes_win_rate":           round(yes_wr, 4),
        "no_win_rate":            round(no_wr,  4),
        "exit_dist":              exit_dist,
        "expiry_win_rate":        round(exp_wr, 3),
        "minute_dist":            dict(sorted(minute_dist.items())),
    }
    return result


# -----------------------------------------------------------------------------
# DB writer
# -----------------------------------------------------------------------------

def write_to_db(r: dict, start_year: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        INSERT INTO stress_test_results (
            run_ts, start_date, end_date,
            total_markets, total_trades, win_rate,
            total_pnl_dollars, max_drawdown_percent,
            avg_confidence, avg_profit_percent,
            sharpe_ratio, max_consecutive_losses
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        f"{start_year}-01-01",
        "2026-12-31",
        r["total_windows"],
        r["total_trades"],
        r["win_rate"],
        r["total_pnl_dollars"],
        r["max_drawdown_percent"],
        r["avg_confidence"],
        r["avg_profit_percent"],
        r["sharpe_ratio"],
        r["max_consecutive_losses"],
    ))
    conn.commit()
    conn.close()


# -----------------------------------------------------------------------------
# Report printer
# -----------------------------------------------------------------------------

def print_report(r: dict) -> None:
    if not r:
        return
    W = 64
    print("\n" + "=" * W)
    print(f"  BACKTEST RESULTS  -  {r['start_year']}+  |  min_ev={r['min_ev']:.0%}")
    print("=" * W)
    print(f"  Fee model         : {r.get('fee_model', '$0.07/contract included')}")
    _asset_r = r.get("asset", "BTC")
    _pre23_path = os.path.join(_BV3_DIR, f"{_asset_r}_bv3_pre2023.json")
    if os.path.exists(_pre23_path):
        print(f"  [OK] BV3 table: pre-2023 data only -- honest out-of-sample baseline.")
    else:
        print(f"  [!!] WARNING: BV3 table built on 2017-2026 data -- OOS period NOT clean.")
        print(f"     Run: python generate_bv3_table.py --asset {_asset_r}")
    print("-" * W)
    print(f"  Windows simulated : {r['total_windows']:>10,}")
    print(f"  Trades placed     : {r['total_trades']:>10,}  "
          f"({r['total_trades']/r['total_windows']*100:.1f}% of windows)")
    print(f"  YES / NO split    : {r['yes_trades']:,} / {r['no_trades']:,}  "
          f"(WR: YES={r.get('yes_win_rate',0)*100:.1f}%  NO={r.get('no_win_rate',0)*100:.1f}%)")
    print("-" * W)
    print(f"  Win rate          : {r['win_rate']*100:>10.1f}%")
    print(f"  Total P&L         : ${r['total_pnl_dollars']:>9.2f}")
    print(f"  Avg profit/trade  : {r['avg_profit_percent']:>9.1f}%")
    print(f"  Avg confidence    : {r['avg_confidence']:>10.1f}")
    print("-" * W)
    print(f"  Max drawdown      : {r['max_drawdown_percent']:>9.1f}%")
    spt = r.get("sharpe_per_trade", 0.0)
    print(f"  Sharpe (annlzd)   : {r['sharpe_ratio']:>10.3f}  <- inflated by sqrt35040")
    print(f"  Sharpe (per-trade): {spt:>10.4f}  <- use this for honest assessment")
    print(f"  NOTE: Per-slot annualized Sharpe is inflated by sqrt35040 multiplier.")
    print(f"        Use per-trade or daily Sharpe for realistic assessment.")
    if spt > 3.0:
        print(f"  [!]  Per-trade Sharpe > 3.0 is suspicious.")
        print(f"     Verify that simulated prices match real market prices before")
        print(f"     trusting this number. Run price_validator.py to check.")
    print(f"  Max consec losses : {r['max_consecutive_losses']:>10}")
    print("-" * W)
    print(f"  Exit at expiry    : {r['exit_dist'].get('expiry',0):>6,}  WR={r['expiry_win_rate']*100:.1f}%")
    print("-" * W)
    print("  Entry minute distribution:")
    for m, n in sorted(r["minute_dist"].items()):
        bar = "#" * (n * 30 // max(r["minute_dist"].values()))
        print(f"    min {m:2d}: {n:6,}  {bar}")
    print("=" * W)


# -----------------------------------------------------------------------------
# Reality check -- always-on block, cannot be disabled
# -----------------------------------------------------------------------------

def print_reality_check(r: dict) -> None:
    """
    Print a clearly labeled REALITY CHECK block at the end of every backtest run.
    Cannot be disabled. Always prints.
    """
    if not r:
        return

    BORDER = "=" * 51

    # Pull metrics from result
    total      = r.get("total_trades", 0)
    spt        = r.get("sharpe_per_trade", 0.0)
    sdaily     = r.get("sharpe_daily",     0.0)
    sann       = r.get("sharpe_ratio",     0.0)
    wr         = r.get("win_rate",         0.0) * 100
    pnl        = r.get("total_pnl_dollars",  0.0)
    pnl_nf     = r.get("total_pnl_no_fees",  pnl)
    fees       = r.get("total_fees",         0.0)
    avg_pnl    = pnl    / total if total else 0.0
    avg_pnl_nf = pnl_nf / total if total else 0.0
    avg_fee    = fees   / total if total else 0.0

    # Price validation CSV
    pv_csv = os.path.join(_BASE_DIR, "price_validation_log.csv")
    pv_samples      = 0
    price_validated = False
    if os.path.isfile(pv_csv):
        try:
            with open(pv_csv) as _fh:
                pv_samples = max(0, len(_fh.readlines()) - 1)
            price_validated = pv_samples >= 200
        except Exception:
            pass

    # Walk-forward param variation (read wfv_report.json if present)
    wfv_report_path = os.path.join(_BASE_DIR, "results", "wfv_report.json")
    wfv_varied = None  # None = not run yet
    if os.path.isfile(wfv_report_path):
        try:
            with open(wfv_report_path) as _wf:
                _wfv = json.load(_wf)
            _all_params = [w.get("best_params") for w in _wfv.get("windows", [])
                           if w.get("best_params")]
            if len(_all_params) >= 2:
                _u_ev   = len({p.get("min_ev")          for p in _all_params})
                _u_conf = len({p.get("min_confidence")  for p in _all_params})
                wfv_varied = (_u_ev > 1 or _u_conf > 1)
        except Exception:
            pass

    # Print the block
    print("\n" + BORDER)
    print("REALITY CHECK -- READ THIS BEFORE TRUSTING RESULTS")
    print(BORDER)
    print()
    print(f"Sharpe (per-trade):            {spt:.2f}")
    print(f"Sharpe (daily, sqrt252):           {sdaily:.2f}")
    print(f"Sharpe (per-slot, sqrt35040):      {sann:.2f}  <- inflated, do not use for decisions")
    print()
    print(f"Win rate:                      {wr:.1f}%")
    print(f"Avg PnL per trade (with fees): ${avg_pnl:.2f}")
    print(f"Avg PnL per trade (no fees):   ${avg_pnl_nf:.2f}  <- this is what old backtest reported")
    print(f"Fee drag per trade:            -${avg_fee:.2f}")
    print()
    print(f"Total trades:                  {total:,}")
    print(f"Total PnL (with fees):        ${pnl:.2f}")
    print(f"Total PnL (no fees):          ${pnl_nf:.2f}  <- old backtest reported this")
    print()
    _asset_rc = r.get("asset", "BTC")
    _pre23_path_rc = os.path.join(_BV3_DIR, f"{_asset_rc}_bv3_pre2023.json")
    if os.path.exists(_pre23_path_rc):
        print(f"BV3 data leakage:             NO  -- pre-2023 table used (honest baseline)")
    else:
        print(f"BV3 data leakage:             YES -- OOS metrics unreliable (generate pre-2023 table)")
    if price_validated:
        print(f"Price model validated:         [OK] YES --   {pv_samples} samples in price_validation_log.csv")
    else:
        _s = f"{pv_samples}/200 samples" if pv_samples else "0 samples"
        print(f"Price model validated:         [X]  NO --   {_s}, run price_validator.py for 200+ trades")
    if wfv_varied is None:
        print(f"Walk-forward param variation:  NOT RUN --   use --walk-forward to check")
    elif wfv_varied:
        print(f"Walk-forward param variation:  YES --   params changed across windows")
    else:
        print(f"Walk-forward param variation:  NO --   params converged (edge may come from BV3 only)")
    print()

    # Verdicts -- all matching rules print, in priority order
    verdicts: list[str] = []
    if not price_validated:
        verdicts.append(
            "UNRELIABLE --   backtest uses simulated prices not compared to real Kalshi "
            "prices. Run paper mode with price_validator.py before trusting any results."
        )
    if spt > 3.0:
        verdicts.append(
            "SUSPICIOUS --   per-trade Sharpe above 3.0 suggests simulated prices are "
            "too favorable."
        )
    if avg_pnl < 0.0:
        verdicts.append(
            "NEGATIVE EV --   strategy loses money after fees with current parameters."
        )
    elif avg_pnl < 1.0:
        verdicts.append(
            "MARGINAL --   edge is small after fees. Sensitive to price model accuracy."
        )

    if verdicts:
        for v in verdicts:
            print(f"VERDICT: {v}")
    else:
        print("VERDICT: No major flags. Note BV3 leakage means OOS metrics are still unreliable.")
    print(BORDER)


# -----------------------------------------------------------------------------
# EV sweep - test multiple thresholds and rank them
# -----------------------------------------------------------------------------

def run_sweep(start_year: int, trade_amount: float) -> None:
    thresholds = [0.05, 0.10, 0.12, 0.15, 0.18, 0.20, 0.25, 0.30]
    results = []

    for ev in thresholds:
        print(f"\n-- Sweep: min_ev={ev:.0%} ------------------------------")
        r = run_backtest(
            start_year=start_year,
            min_ev=ev,
            trade_amount=trade_amount,
            verbose=False,
        )
        if r:
            results.append(r)
            print(f"  trades={r['total_trades']:,}  "
                  f"wr={r['win_rate']*100:.1f}%  "
                  f"pnl=${r['total_pnl_dollars']:.2f}  "
                  f"sharpe={r['sharpe_ratio']:.2f}  "
                  f"maxdd={r['max_drawdown_percent']:.1f}%")

    if not results:
        return

    print("\n" + "=" * 80)
    print("  SWEEP SUMMARY - ranked by Sharpe ratio")
    print("=" * 80)
    print(f"{'min_ev':>8}  {'trades':>8}  {'win_rate':>9}  {'pnl':>8}  "
          f"{'sharpe':>7}  {'max_dd':>7}  {'cons_loss':>9}")
    print("-" * 80)
    sorted_results = sorted(results, key=lambda x: -x["sharpe_ratio"])
    for r in sorted_results:
        print(f"  {r['min_ev']:>5.0%}  {r['total_trades']:>8,}  "
              f"{r['win_rate']*100:>8.1f}%  "
              f"${r['total_pnl_dollars']:>7.2f}  "
              f"{r['sharpe_ratio']:>7.3f}  "
              f"{r['max_drawdown_percent']:>6.1f}%  "
              f"{r['max_consecutive_losses']:>9}")
    # Show reality check for best result so sweep output includes all warnings
    if sorted_results:
        print(f"\n  REALITY CHECK for best sweep result (min_ev={sorted_results[0]['min_ev']:.0%}):")
        print_reality_check(sorted_results[0])


# -----------------------------------------------------------------------------
# Monte Carlo parameter search
# -----------------------------------------------------------------------------

MONTE_CARLO_OUT = os.path.join(_BASE_DIR, "monte_carlo_results.json")

PARAM_SPACE = {
    "min_ev":         [0.03, 0.05, 0.08, 0.10, 0.12, 0.15],
    "min_confidence": [60, 65, 70, 75, 80, 85],
}

# Pre-computed pool of all unique combinations (6 x 6 = 36)
_ALL_COMBOS = list(itertools.product(
    PARAM_SPACE["min_ev"],
    PARAM_SPACE["min_confidence"],
))


def run_monte_carlo(n_simulations: int = 10_000, start_year: int = 2020,
                    trade_amount: float = 5.0) -> None:
    """
    Randomly sample from PARAM_SPACE, run the full backtest for each sample,
    track the top-20 by Sharpe ratio, write results to monte_carlo_results.json,
    and print a final summary.

    ob_imbalance_thresh and min_confidence act as additional filters applied
    on top of the existing printer_brain logic: a trade is only taken when the
    simulated order-book imbalance exceeds the threshold AND the brain confidence
    is at or above the minimum.

    entry_window = (lo_sec, hi_sec) - a trade in a window must originate between
    lo_sec and hi_sec seconds into the 15-minute window (900 s total).
    """
    rng_mc = random.Random(0)          # reproducible sampling
    top20: list[dict] = []             # sorted best->worst by Sharpe
    all_results: list[dict] = []

    total_combos = len(_ALL_COMBOS)
    effective_n  = min(n_simulations, total_combos)
    print(f"\nMonte Carlo - {effective_n:,} simulations "
          f"({total_combos:,} unique combos, seed=0)")
    print(f"  start_year={start_year}, trade_amount=${trade_amount}")
    print("  Writing progress to:", MONTE_CARLO_OUT)
    print()

    # Load CSV once - reused across all simulations
    print("  Loading data (once)...")
    windows, price_lookup = load_data(start_year, verbose=True)
    print()

    # Shuffle once, then iterate - avoids duplicate runs while staying random
    combo_pool = list(_ALL_COMBOS)
    rng_mc.shuffle(combo_pool)
    combos_to_run = combo_pool[:effective_n]

    t0 = time.time()
    for i, (min_ev, min_confidence) in enumerate(combos_to_run, 1):

        r = run_backtest(
            min_ev         = min_ev,
            trade_amount   = trade_amount,
            min_confidence = min_confidence,
            verbose        = False,
            _windows       = windows,
            _price_lookup  = price_lookup,
        )

        if not r:
            continue

        r["params"] = {
            "min_ev":         min_ev,
            "min_confidence": min_confidence,
        }
        all_results.append(r)

        # Maintain top-20 sorted by Sharpe (descending)
        top20.append(r)
        top20.sort(key=lambda x: -x["sharpe_ratio"])
        if len(top20) > 20:
            top20.pop()

        # Progress every 50 runs
        if i % 50 == 0 or i == effective_n:
            elapsed = time.time() - t0
            eta     = (elapsed / i) * (effective_n - i)
            best_s  = top20[0]["sharpe_ratio"] if top20 else 0.0
            print(f"  [{i:>5}/{effective_n}]  "
                  f"elapsed={elapsed:.0f}s  eta={eta:.0f}s  "
                  f"best_sharpe={best_s:.3f}")

            # Incremental save so user can Ctrl-C early
            _save_mc_results(top20, all_results, i, effective_n)

    _save_mc_results(top20, all_results, effective_n, effective_n)
    _print_mc_summary(top20)


def _save_mc_results(top20: list, all_results: list,
                     done: int, total: int) -> None:
    """Write current top-20 + metadata to JSON."""
    payload = {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "simulations_done": done,
        "simulations_total": total,
        "top_20": [
            {
                "rank":              rank + 1,
                "sharpe_ratio":      r["sharpe_ratio"],
                "win_rate":          r["win_rate"],
                "total_pnl_dollars": r["total_pnl_dollars"],
                "total_trades":      r["total_trades"],
                "max_drawdown_pct":  r["max_drawdown_percent"],
                "params":            r["params"],
            }
            for rank, r in enumerate(top20)
        ],
    }
    try:
        with open(MONTE_CARLO_OUT, "w") as f:
            json.dump(payload, f, indent=2)
    except OSError as e:
        print(f"  [warn] could not write {MONTE_CARLO_OUT}: {e}")


def _print_mc_summary(top20: list) -> None:
    if not top20:
        print("\nNo valid results found.")
        return

    best = top20[0]
    W = 70
    print("\n" + "=" * W)
    print("  MONTE CARLO SUMMARY - top 20 by Sharpe ratio")
    print("=" * W)
    print(f"  {'Rank':>4}  {'Sharpe':>7}  {'WinRate':>8}  "
          f"{'PnL':>8}  {'Trades':>7}  {'MaxDD':>7}  params")
    print("-" * W)
    for rank, r in enumerate(top20, 1):
        p = r["params"]
        param_str = f"ev={p['min_ev']:.0%} conf={p['min_confidence']}"
        print(f"  {rank:>4}  {r['sharpe_ratio']:>7.3f}  "
              f"{r['win_rate']*100:>7.1f}%  "
              f"${r['total_pnl_dollars']:>7.2f}  "
              f"{r['total_trades']:>7,}  "
              f"{r['max_drawdown_percent']:>6.1f}%  {param_str}")
    print("=" * W)
    print(f"\n  BEST PARAMS:")
    bp = best["params"]
    print(f"    min_ev          = {bp['min_ev']:.0%}")
    print(f"    min_confidence  = {bp['min_confidence']}")
    print(f"\n  Expected win rate     : {best['win_rate']*100:.1f}%")
    ann_return = (best["total_pnl_dollars"] / (best["total_trades"] * 5.0) *
                  35_040 * 5.0) if best["total_trades"] else 0
    print(f"  Expected annual PnL   : ${ann_return:,.0f}  (at $5/trade, 35k slots/yr)")
    print(f"  Sharpe ratio          : {best['sharpe_ratio']:.3f}")
    print(f"\n  Full results saved to : {MONTE_CARLO_OUT}")


# -----------------------------------------------------------------------------
# OOS evaluation
# -----------------------------------------------------------------------------

def run_oos_eval(start_year: int = 2020, trade_amount: float = 5.0,
                 custom_ev: float | None = None,
                 custom_confidence: int | None = None,
                 custom_watch: int = 1) -> None:
    """
    Evaluate params on the locked OOS holdout set.

    Pass custom_ev / custom_confidence to test your own settings instead of
    the Monte Carlo best params.
    """
    split_cfg = _ensure_split(start_year)

    if custom_ev is not None or custom_confidence is not None:
        base = _load_best_params()
        params = {
            "min_ev":         custom_ev         if custom_ev         is not None else base["min_ev"],
            "min_confidence": custom_confidence if custom_confidence is not None else base["min_confidence"],
        }
    else:
        params = _load_best_params()

    print("\n" + "=" * 70)
    print("  OOS EVALUATION")
    print("=" * 70)
    src = "custom" if (custom_ev is not None or custom_confidence is not None) else "MC best"
    print(f"  Params ({src})  :  ev={params['min_ev']:.0%}  conf={params['min_confidence']}")
    print(f"  Train period :  {split_cfg['train_start_date']} -> "
          f"{split_cfg['train_end_date']}  ({split_cfg['train_windows']:,} windows)")
    print(f"  OOS period   :  {split_cfg['oos_start_date']} -> "
          f"{split_cfg['oos_end_date']}  ({split_cfg['oos_windows']:,} windows)")

    # Load full dataset once, then slice - avoids reading CSV twice
    print("\n  Loading full dataset ...")
    all_windows, all_pl = load_data(start_year, verbose=True, mode="full")

    train_w, train_pl = _filter_to(all_windows, all_pl, "train", split_cfg)
    oos_w,   oos_pl   = _filter_to(all_windows, all_pl, "oos",   split_cfg)

    print(f"\n  Running in-sample backtest  ({len(train_w):,} windows) ...")
    train_r = run_backtest(
        min_ev         = params["min_ev"],
        trade_amount   = trade_amount,
        min_confidence = params["min_confidence"],
        watch_minutes  = custom_watch,
        verbose        = False,
        _windows       = train_w,
        _price_lookup  = train_pl,
    )

    print(f"  Running OOS backtest        ({len(oos_w):,} windows) ...")
    oos_r = run_backtest(
        min_ev         = params["min_ev"],
        trade_amount   = trade_amount,
        min_confidence = params["min_confidence"],
        watch_minutes  = custom_watch,
        verbose        = False,
        _windows       = oos_w,
        _price_lookup  = oos_pl,
    )

    if not train_r or not oos_r:
        print("\n  [error] One or both backtests returned no trades.")
        return

    _print_oos_comparison(train_r, oos_r, split_cfg, params)
    _save_oos_report(train_r, oos_r, split_cfg, params)


def _print_oos_comparison(train_r: dict, oos_r: dict,
                           split_cfg: dict, params: dict) -> None:
    W = 72
    print("\n" + "=" * W)
    print("  IN-SAMPLE vs OOS HOLDOUT - side by side")
    print("=" * W)

    # Header row
    th = f"{'IN-SAMPLE':>20}"
    oh = f"{'OOS HOLDOUT':>16}"
    print(f"  {'Metric':<28}  {th}  {oh}")
    print("-" * W)

    def row(label, tv, ov, fmt):
        print(f"  {label:<28}  {fmt.format(tv):>20}  {fmt.format(ov):>16}")

    print(f"  {'Period':<28}  "
          f"{split_cfg['train_start_date']+' -> '+split_cfg['train_end_date']:>20}  "
          f"{split_cfg['oos_start_date']+' -> '+split_cfg['oos_end_date']:>16}")
    print(f"  {'Windows':<28}  {split_cfg['train_windows']:>20,}  "
          f"{split_cfg['oos_windows']:>16,}")
    row("Total trades",          train_r["total_trades"],       oos_r["total_trades"],       "{:,}")
    row("Win rate (%)",          train_r["win_rate"]*100,       oos_r["win_rate"]*100,       "{:.1f}%")
    row("Total P&L ($)",         train_r["total_pnl_dollars"],  oos_r["total_pnl_dollars"],  "${:.2f}")
    row("Sharpe ratio",          train_r["sharpe_ratio"],       oos_r["sharpe_ratio"],       "{:.3f}")
    row("Max drawdown (%)",      train_r["max_drawdown_percent"],oos_r["max_drawdown_percent"],"{:.1f}%")
    row("Avg confidence",        train_r["avg_confidence"],     oos_r["avg_confidence"],     "{:.1f}")
    row("Max consec losses",     train_r["max_consecutive_losses"],oos_r["max_consecutive_losses"],"{}")
    print("=" * W)

    # Generalisation efficiency - per-trade P&L basis (removes window-count bias)
    t_ppt = (train_r["total_pnl_dollars"] / train_r["total_trades"]
             if train_r["total_trades"] else 0.0)
    o_ppt = (oos_r["total_pnl_dollars"]   / oos_r["total_trades"]
             if oos_r["total_trades"]   else 0.0)
    eff   = round(o_ppt / t_ppt, 4) if t_ppt > 0 else 0.0

    print(f"\n  P&L per trade - in-sample: ${t_ppt:.4f}  |  OOS: ${o_ppt:.4f}")
    print(f"  Generalisation efficiency (OOS / in-sample P&L per trade): {eff:.2f}")
    if eff >= 0.70:
        verdict = "[OK] PASS"
        note    = "Strategy generalises well to unseen data."
    elif eff >= 0.50:
        verdict = "~ MARGINAL"
        note    = "Some degradation on OOS data (50-70%). Monitor closely."
    else:
        verdict = "[X] WARN - POSSIBLE OVERFIT"
        note    = "Significant OOS degradation (<50%). Consider re-tuning on more data."
    print(f"  Verdict  :  {verdict}")
    print(f"  Note     :  {note}")
    print()


def _save_oos_report(train_r: dict, oos_r: dict,
                     split_cfg: dict, params: dict) -> None:
    os.makedirs(_RESULTS_DIR, exist_ok=True)

    t_ppt = (train_r["total_pnl_dollars"] / train_r["total_trades"]
             if train_r["total_trades"] else 0.0)
    o_ppt = (oos_r["total_pnl_dollars"]   / oos_r["total_trades"]
             if oos_r["total_trades"]   else 0.0)
    eff   = round(o_ppt / t_ppt, 4) if t_ppt > 0 else 0.0

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params":       params,
        "split":        split_cfg,
        "in_sample": {
            "period":              (f"{split_cfg['train_start_date']} -> "
                                    f"{split_cfg['train_end_date']}"),
            "total_trades":        train_r.get("total_trades"),
            "win_rate":            train_r.get("win_rate"),
            "total_pnl_dollars":   train_r.get("total_pnl_dollars"),
            "pnl_per_trade":       round(t_ppt, 6),
            "sharpe_ratio":        train_r.get("sharpe_ratio"),
            "max_drawdown_pct":    train_r.get("max_drawdown_percent"),
            "avg_confidence":      train_r.get("avg_confidence"),
            "max_consec_losses":   train_r.get("max_consecutive_losses"),
        },
        "oos": {
            "period":              (f"{split_cfg['oos_start_date']} -> "
                                    f"{split_cfg['oos_end_date']}"),
            "total_trades":        oos_r.get("total_trades"),
            "win_rate":            oos_r.get("win_rate"),
            "total_pnl_dollars":   oos_r.get("total_pnl_dollars"),
            "pnl_per_trade":       round(o_ppt, 6),
            "sharpe_ratio":        oos_r.get("sharpe_ratio"),
            "max_drawdown_pct":    oos_r.get("max_drawdown_percent"),
            "avg_confidence":      oos_r.get("avg_confidence"),
            "max_consec_losses":   oos_r.get("max_consecutive_losses"),
        },
        "generalisation_efficiency": eff,
        "verdict": (
            "PASS"         if eff >= 0.70 else
            "MARGINAL"     if eff >= 0.50 else
            "WARN_OVERFIT"
        ),
    }

    with open(OOS_REPORT_PATH, "w") as fh:
        json.dump(report, fh, indent=2)
    print(f"  Report saved to: {OOS_REPORT_PATH}")


# -----------------------------------------------------------------------------
# Period comparison -- runs the current live strategy across multiple date ranges
# -----------------------------------------------------------------------------

def run_period_comparison(trade_amount: float = 25.0) -> None:
    """
    Run the current live strategy parameters across three date ranges and
    print a side-by-side comparison table.

    Live params replicated here:
      min_ev        = 0.05  (5% neutral floor; 3% US hours in live bot)
      min_confidence= 65    (config.json confidence_threshold)
      vol_threshold = 1.50  (skip if expected_move >= 1.5x strike distance)
      trade_amount  = $25   (config.json trade_amount_dollars)
    """
    MIN_EV      = 0.05
    MIN_CONF    = 65
    VOL_THRESH  = 1.50

    periods = [
        ("2020 -> 2026  (full history)", 2020, 2026),
        ("2024 -> 2025  (recent bull)",  2024, 2025),
        ("2025 -> 2026  (live regime)",  2025, 2026),
    ]

    results = []
    for label, sy, ey in periods:
        print(f"\n{'-'*60}")
        print(f"  Period: {label}")
        print(f"{'-'*60}")
        windows, price_lookup = load_data(
            start_year=sy, end_year=ey, verbose=True, mode="full"
        )
        if len(windows) == 0:
            print("  [!] No data for this period -- skipping.")
            continue
        r = run_backtest(
            start_year     = sy,
            end_year       = ey,
            min_ev         = MIN_EV,
            trade_amount   = trade_amount,
            min_confidence = MIN_CONF,
            vol_threshold  = VOL_THRESH,
            verbose        = True,
            _windows       = windows,
            _price_lookup  = price_lookup,
        )
        if r:
            r["label"] = label
            results.append(r)
            print_report(r)
            print_reality_check(r)

    if not results:
        return

    # == Annotate computed fields =============================================
    for r in results:
        t = r.get("total_trades", 0)
        w = r.get("total_windows", 1)
        r["_trade_rate"]   = t / w * 100 if w else 0
        r["_pnl_per_trade"] = r.get("total_pnl_dollars", 0) / t if t else 0
        # Annualised: 96 windows/day x 365 days = 35,040 slots/year
        r["_ann_pnl"] = r["_pnl_per_trade"] * 35040 if t else 0

    # == Side-by-side summary =================================================
    W = 78
    print("\n\n" + "=" * W)
    print("  PERIOD COMPARISON  --  current live strategy params")
    print(f"  min_ev={MIN_EV:.0%}  |  confidence>={MIN_CONF}  |  vol_gate>={VOL_THRESH}  |  ${trade_amount}/trade")
    print("=" * W)

    col = 22

    def row(label, key, fmt):
        line = f"  {label:<28}"
        for r in results:
            v = r.get(key, 0)
            line += f"  {fmt.format(v):>{col}}"
        print(line)

    hdr = f"  {'Metric':<28}"
    for r in results:
        lbl = r["label"].split("(")[0].strip()
        hdr += f"  {lbl:>{col}}"
    print(hdr)
    print("-" * W)

    row("Windows",           "total_windows",          "{:,}")
    row("Trades placed",     "total_trades",           "{:,}")
    row("Trade rate",        "_trade_rate",             "{:.1f}%")
    row("Win rate",          "win_rate",               "{:.1%}")
    row("Total P&L",         "total_pnl_dollars",      "${:,.2f}")
    row("P&L per trade",     "_pnl_per_trade",         "${:.4f}")
    row("Ann. P&L (est.)",   "_ann_pnl",               "${:,.0f}")
    row("Sharpe ratio",      "sharpe_ratio",           "{:.3f}")
    row("Max drawdown",      "max_drawdown_percent",   "{:.1f}%")
    row("Max consec losses", "max_consecutive_losses", "{}")
    row("Avg confidence",    "avg_confidence",         "{:.1f}")
    print("=" * W)
    print()


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backtest printer_brain strategy against historical BTC data"
    )
    parser.add_argument("--periods",       action="store_true",
                        help="Run period comparison: 2020-26 / 2024-25 / 2025-26 at live params")
    parser.add_argument("--start-year",   type=int,   default=2020,
                        help="First year of data to include (default: 2020)")
    parser.add_argument("--end-year",     type=int,   default=9999,
                        help="Last year of data to include (default: all)")
    parser.add_argument("--ev",           type=float, default=0.05,
                        help="Min EV threshold, e.g. 0.05 = 5%% (default: 0.05)")
    parser.add_argument("--amount",       type=float, default=25.0,
                        help="Trade amount in dollars (default: 25)")
    parser.add_argument("--confidence",   type=int,   default=65,
                        help="Min confidence %% to enter a trade (default: 65)")
    parser.add_argument("--watch",        type=int,   default=1,
                        help="Earliest minute to enter a trade (default: 1, try 8 or 9)")
    parser.add_argument("--sweep",        action="store_true",
                        help="Run multiple EV thresholds and compare results")
    parser.add_argument("--monte-carlo",    action="store_true",
                        help="Run Monte Carlo parameter search (10k simulations)")
    parser.add_argument("--mc-sims",        type=int,   default=10_000,
                        help="Number of Monte Carlo simulations (default: 10000)")
    parser.add_argument("--no-db",          action="store_true",
                        help="Skip writing to kalshi_bot.db")
    parser.add_argument("--oos-eval",       action="store_true",
                        help="Run best params on the locked OOS holdout set and "
                             "print in-sample vs OOS comparison")
    parser.add_argument("--oos-ev",         type=float, default=None,
                        help="Custom EV threshold for OOS eval (e.g. 0.20)")
    parser.add_argument("--oos-confidence", type=int,   default=None,
                        help="Custom confidence threshold for OOS eval (e.g. 80)")
    parser.add_argument("--oos-watch",      type=int,   default=1,
                        help="Earliest entry minute for OOS eval (default: 1)")
    parser.add_argument("--generate-split", action="store_true",
                        help="Generate (or regenerate) the 70/30 train/OOS split "
                             "config and exit  (saves data/split_config.json)")
    parser.add_argument("--walk-forward",   action="store_true",
                        help="Run Walk-Forward Validation over the train partition")
    parser.add_argument("--wf-windows",     type=int, default=8,
                        help="Number of WFV rolling windows (default: 8)")
    parser.add_argument("--wf-mc-sims",     type=int, default=50,
                        help="MC simulations per WFV window (default: 50)")
    parser.add_argument("--stress-test",    action="store_true",
                        help="Run execution noise stress test (slippage/latency/partial fills)")
    parser.add_argument("--st-iters",       type=int,   default=500,
                        help="Noise iterations for stress test (default: 500)")
    parser.add_argument("--st-max-slippage", type=float, default=20.0,
                        help="Max slippage in bps for stress test (default: 20)")
    parser.add_argument("--st-latency-ms",  type=int,   default=0,
                        help="Latency in ms for miss model (default: 0)")
    parser.add_argument("--asset",          nargs="+",  default=["BTC"],
                        metavar="ASSET",
                        help="Asset(s) to backtest: BTC ETH SOL XRP DOGE, or ALL "
                             "(default: BTC). Multiple assets print a combined summary.")
    parser.add_argument("--max-entry-price", type=float, default=100.0,
                        dest="max_entry_price",
                        help="Max entry price in cents (default: 100 = no filter). "
                             "E.g. --max-entry-price 82 to skip entries above 82c.")
    parser.add_argument("--min-reward",      type=float, default=0.0,
                        dest="min_reward",
                        help="Min reward in cents (100 - entry_c). Default: 0 = no filter.")
    parser.add_argument("--max-rr",          type=float, default=999.0,
                        dest="max_rr",
                        help="Max risk:reward ratio (entry / reward). Default: 999 = no filter.")
    parser.add_argument("--vol-gate",        type=float, default=1.50,
                        dest="vol_gate",
                        help="Skip window if expected_move / realized_vol >= this. Default: 1.50.")
    args = parser.parse_args()

    if args.periods:
        run_period_comparison(trade_amount=args.amount)
        sys.exit(0)

    if args.generate_split:
        _ensure_split(args.start_year)
        sys.exit(0)

    if args.walk_forward:
        import importlib.util as _ilu
        _wf = os.path.join(_BASE_DIR, "backtesting", "walk_forward.py")
        _spec = _ilu.spec_from_file_location("walk_forward", _wf)
        _mod  = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _mod.run_walk_forward(
            n_windows    = args.wf_windows,
            n_mc_sims    = args.wf_mc_sims,
            trade_amount = args.amount,
            start_year   = args.start_year,
            asset        = args.asset[0] if args.asset else "BTC",
        )
        sys.exit(0)

    if args.stress_test:
        import importlib.util as _ilu
        _st = os.path.join(_BASE_DIR, "backtesting", "stress_test.py")
        _spec = _ilu.spec_from_file_location("stress_test", _st)
        _mod  = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _mod.run_stress_test(
            n_iters          = args.st_iters,
            max_slippage_bps = args.st_max_slippage,
            latency_ms       = args.st_latency_ms,
            trade_amount     = args.amount,
            start_year       = args.start_year,
            asset            = args.asset[0] if args.asset else "BTC",
        )
        sys.exit(0)

    if args.oos_eval:
        run_oos_eval(
            start_year        = args.start_year,
            trade_amount      = args.amount,
            custom_ev         = args.oos_ev,
            custom_confidence = args.oos_confidence,
            custom_watch      = args.oos_watch,
        )
        sys.exit(0)

    if args.monte_carlo:
        run_monte_carlo(
            n_simulations = args.mc_sims,
            start_year    = args.start_year,
            trade_amount  = args.amount,
        )
    elif args.sweep:
        run_sweep(args.start_year, args.amount)
    else:
        # Resolve asset list -- "ALL" expands to every supported asset
        _raw_assets = args.asset
        if len(_raw_assets) == 1 and _raw_assets[0].upper() == "ALL":
            requested_assets = ALL_ASSETS
        else:
            requested_assets = [a.upper() for a in _raw_assets]
            for _a in requested_assets:
                if _a not in STRIKE_INCREMENTS:
                    print(f"Unknown asset '{_a}'. Choices: {', '.join(ALL_ASSETS)} or ALL")
                    sys.exit(1)

        all_results: list[dict] = []
        for _asset in requested_assets:
            print(f"\n{'='*70}")
            print(f"  BACKTEST: {_asset}  (pre-2023 BV3 table, no data leakage)")
            print(f"{'='*70}")
            _r = run_backtest(
                start_year            = args.start_year,
                end_year              = args.end_year,
                min_ev                = args.ev,
                trade_amount          = args.amount,
                min_confidence        = args.confidence,
                watch_minutes         = args.watch,
                asset                 = _asset,
                max_entry_price_cents = args.max_entry_price,
                min_reward_cents      = args.min_reward,
                max_risk_reward_ratio = args.max_rr,
                vol_threshold         = args.vol_gate,
            )
            if _r:
                print_report(_r)
                print_reality_check(_r)
                all_results.append(_r)
                if not args.no_db:
                    write_to_db(_r, args.start_year)
                    print(f"\nResults saved to {DB_PATH} -> stress_test_results table.")

        # Combined summary when multiple assets were run
        if len(all_results) > 1:
            print(f"\n{'='*70}")
            print("  COMBINED REALITY CHECK -- ALL ASSETS")
            print(f"{'='*70}")
            W = 70
            print(f"  {'Asset':<8}  {'Trades':>7}  {'Win%':>6}  {'P&L':>10}  {'Sharpe':>8}  {'MaxDD%':>7}")
            print(f"  {'-'*8}  {'-'*7}  {'-'*6}  {'-'*10}  {'-'*8}  {'-'*7}")
            combined_pnl = 0.0
            combined_trades = 0
            combined_wins = 0
            for _r in all_results:
                _a     = _r.get("asset", "?")
                _n     = _r.get("total_trades", 0)
                _wr    = _r.get("win_rate", 0.0) * 100
                _pnl   = _r.get("total_pnl_dollars", 0.0)
                _sh    = _r.get("sharpe_daily", 0.0)
                _dd    = _r.get("max_drawdown_percent", 0.0)
                combined_pnl    += _pnl
                combined_trades += _n
                combined_wins   += _r.get("wins", 0)
                print(f"  {_a:<8}  {_n:>7,}  {_wr:>5.1f}%  ${_pnl:>9,.2f}  {_sh:>8.3f}  {_dd:>6.1f}%")
            print(f"  {'-'*8}  {'-'*7}  {'-'*6}  {'-'*10}  {'-'*8}  {'-'*7}")
            _cwr = combined_wins / combined_trades * 100 if combined_trades else 0
            print(f"  {'TOTAL':<8}  {combined_trades:>7,}  {_cwr:>5.1f}%  ${combined_pnl:>9,.2f}")
            print()
            print("NOTE: Uses pre-2023 BV3 tables for all assets -- honest out-of-sample baseline.")
            print(f"{'='*70}")
