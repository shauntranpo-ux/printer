#!/usr/bin/env python3
"""
data/splitter.py — Deterministic chronological train / OOS holdout split.

Divides all 15-minute windows into:
  70%  TRAIN   — used for all backtesting and Monte Carlo optimisation
  30%  OOS     — locked holdout, never touched during parameter tuning

Split boundaries are saved to data/split_config.json.  Running this script
again with the same --start-year always produces the identical split.

Usage
-----
    python data/splitter.py                     # generate (start_year=2020)
    python data/splitter.py --start-year 2022   # custom start year
    python data/splitter.py --force             # regenerate even if config exists
"""

import argparse
import json
import os
import time

import pandas as pd

# ── Paths ─────────────────────────────────────────────────────────────────────
_THIS_DIR         = os.path.dirname(os.path.abspath(__file__))
CSV_PATH          = r"C:\Users\alxnt\Downloads\btcusd_1-min_data.csv"
SPLIT_CONFIG_PATH = os.path.join(_THIS_DIR, "split_config.json")
TRAIN_RATIO       = 0.70


# ── Public API ────────────────────────────────────────────────────────────────

def generate_split(start_year: int = 2020, force: bool = False) -> dict:
    """
    Load the BTC CSV, derive all 15-min window timestamps, split 70/30
    chronologically, and write the boundaries to data/split_config.json.

    Returns the split config dict.  If the config already exists and
    force=False the existing config is returned without re-reading the CSV
    (deterministic: same inputs ->same split every run).
    """
    if os.path.exists(SPLIT_CONFIG_PATH) and not force:
        cfg = _load_json(SPLIT_CONFIG_PATH)
        _print_existing(cfg)
        return cfg

    print(f"[splitter] Loading {CSV_PATH} ...")
    t0 = time.time()

    df = pd.read_csv(
        CSV_PATH,
        usecols=["Timestamp", "Open", "Close"],
        dtype={"Timestamp": "float64", "Open": "float64", "Close": "float64"},
        engine="c",
    )
    df["ts"] = df["Timestamp"].astype("int64")
    df = df.dropna(subset=["Open", "Close"])
    df = df[df["Close"] > 0]
    df = df[pd.to_datetime(df["ts"], unit="s").dt.year >= start_year].copy()
    df["window_start"] = (df["ts"] // 900) * 900

    all_ws  = sorted(df["window_start"].unique())
    n_total = len(all_ws)
    if n_total == 0:
        raise ValueError(f"No windows found for start_year={start_year}. Check CSV_PATH.")

    n_train = int(n_total * TRAIN_RATIO)
    n_oos   = n_total - n_train

    train_ws = all_ws[:n_train]
    oos_ws   = all_ws[n_train:]

    def _d(ts: int) -> str:
        return pd.Timestamp(ts, unit="s", tz="UTC").strftime("%Y-%m-%d")

    cfg = {
        "generated_at":      pd.Timestamp.now(tz="UTC").isoformat(),
        "start_year":        start_year,
        "train_ratio":       TRAIN_RATIO,
        "total_windows":     n_total,
        "train_windows":     n_train,
        "oos_windows":       n_oos,
        "train_start_ts":    int(train_ws[0]),
        "train_end_ts":      int(train_ws[-1]),
        "oos_start_ts":      int(oos_ws[0]),
        "oos_end_ts":        int(oos_ws[-1]),
        "train_start_date":  _d(train_ws[0]),
        "train_end_date":    _d(train_ws[-1]),
        "oos_start_date":    _d(oos_ws[0]),
        "oos_end_date":      _d(oos_ws[-1]),
    }

    os.makedirs(_THIS_DIR, exist_ok=True)
    with open(SPLIT_CONFIG_PATH, "w") as fh:
        json.dump(cfg, fh, indent=2)

    elapsed = time.time() - t0
    print(f"[splitter] Split generated in {elapsed:.1f}s")
    print(f"  Total  : {n_total:>8,} windows")
    print(f"  Train  : {n_train:>8,} windows  "
          f"[{cfg['train_start_date']} ->{cfg['train_end_date']}]")
    print(f"  OOS    : {n_oos:>8,} windows  "
          f"[{cfg['oos_start_date']} ->{cfg['oos_end_date']}]")
    print(f"  Saved  : {SPLIT_CONFIG_PATH}")
    return cfg


def load_split_config() -> dict | None:
    """Return existing split config, or None if not yet generated."""
    if not os.path.exists(SPLIT_CONFIG_PATH):
        return None
    return _load_json(SPLIT_CONFIG_PATH)


def filter_windows(windows, price_lookup, mode: str, split_cfg: dict):
    """
    Slice a (windows DataFrame, price_lookup Series) pair to the
    'train' or 'oos' portion defined by split_cfg.

    Parameters
    ----------
    windows      : pd.DataFrame indexed by window_start (int Unix seconds)
    price_lookup : pd.Series   indexed by window_start
    mode         : 'train' or 'oos'
    split_cfg    : dict from load_split_config() / generate_split()

    Returns
    -------
    (filtered_windows, filtered_price_lookup)
    """
    if mode not in ("train", "oos"):
        raise ValueError(f"mode must be 'train' or 'oos', got {mode!r}")

    if mode == "train":
        ts_lo, ts_hi = split_cfg["train_start_ts"], split_cfg["train_end_ts"]
    else:
        ts_lo, ts_hi = split_cfg["oos_start_ts"],   split_cfg["oos_end_ts"]

    mask   = (windows.index >= ts_lo) & (windows.index <= ts_hi)
    w_out  = windows[mask]
    pl_out = price_lookup[price_lookup.index.isin(w_out.index)]
    return w_out, pl_out


# ── Internal helpers ──────────────────────────────────────────────────────────

def _load_json(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def _print_existing(cfg: dict) -> None:
    print("[splitter] Loaded existing split config  (pass --force to regenerate)")
    print(f"  Train  : {cfg['train_windows']:>8,} windows  "
          f"[{cfg['train_start_date']} ->{cfg['train_end_date']}]")
    print(f"  OOS    : {cfg['oos_windows']:>8,} windows  "
          f"[{cfg['oos_start_date']} ->{cfg['oos_end_date']}]")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Generate deterministic 70/30 train/OOS split for BTC backtest data"
    )
    ap.add_argument("--start-year", type=int, default=2020,
                    help="First year of data to include (default: 2020)")
    ap.add_argument("--force", action="store_true",
                    help="Regenerate split even if split_config.json already exists")
    args = ap.parse_args()
    generate_split(start_year=args.start_year, force=args.force)
