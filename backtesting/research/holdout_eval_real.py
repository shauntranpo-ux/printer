"""
Step 5 -- Real holdout evaluation of inverted D3-hybrid ensemble.

Last 3 months of each asset's data is held out (never seen in steps 1-4).
Pass threshold: IC > 0.01, WR_Y > 50% (BTC/ETH), WR_N > 50% (all).
SOL/XRP are NO-only so WR_Y threshold is waived for them.

Usage:
    py backtesting/research/holdout_eval_real.py
"""
from __future__ import annotations

import math
import os
import sys
from datetime import timedelta

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

ASSETS       = ["BTC", "ETH", "SOL", "XRP"]
HISTORY_BARS = 60
SECONDS_LEFT = 600.0
WINDOW_MIN   = 15
HOLDOUT_DAYS = 90   # last 3 months reserved

NO_ONLY = {"SOL", "XRP"}   # only NO trades have WFA-validated edge

_MTF_T  = {"BTC": 0.0005, "ETH": 0.0005, "SOL": 0.0005, "XRP": 0.0005}
_RSI_T  = {"BTC": 5.0,    "ETH": 8.0,    "SOL": 10.0,   "XRP": 8.0}
_BOLL_T = {"BTC": 0.75,   "ETH": 0.50,   "SOL": 0.50,   "XRP": 0.35}


def _rsi(prices, period=14):
    if len(prices) < period + 2:
        return None
    ch = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    g  = [max(0.0, c) for c in ch]
    l  = [max(0.0, -c) for c in ch]
    ag = sum(g[:period]) / period
    al = sum(l[:period]) / period
    for i in range(period, len(ch)):
        ag = (ag * (period-1) + g[i]) / period
        al = (al * (period-1) + l[i]) / period
    return 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)


def _boll_z(prices, period=20):
    if len(prices) < period:
        return None
    r = prices[-period:]
    m = sum(r) / len(r)
    v = sum((p - m)**2 for p in r) / (len(r) - 1)
    s = math.sqrt(v) if v > 0 else 0.0
    return (prices[-1] - m) / s if s > 0 else None


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


def _vote(prices, asset, strike):
    if len(prices) < 32:
        return None
    vol = _vol(prices)
    bs  = compute_bs_p_yes(prices[-1], strike, vol, SECONDS_LEFT)
    if bs is None:
        return None

    mt = _MTF_T[asset]; rt = _RSI_T[asset]; bt = _BOLL_T[asset]
    m  = _mtf(prices)
    rv = _rsi(prices)
    rd = (float(rv) - 50.0) if rv is not None else None
    bz = _boll_z(prices)

    v1 = +1 if bs > 0.5 else -1
    v2 = (-1 if m  is not None and float(m)  >  mt else (+1 if m  is not None and float(m)  < -mt else 0))
    v3 = (-1 if rd is not None and rd         >  rt else (+1 if rd is not None and rd         < -rt else 0))
    v4 = (-1 if bz is not None and float(bz) >  bt else (+1 if bz is not None and float(bz) < -bt else 0))
    v5 = (-1 if m  is not None and abs(float(m)) > mt/2 and float(m) >  0 else
          (+1 if m  is not None and abs(float(m)) > mt/2 and float(m) <  0 else 0))

    yv = sum(1 for v in [v1,v2,v3,v4,v5] if v == +1)
    nv = sum(1 for v in [v1,v2,v3,v4,v5] if v == -1)
    if yv >= 3: return  1
    if nv >= 3: return -1
    return 0


