"""
Step 4 -- Walk-Forward Analysis of D3-hybrid inverted ensemble.

Splits full history into N yearly windows and measures IC + WR per window.
Signal is stable if IC stays positive and WR > 50% across all regimes.

Usage:
    py backtesting/research/wfa_signal_real.py
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
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
WINDOW_MIN   = 15
N_FOLDS      = 8   # split into 8 time windows

# Best thresholds from step 2 (inverted directions)
_MTF_T  = {"BTC": 0.0005, "ETH": 0.0005, "SOL": 0.0005, "XRP": 0.0005}
_RSI_T  = {"BTC": 5.0,    "ETH": 8.0,    "SOL": 10.0,   "XRP": 8.0}
_BOLL_T = {"BTC": 0.75,   "ETH": 0.50,   "SOL": 0.50,   "XRP": 0.35}


def _rsi(prices, period=14):
    if len(prices) < period + 2:
        return None
    changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains  = [max(0.0, c) for c in changes]
    losses = [max(0.0, -c) for c in changes]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        ag = (ag * (period-1) + gains[i]) / period
        al = (al * (period-1) + losses[i]) / period
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def _boll_z(prices, period=20):
    if len(prices) < period:
        return None
    rec  = prices[-period:]
    mean = sum(rec) / len(rec)
    var  = sum((p - mean)**2 for p in rec) / (len(rec) - 1)
    std  = math.sqrt(var) if var > 0 else 0.0
    return (prices[-1] - mean) / std if std > 0 else None


def _mtf(prices):
    if len(prices) < 31 or prices[-1] <= 0:
        return None
    c = prices[-1]
    r5  = (c - prices[-6])  / prices[-6]  if prices[-6]  > 0 else 0.0
    r15 = (c - prices[-16]) / prices[-16] if prices[-16] > 0 else 0.0
    r30 = (c - prices[-31]) / prices[-31] if prices[-31] > 0 else 0.0
    return (r5 + r15 + r30) / 3.0


def _vol(prices):
    lr = [math.log(prices[i]/prices[i-1])
          for i in range(1, len(prices))
          if prices[i] > 0 and prices[i-1] > 0]
    if len(lr) < 2:
        return 0.0
    m = sum(lr) / len(lr)
    return math.sqrt(sum((r-m)**2 for r in lr) / (len(lr)-1))


def _ensemble_vote(prices, asset, strike):
    if len(prices) < 32:
        return None, None
    vol = _vol(prices)
    bs  = compute_bs_p_yes(prices[-1], strike, vol, SECONDS_LEFT)
    if bs is None:
        return None, None

    mt = _MTF_T[asset];  rt = _RSI_T[asset];  bt = _BOLL_T[asset]
    m  = _mtf(prices)
    rv = _rsi(prices)
    rd = (float(rv) - 50.0) if rv is not None else None
    bz = _boll_z(prices)

    v1 = +1 if bs > 0.5 else -1
    v2 = (-1 if m  is not None and float(m)  >  mt else
          (+1 if m  is not None and float(m)  < -mt else 0))
    v3 = (-1 if rd is not None and rd         >  rt else
          (+1 if rd is not None and rd         < -rt else 0))
    v4 = (-1 if bz is not None and float(bz) >  bt else
          (+1 if bz is not None and float(bz) < -bt else 0))
    v5 = (-1 if m  is not None and abs(float(m)) > mt/2 and float(m) >  0 else
          (+1 if m  is not None and abs(float(m)) > mt/2 and float(m) <  0 else 0))

    yv = sum(1 for v in [v1,v2,v3,v4,v5] if v == +1)
    nv = sum(1 for v in [v1,v2,v3,v4,v5] if v == -1)

    if yv >= 3:   return 1, bs
    if nv >= 3:   return -1, bs
    return 0, bs


def compute_fold_metrics(closes, asset, fold_label):
    rows = []
    for i in range(HISTORY_BARS, len(closes) - WINDOW_MIN):
        hist   = list(closes[i - HISTORY_BARS:i])
        strike = closes[i]
        wopen  = closes[i]
        wclose = closes[i + WINDOW_MIN - 1]
        if wopen <= 0:
            continue
        label = 1 if wclose > wopen else 0
        vote, _ = _ensemble_vote(hist, asset, strike)
        if vote is None:
            continue
        rows.append({"vote": vote, "label": label})

    if not rows:
        return None

    df    = pd.DataFrame(rows)
    n     = len(df)
    fired = df[df["vote"] != 0]
    yf    = df[df["vote"] ==  1]
    nf    = df[df["vote"] == -1]

    ic       = df["vote"].corr(df["label"], method="spearman")
    fire_r   = len(fired) / n
    yes_pct  = len(yf) / max(len(fired), 1)
    wr_yes   = yf["label"].mean() if len(yf) > 0 else float("nan")
    wr_no    = (1 - nf["label"].mean()) if len(nf) > 0 else float("nan")

    return {
        "fold":     fold_label,
        "n":        n,
        "ic":       round(float(ic), 4),
        "fire_r":   round(float(fire_r), 3),
        "yes_pct":  round(float(yes_pct), 3),
        "wr_yes":   round(float(wr_yes), 3),
        "wr_no":    round(float(wr_no), 3),
        "n_yes":    len(yf),
        "n_no":     len(nf),
    }


def run_wfa(asset):
    print(f"\n[{asset}] Loading...")
    bars   = load_bars(asset, check_min_history=False)
    bars   = bars.sort_values("timestamp").reset_index(drop=True)
    closes = bars["close"].values
    ts     = pd.to_datetime(bars["timestamp"].values)

    # Split into N_FOLDS equal-sized time windows
    fold_size = len(closes) // N_FOLDS
    folds = []
    for k in range(N_FOLDS):
        s = k * fold_size
        e = (k + 1) * fold_size if k < N_FOLDS - 1 else len(closes)
        label = f"{ts[s].year}-Q{(ts[s].month-1)//3+1}"
        folds.append((s, e, label))

    print(f"[{asset}] {len(closes):,} bars | {N_FOLDS} folds of ~{fold_size:,} bars each")
    print(f"\n  {'Fold':<10} {'N':>8} {'IC':>8} {'Fire%':>7} {'YES%':>6} {'WR_Y':>6} {'WR_N':>6}  Pass?")
    print(f"  {'-'*65}")

    results = []
    ic_pass = 0
    wr_pass = 0

    for s, e, label in folds:
        # Add history buffer before fold start
        buf_s = max(0, s - HISTORY_BARS - WINDOW_MIN)
        chunk = closes[buf_s:e]
        offset = s - buf_s  # index within chunk where the fold actually starts

        fold_closes = chunk[offset - HISTORY_BARS:] if offset >= HISTORY_BARS else chunk
        m = compute_fold_metrics(fold_closes, asset, label)
        if m is None:
            print(f"  {label:<10} {'--':>8}")
            continue

        ic_ok  = m["ic"] > 0.01
        wry_ok = m["wr_yes"] >= 0.50
        wrn_ok = m["wr_no"]  >= 0.50
        both   = wry_ok and wrn_ok
        flag   = "PASS" if ic_ok and both else ("IC+" if ic_ok else "FAIL")

        if ic_ok:   ic_pass += 1
        if both:    wr_pass += 1

        wr_y_str = f"{m['wr_yes']:.1%}" if not math.isnan(m["wr_yes"]) else "  n/a"
        wr_n_str = f"{m['wr_no']:.1%}"  if not math.isnan(m["wr_no"])  else "  n/a"

        print(f"  {label:<10} {m['n']:>8,} {m['ic']:>8.4f} {m['fire_r']:>7.1%} {m['yes_pct']:>6.1%} {wr_y_str:>6} {wr_n_str:>6}  {flag}")
        results.append(m)

    n_folds = len(results)
    if n_folds == 0:
        return []

    avg_ic    = sum(r["ic"]     for r in results) / n_folds
    avg_wry   = sum(r["wr_yes"] for r in results if not math.isnan(r["wr_yes"])) / n_folds
    avg_wrn   = sum(r["wr_no"]  for r in results if not math.isnan(r["wr_no"]))  / n_folds

    print(f"  {'-'*65}")
    print(f"  {'AVERAGE':<10} {'':>8} {avg_ic:>8.4f} {'':>7} {'':>6} {avg_wry:>6.1%} {avg_wrn:>6.1%}")
    print(f"\n  IC > 0.01 in {ic_pass}/{n_folds} folds | Both WR > 50% in {wr_pass}/{n_folds} folds")

    verdict = "STABLE" if ic_pass >= n_folds * 0.75 else ("MARGINAL" if ic_pass >= n_folds * 0.5 else "UNSTABLE")
    print(f"  Verdict: {verdict}")

    return results


def main():
    print("=" * 65)
    print("Step 4 -- Walk-Forward Signal Stability (Inverted D3-hybrid)")
    print("=" * 65)

    all_verdicts = {}
    out_rows = []

    for asset in ASSETS:
        try:
            results = run_wfa(asset)
            if results:
                n_folds  = len(results)
                ic_pass  = sum(1 for r in results if r["ic"] > 0.01)
                wr_pass  = sum(1 for r in results if r["wr_yes"] >= 0.50 and r["wr_no"] >= 0.50)
                verdict  = "STABLE" if ic_pass >= n_folds * 0.75 else ("MARGINAL" if ic_pass >= n_folds * 0.5 else "UNSTABLE")
                all_verdicts[asset] = (verdict, ic_pass, wr_pass, n_folds)
                for r in results:
                    out_rows.append({"asset": asset, **r})
        except Exception as e:
            print(f"\n[{asset}] ERROR: {e}")

    print(f"\n{'='*65}")
    print("OVERALL VERDICT")
    print(f"{'='*65}")
    for asset, (verdict, icp, wrp, nf) in all_verdicts.items():
        print(f"  {asset}: {verdict}  (IC+ {icp}/{nf} folds | WR both-sides {wrp}/{nf} folds)")

    out = os.path.join(_THIS_DIR, "wfa_results_real.md")
    with open(out, "w") as f:
        f.write("# Step 4 -- Walk-Forward Signal Stability\n\n")
        for asset, (verdict, icp, wrp, nf) in all_verdicts.items():
            f.write(f"## {asset} -- {verdict}\n\n")
            f.write(f"IC positive in {icp}/{nf} folds. Both-WR in {wrp}/{nf} folds.\n\n")
            asset_rows = [r for r in out_rows if r["asset"] == asset]
            f.write("| Fold | N | IC | Fire% | YES% | WR_Y | WR_N |\n")
            f.write("|------|---|----|-------|------|------|------|\n")
            for r in asset_rows:
                f.write(
                    f"| {r['fold']} | {r['n']:,} | {r['ic']:.4f} "
                    f"| {r['fire_r']:.1%} | {r['yes_pct']:.1%} "
                    f"| {r['wr_yes']:.1%} | {r['wr_no']:.1%} |\n"
                )
            f.write("\n")
    print(f"\nResults -> {out}")


if __name__ == "__main__":
    main()
