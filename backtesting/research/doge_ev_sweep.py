"""DOGE EV threshold sweep — same methodology as run_ev_sweep_d3.py"""
from __future__ import annotations
import math, os, sys
import numpy as np
import pandas as pd
from datetime import timedelta

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
for _p in [_ROOT, os.path.join(_ROOT, "src")]:
    if _p not in sys.path: sys.path.insert(0, _p)

from backtesting.data.loaders import load_bars
from strategies.signals.black_scholes import compute_bs_p_yes

HISTORY_BARS = 60; SECONDS_LEFT = 600.0; WINDOW_MIN = 15; HOLDOUT_DAYS = 90
MTF_T = 0.0005; RSI_T = 8.0; BOLL_T = 0.50; FEE = 0.007; MIN_TRADES = 200

def _rsi(prices, period=14):
    if len(prices) < period + 2: return None
    ch = [prices[i]-prices[i-1] for i in range(1, len(prices))]
    g = [max(0.0, c) for c in ch]; l = [max(0.0, -c) for c in ch]
    ag = sum(g[:period])/period; al = sum(l[:period])/period
    for i in range(period, len(ch)):
        ag = (ag*(period-1)+g[i])/period; al = (al*(period-1)+l[i])/period
    return 100.0 if al == 0 else 100.0 - 100.0/(1.0 + ag/al)

def _boll_z(prices, period=20):
    if len(prices) < period: return None
    r = prices[-period:]; m = sum(r)/len(r)
    v = sum((p-m)**2 for p in r)/(len(r)-1); s = math.sqrt(v) if v > 0 else 0.0
    return (prices[-1]-m)/s if s > 0 else None

def _mtf(prices):
    if len(prices) < 31 or prices[-1] <= 0: return None
    c = prices[-1]
    r5  = (c-prices[-6])/prices[-6]  if prices[-6]  > 0 else 0.0
    r15 = (c-prices[-16])/prices[-16] if prices[-16] > 0 else 0.0
    r30 = (c-prices[-31])/prices[-31] if prices[-31] > 0 else 0.0
    return (r5+r15+r30)/3.0

def _vol(prices):
    lr = [math.log(prices[i]/prices[i-1]) for i in range(1, len(prices))
          if prices[i] > 0 and prices[i-1] > 0]
    if len(lr) < 2: return 0.0
    m = sum(lr)/len(lr)
    return math.sqrt(sum((r-m)**2 for r in lr)/(len(lr)-1))

def _vote(prices, strike):
    if len(prices) < 32: return None
    vol = _vol(prices); bs = compute_bs_p_yes(prices[-1], strike, vol, SECONDS_LEFT)
    if bs is None: return None
    m = _mtf(prices); rv = _rsi(prices)
    rd = (float(rv)-50.0) if rv is not None else None; bz = _boll_z(prices)
    v1 = +1 if bs > 0.5 else -1
    v2 = (-1 if m  is not None and float(m)  >  MTF_T  else (+1 if m  is not None and float(m)  < -MTF_T  else 0))
    v3 = (-1 if rd is not None and rd         >  RSI_T  else (+1 if rd is not None and rd         < -RSI_T  else 0))
    v4 = (-1 if bz is not None and float(bz) >  BOLL_T else (+1 if bz is not None and float(bz) < -BOLL_T else 0))
    v5 = (-1 if m  is not None and abs(float(m)) > MTF_T/2 and float(m) > 0 else
          (+1 if m  is not None and abs(float(m)) > MTF_T/2 and float(m) < 0 else 0))
    yv = sum(1 for v in [v1,v2,v3,v4,v5] if v == +1)
    nv = sum(1 for v in [v1,v2,v3,v4,v5] if v == -1)
    if yv >= 3: return  1
    if nv >= 3: return -1
    return 0

def _eval_window(closes, p_model_no):
    rows = []
    for i in range(HISTORY_BARS, len(closes) - WINDOW_MIN):
        hist = list(closes[max(0, i-HISTORY_BARS):i])
        if len(hist) < HISTORY_BARS: continue
        strike = closes[i]; wclose = closes[i + WINDOW_MIN - 1]
        if strike <= 0: continue
        label = 1 if wclose > strike else 0
        vote  = _vote(hist, strike)
        if vote is None or vote != -1: continue  # NO-only
        fp   = 1.0 - p_model_no          # fill price for NO
        pnl  = ((1 - label) - fp) - FEE  # per-unit pnl
        rows.append({"pnl": pnl, "win": int(label == 0)})
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["pnl","win"])