def run_holdout(asset):
    bars = load_bars(asset, check_min_history=False)
    bars = bars.sort_values("timestamp").reset_index(drop=True)

    cutoff = pd.to_datetime(bars["timestamp"].iloc[-1]) - timedelta(days=HOLDOUT_DAYS)
    train  = bars[pd.to_datetime(bars["timestamp"]) <  cutoff]
    holdout = bars[pd.to_datetime(bars["timestamp"]) >= cutoff]

    closes_all = bars["close"].values
    ho_start   = len(train)

    print(f"\n[{asset}] Total {len(bars):,} bars")
    print(f"  Train  : {len(train):,} bars up to {cutoff.date()}")
    print(f"  Holdout: {len(holdout):,} bars ({cutoff.date()} to {pd.to_datetime(bars['timestamp'].iloc[-1]).date()})")

    rows = []
    for i in range(ho_start, len(closes_all) - WINDOW_MIN):
        buf_s  = max(0, i - HISTORY_BARS)
        hist   = list(closes_all[buf_s:i])
        if len(hist) < HISTORY_BARS:
            continue
        strike = closes_all[i]
        wopen  = closes_all[i]
        wclose = closes_all[i + WINDOW_MIN - 1]
        if wopen <= 0:
            continue
        label = 1 if wclose > wopen else 0
        vote  = _vote(hist, asset, strike)
        if vote is None:
            continue
        rows.append({"vote": vote, "label": label})

    if not rows:
        print(f"  No valid windows in holdout")
        return None

    df    = pd.DataFrame(rows)
    n     = len(df)
    fired = df[df["vote"] != 0]
    yf    = df[df["vote"] ==  1]
    nf    = df[df["vote"] == -1]

    ic      = df["vote"].corr(df["label"], method="spearman")
    fire_r  = len(fired) / n
    yes_pct = len(yf) / max(len(fired), 1)
    wr_yes  = yf["label"].mean() if len(yf) > 0 else float("nan")
    wr_no   = (1 - nf["label"].mean()) if len(nf) > 0 else float("nan")

    no_only = asset in NO_ONLY
    ic_ok   = float(ic) > 0.01
    wry_ok  = no_only or (not math.isnan(wr_yes) and wr_yes >= 0.50)
    wrn_ok  = not math.isnan(wr_no) and wr_no >= 0.50
    passed  = ic_ok and wry_ok and wrn_ok

    bias_flag = " *** YES-BIAS ***" if yes_pct > 0.80 else ""

    print(f"\n  Holdout results ({n:,} windows):")
    print(f"    IC        : {ic:+.4f}  {'PASS' if ic_ok else 'FAIL'}")
    print(f"    Fire rate : {fire_r:.1%}")
    print(f"    YES / NO  : {yes_pct:.1%} / {1-yes_pct:.1%}{bias_flag}")
    wr_y_str = f"{wr_yes:.1%}" if not math.isnan(wr_yes) else "n/a"
    wr_n_str = f"{wr_no:.1%}"  if not math.isnan(wr_no)  else "n/a"
    wry_flag = "PASS" if wry_ok else "FAIL"
    wrn_flag = "PASS" if wrn_ok else "FAIL"
    print(f"    WR (YES)  : {wr_y_str}  {wry_flag}{'  (waived - NO-only asset)' if no_only else ''}")
    print(f"    WR (NO)   : {wr_n_str}  {wrn_flag}")
    print(f"\n  Verdict: {'PASS' if passed else 'FAIL'}")

    return {
        "asset": asset, "n": n, "ic": round(float(ic), 4),
        "fire_rate": round(float(fire_r), 3),
        "yes_pct": round(float(yes_pct), 3),
        "wr_yes": round(float(wr_yes), 3) if not math.isnan(wr_yes) else None,
        "wr_no":  round(float(wr_no),  3) if not math.isnan(wr_no)  else None,
        "passed": passed,
    }


def main():
    print("=" * 60)
    print(f"Step 5 -- Real Holdout Evaluation (last {HOLDOUT_DAYS} days unseen)")
    print("=" * 60)

    results = []
    for asset in ASSETS:
        try:
            r = run_holdout(asset)
            if r:
                results.append(r)
        except Exception as e:
            print(f"\n[{asset}] ERROR: {e}")

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    hdr = f"{'Asset':<6} {'IC':>8} {'Fire%':>6} {'YES%':>6} {'WR_Y':>6} {'WR_N':>6}  Result"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        wr_y = f"{r['wr_yes']:.1%}" if r["wr_yes"] is not None else "  n/a"
        wr_n = f"{r['wr_no']:.1%}"  if r["wr_no"]  is not None else "  n/a"
        no_note = " (NO-only)" if r["asset"] in NO_ONLY else ""
        print(f"{r['asset']:<6} {r['ic']:>8.4f} {r['fire_rate']:>6.1%} {r['yes_pct']:>6.1%} {wr_y:>6} {wr_n:>6}  {'PASS' if r['passed'] else 'FAIL'}{no_note}")

    passed = [r for r in results if r["passed"]]
    print(f"\n{len(passed)}/{len(results)} assets pass holdout")

    out = os.path.join(_THIS_DIR, "holdout_results_real.md")
    with open(out, "w") as f:
        f.write(f"# Step 5 -- Real Holdout ({HOLDOUT_DAYS}-day unseen window)\n\n")
        f.write("| Asset | IC | Fire% | YES% | WR_Y | WR_N | Result |\n")
        f.write("|-------|-----|-------|------|------|------|--------|\n")
        for r in results:
            wy = f"{r['wr_yes']:.1%}" if r["wr_yes"] is not None else "n/a"
            wn = f"{r['wr_no']:.1%}"  if r["wr_no"]  is not None else "n/a"
            f.write(f"| {r['asset']} | {r['ic']:.4f} | {r['fire_rate']:.1%} | {r['yes_pct']:.1%} | {wy} | {wn} | {'PASS' if r['passed'] else 'FAIL'} |\n")
    print(f"Results -> {out}")


if __name__ == "__main__":
    main()
