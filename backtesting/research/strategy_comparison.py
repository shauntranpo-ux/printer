"""
Strategy Comparison -- vectorized, runs in ~2 min.

Baseline  : D3-hybrid 3-of-5, ETH/SOL/XRP
S1        : DOGE enablement
S2        : Time-of-day filter (best N hours by training IC)
S3        : Strike proximity gate (price >= X% from open)
S4        : Volume surge gate (vol > 30-bar rolling median)
S5        : 4-of-5 vote threshold

Usage:
    py backtesting/research/strategy_comparison.py
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd
from scipy.stats import norm as _norm

_THIS_DIR     = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_THIS_DIR, "..", ".."))
_SRC_DIR      = os.path.join(_PROJECT_ROOT, "src")
for _p in [_PROJECT_ROOT, _SRC_DIR]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtesting.data.loaders import load_bars

# ---------------------------------------------------------------------------
HOLDOUT_DAYS = 90
WINDOW_MIN   = 15
SECONDS_LEFT = 600.0
WARM_BARS    = 60       # bars needed before first valid signal

BASE_ASSETS = ["ETH", "SOL", "XRP"]
NO_ONLY     = {"SOL", "XRP", "DOGE"}

_MTF_T  = {"ETH": 0.0005, "SOL": 0.0005, "XRP": 0.0005, "DOGE": 0.0005}
_RSI_T  = {"ETH": 8.0,    "SOL": 10.0,   "XRP": 8.0,    "DOGE": 8.0}
_BOLL_T = {"ETH": 0.50,   "SOL": 0.50,   "XRP": 0.35,   "DOGE": 0.50}

# Baseline weekly $ from validated EV sweep (660-day period)
_BASELINE_WEEKLY = {"ETH": 72.6, "SOL": 75.3, "XRP": 66.7}
_BASELINE_TRADES = {"ETH": 105,  "SOL": 51,   "XRP": 46}   # per week

# ---------------------------------------------------------------------------

def _ema_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    ag    = gain.ewm(com=period - 1, min_periods=period).mean()
    al    = loss.ewm(com=period - 1, min_periods=period).mean()
    rs    = ag / al.replace(0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def build_features(bars: pd.DataFrame, asset: str) -> pd.DataFrame:
    """Vectorised feature computation — O(n) pandas ops, no Python loops."""
    c  = bars["close"].astype(float)
    ts = pd.to_datetime(bars["timestamp"], utc=True)
    vol_col = bars["volume"].astype(float) if "volume" in bars.columns else pd.Series(np.nan, index=c.index)

    # Log-return realized vol (60-bar)
    lr     = np.log(c / c.shift(1))
    rvol   = lr.rolling(60, min_periods=30).std()

    # BS p_yes:  current_price = c.shift(1), strike = c
    mins   = SECONDS_LEFT / 60.0
    sigma  = rvol * math.sqrt(mins)
    d      = np.log(c.shift(1) / c) / sigma.replace(0, np.nan)
    bs     = pd.Series(_norm.cdf(d.values), index=c.index)

    # MTF momentum (inverted — mean reversion)
    r5  = (c - c.shift(5))  / c.shift(5).replace(0, np.nan)
    r15 = (c - c.shift(15)) / c.shift(15).replace(0, np.nan)
    r30 = (c - c.shift(30)) / c.shift(30).replace(0, np.nan)
    mtf = (r5 + r15 + r30) / 3.0

    # RSI deviation from 50
    rsi_dev = _ema_rsi(c, 14) - 50.0

    # Bollinger z-score
    bm    = c.rolling(20, min_periods=20).mean()
    bs_   = c.rolling(20, min_periods=20).std()
    boll  = (c - bm) / bs_.replace(0, np.nan)

    # Forward label: close[i + WINDOW_MIN - 1] > close[i]
    label = (c.shift(-(WINDOW_MIN - 1)) > c).astype(float)

    # Strike proximity: |close[i] - close[i-1]| / close[i-1]  (open of 15m window = prev close)
    # We define "deviation from strike" as the absolute % move of c vs c.shift(1)
    strike_dev = (c - c.shift(1)).abs() / c.shift(1).replace(0, np.nan)

    # Volume vs rolling median
    vol_median = vol_col.rolling(30, min_periods=10).median()
    vol_above  = (vol_col > vol_median).astype(float)

    # Hour of day
    hour = ts.dt.hour

    mtf_t  = _MTF_T.get(asset, 0.0005)
    rsi_t  = _RSI_T.get(asset, 8.0)
    boll_t = _BOLL_T.get(asset, 0.50)

    # Votes (vectorised)
    v1 = np.where(bs > 0.5,   1, -1).astype(float)
    v2 = np.where(mtf >  mtf_t, -1, np.where(mtf < -mtf_t, 1, 0)).astype(float)
    v3 = np.where(rsi_dev >  rsi_t, -1, np.where(rsi_dev < -rsi_t, 1, 0)).astype(float)
    v4 = np.where(boll >  boll_t, -1, np.where(boll < -boll_t, 1, 0)).astype(float)
    v5 = np.where((np.abs(mtf.values) > mtf_t / 2) & (mtf.values > 0), -1,
         np.where((np.abs(mtf.values) > mtf_t / 2) & (mtf.values < 0), 1, 0)).astype(float)

    yes_v = v1 + (v2 == 1) + (v3 == 1) + (v4 == 1) + (v5 == 1)
    no_v  = (v1 == -1).astype(float) + (v2 == -1) + (v3 == -1) + (v4 == -1) + (v5 == -1)

    signal_3 = np.where(yes_v >= 3,  1, np.where(no_v >= 3, -1, 0)).astype(float)
    signal_4 = np.where(yes_v >= 4,  1, np.where(no_v >= 4, -1, 0)).astype(float)

    out = pd.DataFrame({
        "timestamp":   ts.values,
        "close":       c.values,
        "label":       label.values,
        "signal_3":    signal_3,
        "signal_4":    signal_4,
        "hour":        hour.values,
        "strike_dev":  strike_dev.values,
        "vol_above":   vol_above.values,
        "bs":          bs.values,
        "mtf":         mtf.values,
        "rsi_dev":     rsi_dev.values,
        "boll":        boll.values,
        "rvol":        rvol.values,
    }, index=c.index)

    return out


def eval_signal(df: pd.DataFrame, sig_col: str, asset: str,
                mask: pd.Series | None = None) -> dict:
    """Compute IC, WR_YES, WR_NO, fire_rate on rows where signal != 0."""
    d = df if mask is None else df[mask]
    fired = d[d[sig_col] != 0].copy()
    if len(fired) < 50:
        return None

    ic       = float(fired[sig_col].corr(fired["label"], method="spearman"))
    yes_rows = fired[fired[sig_col] == 1]
    no_rows  = fired[fired[sig_col] == -1]
    wr_yes   = float(yes_rows["label"].mean()) if len(yes_rows) > 10 else float("nan")
    wr_no    = float(1 - no_rows["label"].mean()) if len(no_rows) > 10 else float("nan")
    fire_pct = len(fired) / max(len(d), 1)
    yes_pct  = len(yes_rows) / max(len(fired), 1)

    return {
        "n":        len(fired),
        "ic":       ic,
        "wr_yes":   wr_yes,
        "wr_no":    wr_no,
        "fire_pct": fire_pct,
        "yes_pct":  yes_pct,
    }


def pnl_estimate(r: dict, asset: str, n_holdout_days: int = 90) -> float:
    """
    Estimate weekly $ at $25/trade.
    Uses the same per-unit formula as the EV sweep:
      pnl_unit = WR_N * (1 - fill) - (1-WR_N) * fill - fee
    Assumes NO-only for SOL/XRP/DOGE, YES+NO for ETH.
    fill assumed = 0.40 for NO, 0.50 for YES.
    fee = 0.007
    """
    if r is None:
        return 0.0
    trades_per_week = r["n"] / n_holdout_days * 7

    if asset in NO_ONLY:
        wr = r.get("wr_no", 0.5)
        if math.isnan(wr):
            wr = 0.5
        fill, payout = 0.40, 0.60
        pnl_unit = wr * payout - (1 - wr) * fill - 0.007
    else:
        # ETH: mix of YES and NO; approximate with WR_NO for simplicity
        # (EV sweep showed ETH NO is the better side too)
        wr = r.get("wr_no", 0.5)
        if math.isnan(wr):
            wr = 0.5
        fill, payout = 0.40, 0.60
        pnl_unit = wr * payout - (1 - wr) * fill - 0.007

    return pnl_unit * 25.0 * trades_per_week


def load_holdout(asset: str) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """Returns (full_feat, holdout_feat, n_holdout_days)."""
    bars = load_bars(asset, check_min_history=False)
    bars = bars.sort_values("timestamp").reset_index(drop=True)
    feat = build_features(bars, asset)

    # Drop warmup rows (no valid signals) and rows that would look forward past end
    feat = feat.dropna(subset=["signal_3", "label", "rvol"])
    feat = feat[feat["label"].notna()]

    ts       = pd.to_datetime(feat["timestamp"], utc=True)
    cutoff   = ts.max() - pd.Timedelta(days=HOLDOUT_DAYS)
    train    = feat[ts < cutoff]
    holdout  = feat[ts >= cutoff]
    n_days   = int((ts.max() - ts[holdout.index[0]]).days) + 1 if len(holdout) else HOLDOUT_DAYS

    return train, holdout, n_days


# ---------------------------------------------------------------------------
# Strategy runners
# ---------------------------------------------------------------------------

def run_baseline(assets=None):
    assets = assets or BASE_ASSETS
    results = {}
    for asset in assets:
        print(f"  Loading {asset}...", end=" ", flush=True)
        _, ho, nd = load_holdout(asset)
        r = eval_signal(ho, "signal_3", asset)
        weekly = pnl_estimate(r, asset, nd)
        results[asset] = {**r, "weekly": weekly, "nd": nd}
        ic_str  = f"{r['ic']:+.4f}" if r else "n/a"
        wn_str  = f"{r['wr_no']:.1%}" if r and not math.isnan(r['wr_no']) else "n/a"
        print(f"IC={ic_str}  WR_N={wn_str}  n={r['n']:,}  est_wk=${weekly:.0f}")
    return results


def run_s1_doge():
    asset = "DOGE"
    print(f"  Loading {asset}...", end=" ", flush=True)
    _, ho, nd = load_holdout(asset)
    r = eval_signal(ho, "signal_3", asset)
    weekly = pnl_estimate(r, asset, nd)
    ic_str = f"{r['ic']:+.4f}" if r else "n/a"
    wn_str = f"{r['wr_no']:.1%}" if r and not math.isnan(r['wr_no']) else "n/a"
    print(f"IC={ic_str}  WR_N={wn_str}  n={r['n']:,}  est_wk=${weekly:.0f}")
    return {asset: {**r, "weekly": weekly, "nd": nd}} if r else {}


def run_s2_tod(assets, baseline):
    """Find best N hours by training IC, apply to holdout."""
    best_weekly = {}
    for asset in assets:
        print(f"  {asset}...", end=" ", flush=True)
        train, ho, nd = load_holdout(asset)

        # Compute per-hour IC on training set
        hour_ic = {}
        for h in range(24):
            mask = train["hour"] == h
            r = eval_signal(train, "signal_3", asset, mask)
            if r:
                hour_ic[h] = r["ic"]

        sorted_hours = sorted(hour_ic, key=lambda h: hour_ic[h], reverse=True)
        best = {}
        for n_hours in [8, 12, 16]:
            top_hours = set(sorted_hours[:n_hours])
            ho_mask   = ho["hour"].isin(top_hours)
            r = eval_signal(ho, "signal_3", asset, ho_mask)
            wk = pnl_estimate(r, asset, nd)
            best[n_hours] = (wk, r)

        best_n, (best_wk, best_r) = max(best.items(), key=lambda x: x[1][0])
        wn_str = f"{best_r['wr_no']:.1%}" if best_r and not math.isnan(best_r.get('wr_no', float('nan'))) else "n/a"
        print(f"best={best_n}h  WR_N={wn_str}  n={best_r['n']:,}  est_wk=${best_wk:.0f}  (top hours: {sorted(sorted_hours[:best_n])})")
        best_weekly[asset] = {"weekly": best_wk, "n_hours": best_n, **(best_r or {})}

    return best_weekly


def run_s3_strike(assets, baseline):
    """Only trade when price has moved >= X% from previous bar (proxy for strike dev)."""
    best_weekly = {}
    for asset in assets:
        print(f"  {asset}...", end=" ", flush=True)
        _, ho, nd = load_holdout(asset)
        best_wk, best_thresh, best_r = 0, 0, None
        for thresh in [0.001, 0.002, 0.003, 0.005, 0.010]:
            mask = ho["strike_dev"] >= thresh
            r = eval_signal(ho, "signal_3", asset, mask)
            wk = pnl_estimate(r, asset, nd)
            if wk > best_wk:
                best_wk, best_thresh, best_r = wk, thresh, r
        wn_str = f"{best_r['wr_no']:.1%}" if best_r and not math.isnan(best_r.get('wr_no', float('nan'))) else "n/a"
        n_str  = f"{best_r['n']:,}" if best_r else "n/a"
        print(f"best_dev>={best_thresh:.1%}  WR_N={wn_str}  n={n_str}  est_wk=${best_wk:.0f}")
        best_weekly[asset] = {"weekly": best_wk, "thresh": best_thresh, **(best_r or {})}
    return best_weekly


def run_s4_volume(assets, baseline):
    """Only trade when volume is above rolling 30-bar median."""
    best_weekly = {}
    for asset in assets:
        print(f"  {asset}...", end=" ", flush=True)
        _, ho, nd = load_holdout(asset)
        mask = ho["vol_above"] == 1
        r    = eval_signal(ho, "signal_3", asset, mask)
        wk   = pnl_estimate(r, asset, nd)
        wn_str = f"{r['wr_no']:.1%}" if r and not math.isnan(r.get('wr_no', float('nan'))) else "n/a"
        n_str  = f"{r['n']:,}" if r else "n/a"
        print(f"WR_N={wn_str}  n={n_str}  est_wk=${wk:.0f}")
        best_weekly[asset] = {"weekly": wk, **(r or {})}
    return best_weekly


def run_s5_4of5(assets, baseline):
    """Require 4-of-5 votes instead of 3-of-5."""
    best_weekly = {}
    for asset in assets:
        print(f"  {asset}...", end=" ", flush=True)
        _, ho, nd = load_holdout(asset)
        r  = eval_signal(ho, "signal_4", asset)
        wk = pnl_estimate(r, asset, nd)
        wn_str = f"{r['wr_no']:.1%}" if r and not math.isnan(r.get('wr_no', float('nan'))) else "n/a"
        n_str  = f"{r['n']:,}" if r else "n/a"
        print(f"WR_N={wn_str}  n={n_str}  est_wk=${wk:.0f}")
        best_weekly[asset] = {"weekly": wk, **(r or {})}
    return best_weekly


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _sum_wk(d: dict) -> float:
    return sum(v.get("weekly", 0) for v in d.values())


def main():
    print("=" * 65)
    print("Strategy Comparison  (D3-hybrid baseline vs 5 enhancements)")
    print("=" * 65)

    print("\n[Baseline] ETH / SOL / XRP")
    base = run_baseline(BASE_ASSETS)
    base_total = _sum_wk(base)
    print(f"  --> Baseline total: ${base_total:.0f}/wk\n")

    print("[S1] DOGE enablement")
    s1 = run_s1_doge()
    s1_total = _sum_wk(s1)
    s1_doge_pass = bool(s1 and s1.get("DOGE", {}).get("wr_no", 0) >= 0.50)
    print(f"  --> DOGE adds: ${s1_total:.0f}/wk  (pass={s1_doge_pass})\n")

    all_assets = BASE_ASSETS + (["DOGE"] if s1_doge_pass else [])

    print("[S2] Time-of-day filter (best N hours by training IC)")
    s2 = run_s2_tod(all_assets, base)
    s2_total = _sum_wk(s2)
    s2_delta = s2_total - (base_total + (s1_total if s1_doge_pass else 0))
    print(f"  --> S2 combined: ${s2_total:.0f}/wk  ({s2_delta:+.0f} vs baseline+DOGE)\n")

    print("[S3] Strike proximity gate (only trade when price moved >= X%)")
    s3 = run_s3_strike(all_assets, base)
    s3_total = _sum_wk(s3)
    s3_delta = s3_total - (base_total + (s1_total if s1_doge_pass else 0))
    print(f"  --> S3 combined: ${s3_total:.0f}/wk  ({s3_delta:+.0f} vs baseline+DOGE)\n")

    print("[S4] Volume surge gate (vol > 30-bar rolling median)")
    s4 = run_s4_volume(all_assets, base)
    s4_total = _sum_wk(s4)
    s4_delta = s4_total - (base_total + (s1_total if s1_doge_pass else 0))
    print(f"  --> S4 combined: ${s4_total:.0f}/wk  ({s4_delta:+.0f} vs baseline+DOGE)\n")

    print("[S5] 4-of-5 vote threshold (higher conviction)")
    s5 = run_s5_4of5(all_assets, base)
    s5_total = _sum_wk(s5)
    s5_delta = s5_total - (base_total + (s1_total if s1_doge_pass else 0))
    print(f"  --> S5 combined: ${s5_total:.0f}/wk  ({s5_delta:+.0f} vs baseline+DOGE)\n")

    # ----- Summary table -----
    print("=" * 65)
    print(f"{'Strategy':<28} {'Weekly $':>9}  {'vs Baseline':>11}  {'vs Target':>9}")
    print("-" * 65)
    rows = [
        ("Baseline (ETH+SOL+XRP)",         base_total),
        ("S1: + DOGE",                      base_total + s1_total),
        ("S2: TOD filter (all assets)",     s2_total),
        ("S3: Strike proximity (all)",      s3_total),
        ("S4: Volume surge (all)",          s4_total),
        ("S5: 4-of-5 votes (all)",          s5_total),
        ("S1+S2: DOGE + TOD",              base_total + s1_total + s2_delta),
        ("S1+S3: DOGE + Strike",           base_total + s1_total + s3_delta),
        ("S1+S4: DOGE + Volume",           base_total + s1_total + s4_delta),
        ("S1+S5: DOGE + 4-of-5",           base_total + s1_total + s5_delta),
    ]
    for name, wk in rows:
        delta = wk - base_total
        gap   = wk - 1000.0
        print(f"  {name:<26} ${wk:>8.0f}  {delta:>+10.0f}   {gap:>+8.0f}")

    print()
    combos = [(n, w) for n, w in rows]
    best_name, best_wk = max(combos, key=lambda x: x[1])
    print(f"WINNER: {best_name}  --> ${best_wk:.0f}/wk")
    gap = 1000.0 - best_wk
    if gap <= 0:
        print("TARGET $1000/wk ACHIEVED.")
    else:
        print(f"Gap to $1000/wk target: ${gap:.0f}")
        pct = best_wk / base_total
        print(f"Best combo is {pct:.1f}x baseline. Remaining gap likely needs signal IC improvement or more assets.")
    print()


if __name__ == "__main__":
    main()