def main():
    print("=" * 60)
    print("DOGE D3-Hybrid EV Threshold Sweep")
    print("=" * 60)

    bars = load_bars("DOGE", check_min_history=False)
    bars = bars.sort_values("timestamp").reset_index(drop=True)
    ts   = pd.to_datetime(bars["timestamp"])
    holdout_cutoff = ts.max() - timedelta(days=HOLDOUT_DAYS)

    # Use same 660-day pre-holdout window as other assets
    sweep_end   = holdout_cutoff
    sweep_start = sweep_end - timedelta(days=660)
    sweep = bars[(ts >= sweep_start) & (ts < sweep_end)]
    closes = sweep["close"].values
    print(f"Sweep window: {sweep_start.date()} to {sweep_end.date()} ({len(sweep):,} bars)")

    # Compute empirical P_NO from full sweep
    print("Computing windows (this takes ~2 min)...", flush=True)
    all_rows = []
    for i in range(HISTORY_BARS, len(closes) - WINDOW_MIN):
        hist = list(closes[max(0, i-HISTORY_BARS):i])
        if len(hist) < HISTORY_BARS: continue
        strike = closes[i]; wclose = closes[i + WINDOW_MIN - 1]
        if strike <= 0: continue
        label = 1 if wclose > strike else 0
        vote  = _vote(hist, strike)
        if vote is None or vote == 0: continue
        all_rows.append({"vote": vote, "label": label})

    df_all = pd.DataFrame(all_rows)
    no_df  = df_all[df_all["vote"] == -1]
    p_no   = float(1 - no_df["label"].mean()) if len(no_df) > 0 else 0.5
    print(f"Total NO signals: {len(no_df):,}  P_NO={p_no:.4f}")

    # EV sweep
    print(f"\nEV sweep (NO side, p_model={p_no:.3f}):")
    print(f"  ev    n      WR      sharpe   total($25)   wk")
    best = {"sharpe": -99, "ev": 0, "tot": 0, "n": 0}
    results = []
    for ev_int in range(1, 31):
        ev = ev_int / 100.0
        fp = 1.0 - p_no
        ev_actual = p_no - fp
        if ev_actual < ev: continue
        # All NO signals pass threshold (p_no is uniform for the asset)
        pnl = no_df["label"].apply(lambda lbl: ((1-lbl) - fp) - FEE).values
        pnl_scaled = pnl * 25.0
        if len(pnl_scaled) < MIN_TRADES: continue
        tot = float(pnl_scaled.sum())
        sp  = float(pnl.mean() / (pnl.std() + 1e-10))
        wr  = float((pnl > 0).mean())
        wk  = tot / 660 * 7
        print(f"  {ev:.2f}  {len(pnl):>6}  WR={wr:.1%}  sharpe={sp:+.3f}  total=${tot:+.0f}  wk=${wk:+.0f}")
        if sp > best["sharpe"]:
            best = {"sharpe": sp, "ev": ev, "tot": tot, "n": len(pnl), "wk": wk, "wr": wr}
        results.append({"ev": ev, "n": len(pnl), "wr": wr, "sharpe": sp, "total": tot, "wk": wk})

    print()
    print(f"RECOMMENDATION: min_ev={best['ev']:.2f}  n={best['n']:,}  WR={best['wr']:.1%}  wk=${best['wk']:+.0f}/wk")

    # WFA: split sweep into 6 folds
    print(f"\nWFA stability check (6 folds):")
    fold_days = 660 // 6
    fold_positive = 0
    for fold in range(6):
        fs = sweep_start + timedelta(days=fold*fold_days)
        fe = fs + timedelta(days=fold_days)
        fc = bars[(ts >= fs) & (ts < fe)]["close"].values
        fold_pnl = []
        for i in range(HISTORY_BARS, len(fc) - WINDOW_MIN):
            hist = list(fc[max(0, i-HISTORY_BARS):i])
            if len(hist) < HISTORY_BARS: continue
            strike = fc[i]; wclose = fc[i+WINDOW_MIN-1]
            if strike <= 0: continue
            label = 1 if wclose > strike else 0
            vote  = _vote(hist, strike)
            if vote != -1: continue
            fp  = 1.0 - p_no
            pnl = (((1-label) - fp) - FEE) * 25.0
            fold_pnl.append(pnl)
        if fold_pnl:
            tot = sum(fold_pnl)
            wr  = sum(1 for p in fold_pnl if p > 0) / len(fold_pnl)
            pos = tot > 0
            fold_positive += int(pos)
            print(f"  Fold {fold} ({fs.date()} to {fe.date()}): n={len(fold_pnl):,}  WR={wr:.1%}  total=${tot:+.0f}  {'PASS' if pos else 'FAIL'}")

    print(f"\n  WFA: {fold_positive}/6 folds profitable")
    print()

if __name__ == "__main__":
    main()
