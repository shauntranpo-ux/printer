"""
IC sweep — D3-hybrid voters on real 1-minute OHLCV data.

Computes Spearman IC for each voter and the ensemble signal against
the 15-minute binary label (close > open at t+15min).

Usage:
    py backtesting/research/ic_hunt_real.py
"""
from __future__ import annotations

import math
import os
import sys

import pandas as pd

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_THIS_DIR, "..", ".."))
_SRC_DIR = os.path.join(_PROJECT_ROOT, "src")
for _p in [_PROJECT_ROOT, _SRC_DIR]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtesting.data.loaders import load_bars
from strategies.signals.black_scholes import compute_bs_p_yes

ASSETS = ["BTC", "ETH", "SOL", "XRP"]
HISTORY_BARS = 60
SECONDS_LEFT = 600.0
WINDOW_MIN = 15

_MTF_THRESHOLDS = {"BTC": 0.0027, "ETH": 0.0033, "SOL": 0.0042, "XRP": 0.0037}
_MTF_DEFAULT = 0.003
_RSI_PERIOD = 14
_BOLL_PERIOD = 20
_RSI_THRESHOLD = 10.0


def _rsi(prices: list, period: int = _RSI_PERIOD):
    if len(prices) < period + 2:
        return None
    changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains  = [max(0.0, c) for c in changes]
    losses = [max(0.0, -c) for c in changes]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def _boll_z(prices: list, period: int = _BOLL_PERIOD):
    if len(prices) < period:
        return None
    recent = prices[-period:]
    mean = sum(recent) / len(recent)
    var = sum((p - mean) ** 2 for p in recent) / (len(recent) - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    if std <= 0:
        return None
    return (prices[-1] - mean) / std


def _mtf_mom(prices: list):
    if len(prices) < 31:
        return None
    cur = prices[-1]
    if cur <= 0:
        return None
    r5  = (cur - prices[-6])  / prices[-6]  if prices[-6]  > 0 else 0.0
    r15 = (cur - prices[-16]) / prices[-16] if prices[-16] > 0 else 0.0
    r30 = (cur - prices[-31]) / prices[-31] if prices[-31] > 0 else 0.0
    return (r5 + r15 + r30) / 3.0


def _vol_1min(prices: list) -> float:
    if len(prices) < 2:
        return 0.0
    log_rets = [
        math.log(prices[i] / prices[i - 1])
        for i in range(1, len(prices))
        if prices[i] > 0 and prices[i - 1] > 0
    ]
    if len(log_rets) < 2:
        return 0.0
    mean = sum(log_rets) / len(log_rets)
    var = sum((r - mean) ** 2 for r in log_rets) / (len(log_rets) - 1)
    return math.sqrt(var)


def _compute_features(prices: list, asset: str, strike: float):
    if len(prices) < 32:
        return None

    vol = _vol_1min(prices)
    current = prices[-1]

    bs_p = compute_bs_p_yes(current, strike, vol, SECONDS_LEFT)
    if bs_p is None:
        return None

    mtf_T = _MTF_THRESHOLDS.get(asset, _MTF_DEFAULT)
    mtf = _mtf_mom(prices)
    rsi_val = _rsi(prices)
    rsi_dev = (float(rsi_val) - 50.0) if rsi_val is not None else None
    boll = _boll_z(prices)

    v1 = +1 if bs_p > 0.5 else -1
    v2 = (+1 if mtf is not None and float(mtf) >  mtf_T else
          (-1 if mtf is not None and float(mtf) < -mtf_T else 0))
    v3 = (+1 if rsi_dev is not None and rsi_dev >  _RSI_THRESHOLD else
          (-1 if rsi_dev is not None and rsi_dev < -_RSI_THRESHOLD else 0))
    v4 = (+1 if boll is not None and float(boll) >  0.5 else
          (-1 if boll is not None and float(boll) < -0.5 else 0))
    v5 = (+1 if mtf is not None and abs(float(mtf)) > mtf_T / 2 and float(mtf) >  0 else
          (-1 if mtf is not None and abs(float(mtf)) > mtf_T / 2 and float(mtf) < 0 else 0))

    votes = [v1, v2, v3, v4, v5]
    yes_v = sum(1 for v in votes if v == +1)
    no_v  = sum(1 for v in votes if v == -1)

    if yes_v >= 3:
        ensemble = 1
    elif no_v >= 3:
        ensemble = -1
    else:
        ensemble = 0

    return {
        "bs_p_yes": bs_p,
        "mtf_mom":  float(mtf) if mtf is not None else 0.0,
        "rsi_dev":  float(rsi_dev) if rsi_dev is not None else 0.0,
        "boll_z":   float(boll) if boll is not None else 0.0,
        "v1": v1, "v2": v2, "v3": v3, "v4": v4, "v5": v5,
        "ensemble": ensemble,
        "yes_votes": yes_v,
        "no_votes":  no_v,
    }


def run_ic_sweep(asset: str) -> dict:
    print(f"\n[{asset}] Loading data...")
    try:
        bars = load_bars(asset, check_min_history=False)
    except Exception as e:
        print(f"[{asset}] ERROR: {e}")
        return {}

    if bars.empty or len(bars) < HISTORY_BARS + WINDOW_MIN * 2:
        print(f"[{asset}] Insufficient data ({len(bars)} bars)")
        return {}

    bars = bars.sort_values("timestamp").reset_index(drop=True)
    closes = bars["close"].values
    ts = bars["timestamp"].values

    print(f"[{asset}] {len(bars):,} bars | {pd.to_datetime(ts[0]).date()} to {pd.to_datetime(ts[-1]).date()}")

    rows = []
    for i in range(HISTORY_BARS, len(closes) - WINDOW_MIN):
        hist   = list(closes[i - HISTORY_BARS:i])
        strike = closes[i]
        window_open  = closes[i]
        window_close = closes[i + WINDOW_MIN - 1]
        if window_open <= 0:
            continue
        label = 1 if window_close > window_open else 0
        log_ret = math.log(window_close / window_open)

        feats = _compute_features(hist, asset, strike)
        if feats is None:
            continue
        rows.append({**feats, "label": label, "log_ret": log_ret})

    if not rows:
        print(f"[{asset}] No valid windows")
        return {}

    df = pd.DataFrame(rows)
    n  = len(df)
    print(f"[{asset}] {n:,} windows")

    signals = {
        "V1 (BS p_yes)": "bs_p_yes",
        "V2 (MTF mom) ": "mtf_mom",
        "V3 (RSI dev) ": "rsi_dev",
        "V4 (Boll z)  ": "boll_z",
        "Ensemble      ": "ensemble",
    }

    result = {"asset": asset, "n_windows": n}
    print(f"\n  {'Signal':<22} {'IC':>8}  {'Result'}")
    print(f"  {'-'*45}")
    for name, col in signals.items():
        ic = df[col].corr(df["label"], method="spearman")
        result[f"IC_{col.strip()}"] = round(float(ic), 4)
        flag = "PASS" if abs(ic) >= 0.02 else "FAIL"
        print(f"  {name:<22} {ic:>8.4f}  {flag}")

    fired    = df[df["ensemble"] != 0]
    yes_fire = df[df["ensemble"] ==  1]
    no_fire  = df[df["ensemble"] == -1]

    fire_rate = len(fired) / n
    yes_pct   = len(yes_fire) / max(len(fired), 1)
    no_pct    = len(no_fire)  / max(len(fired), 1)
    wr_yes    = yes_fire["label"].mean() if len(yes_fire) > 0 else None
    wr_no     = (1 - no_fire["label"].mean()) if len(no_fire) > 0 else None

    result.update({
        "fire_rate": round(fire_rate, 3),
        "yes_pct":   round(yes_pct, 3),
        "no_pct":    round(no_pct, 3),
        "wr_yes":    round(wr_yes, 3) if wr_yes is not None else None,
        "wr_no":     round(wr_no, 3)  if wr_no  is not None else None,
    })

    bias_flag = " *** YES-BIAS ***" if yes_pct > 0.80 else ""
    print(f"\n  Fire rate : {fire_rate:.1%}")
    print(f"  YES / NO  : {yes_pct:.1%} / {no_pct:.1%}{bias_flag}")
    if wr_yes is not None:
        print(f"  WR (YES)  : {wr_yes:.1%}")
    if wr_no is not None:
        print(f"  WR (NO)   : {wr_no:.1%}")

    return result


def main():
    print("=" * 60)
    print("D3-Hybrid IC Sweep — Real OHLCV Data")
    print("=" * 60)

    all_results = []
    for asset in ASSETS:
        r = run_ic_sweep(asset)
        if r:
            all_results.append(r)

    if not all_results:
        print("\nNo results — check data paths.")
        return

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    hdr = f"{'Asset':<6} {'V1 BS':>7} {'V2 MTF':>7} {'V3 RSI':>7} {'V4 Bol':>7} {'Ensemb':>7} {'YES%':>6} {'WR_Y':>6} {'WR_N':>6}"
    print(hdr)
    print("-" * len(hdr))
    for r in all_results:
        bias = " <-- YES-BIAS" if r.get("yes_pct", 0) > 0.80 else ""
        print(
            f"{r['asset']:<6}"
            f"{r.get('IC_bs_p_yes', 0):>7.4f}"
            f"{r.get('IC_mtf_mom', 0):>7.4f}"
            f"{r.get('IC_rsi_dev', 0):>7.4f}"
            f"{r.get('IC_boll_z', 0):>7.4f}"
            f"{r.get('IC_ensemble', 0):>7.4f}"
            f"{r.get('yes_pct', 0):>6.1%}"
            f"{r.get('wr_yes') or 0:>6.1%}"
            f"{r.get('wr_no') or 0:>6.1%}"
            f"{bias}"
        )

    print("\nIC >= 0.02 = weak but real signal | YES% > 80% = YES-bias red flag")

    out = os.path.join(_THIS_DIR, "ic_results_real.md")
    with open(out, "w") as f:
        f.write("# D3-Hybrid IC Sweep — Real OHLCV Data\n\n")
        f.write("| Asset | V1 BS p_yes | V2 MTF mom | V3 RSI | V4 Boll | Ensemble | Fire% | YES% | WR(YES) | WR(NO) |\n")
        f.write("|-------|-------------|------------|--------|---------|----------|-------|------|---------|--------|\n")
        for r in all_results:
            f.write(
                f"| {r['asset']} "
                f"| {r.get('IC_bs_p_yes',0):.4f} "
                f"| {r.get('IC_mtf_mom',0):.4f} "
                f"| {r.get('IC_rsi_dev',0):.4f} "
                f"| {r.get('IC_boll_z',0):.4f} "
                f"| {r.get('IC_ensemble',0):.4f} "
                f"| {r.get('fire_rate',0):.1%} "
                f"| {r.get('yes_pct',0):.1%} "
                f"| {r.get('wr_yes') or 0:.1%} "
                f"| {r.get('wr_no') or 0:.1%} |\n"
            )
    print(f"\nResults -> {out}")


if __name__ == "__main__":
    main()
