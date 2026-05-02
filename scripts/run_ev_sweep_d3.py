"""
EV threshold sweep for D3-hybrid inverted signal (BTC, ETH, SOL, XRP).

Replaces the Supertrend sweep. Key differences:
  - Direction: D3-hybrid inverted ensemble (mean-reversion, 3-of-5 vote)
  - P_MODEL:   per-asset empirical WR from holdout (not fixed 0.70)
  - SOL/XRP:   NO trades only (WR_Y < 50% in WFA)

P_MODEL from holdout_results_real.md:
  BTC  YES=0.528  NO=0.543
  ETH  YES=0.532  NO=0.567
  SOL  NO=0.601
  XRP  NO=0.603

Usage:
    py run_ev_sweep_d3.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backtesting"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import warnings; warnings.filterwarnings("ignore")
import math, time as _time
import numpy as np
import pandas as pd
from collections import deque as _deque

from backtesting.data.loaders import load_bars

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backtesting", "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

LOAD_START = "2024-04-01"
LOAD_END   = "2026-01-20"   # pre-holdout cutoff

EV_VALUES  = [round(x / 100, 2) for x in range(1, 21)]   # 0.01 .. 0.20
MIN_TRADES = 20
TOP_N      = 5

VOL_THRESH = 1.80
ABS_FLOOR  = 0.003
STEP       = 15
ENTRY_MIN  = 5
STRIKE_LB  = 30
MIN_PRICE_C = 5.0
MAX_PRICE_C = 75.0

_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")

# Per-asset empirical win rates from holdout (P_MODEL replaces fixed 0.70)
P_MODEL = {
    "BTC": {"yes": 0.528, "no": 0.543},
    "ETH": {"yes": 0.532, "no": 0.567},
    "SOL": {"yes": None,  "no": 0.601},   # YES not traded
    "XRP": {"yes": None,  "no": 0.603},   # YES not traded
}

# D3-hybrid per-asset thresholds from step 2
_MTF_T  = {"BTC": 0.0005, "ETH": 0.0005, "SOL": 0.0005, "XRP": 0.0005}
_RSI_T  = {"BTC": 5.0,    "ETH": 8.0,    "SOL": 10.0,   "XRP": 8.0}
_BOLL_T = {"BTC": 0.75,   "ETH": 0.50,   "SOL": 0.50,   "XRP": 0.35}


# ── helpers ───────────────────────────────────────────────────────────────────

def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _binary_price(cur, strike, rv, mins):
    sigma = rv * math.sqrt(max(mins, 1e-6))
    if sigma < 1e-8 or strike <= 0 or cur <= 0:
        return 0.5
    return _norm_cdf(math.log(cur / strike) / sigma)


def _fee(p):
    return math.ceil(0.07 * p * (1.0 - p) * 100) / 100.0


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


def _mtf_mom(prices):
    if len(prices) < 31 or prices[-1] <= 0:
        return None
    c = prices[-1]
    r5  = (c - prices[-6])  / prices[-6]  if prices[-6]  > 0 else 0.0
    r15 = (c - prices[-16]) / prices[-16] if prices[-16] > 0 else 0.0
    r30 = (c - prices[-31]) / prices[-31] if prices[-31] > 0 else 0.0
    return (r5 + r15 + r30) / 3.0


def _d3_vote(prices, asset):
    """Inverted D3-hybrid ensemble. Returns 'yes', 'no', or None."""
    if len(prices) < 32:
        return None

    mt = _MTF_T[asset]; rt = _RSI_T[asset]; bt = _BOLL_T[asset]
    cur = prices[-1]
    vol = 0.0
    lr  = [math.log(prices[i]/prices[i-1]) for i in range(1, len(prices))
           if prices[i] > 0 and prices[i-1] > 0]
    if len(lr) >= 2:
        mn = sum(lr) / len(lr)
        vol = math.sqrt(sum((r-mn)**2 for r in lr) / (len(lr)-1))

    # V1: BS p_yes (positive IC — micro mean reversion)
    bs = _binary_price(cur, prices[-1], vol, 10.0)   # ATM, 10 min left
    v1 = +1 if bs > 0.5 else -1

    m  = _mtf_mom(prices)
    rv = _rsi(prices)
    rd = (float(rv) - 50.0) if rv is not None else None
    bz = _boll_z(prices)

    # V2-V5: inverted (against momentum)
    v2 = (-1 if m  is not None and float(m)  >  mt else (+1 if m  is not None and float(m)  < -mt else 0))
    v3 = (-1 if rd is not None and rd         >  rt else (+1 if rd is not None and rd         < -rt else 0))
    v4 = (-1 if bz is not None and float(bz) >  bt else (+1 if bz is not None and float(bz) < -bt else 0))
    v5 = (-1 if m  is not None and abs(float(m)) > mt/2 and float(m) >  0 else
          (+1 if m  is not None and abs(float(m)) > mt/2 and float(m) <  0 else 0))

    yv = sum(1 for v in [v1,v2,v3,v4,v5] if v == +1)
    nv = sum(1 for v in [v1,v2,v3,v4,v5] if v == -1)

    if yv >= 3: return "yes"
    if nv >= 3: return "no"
    return None


# ── window precomputation ─────────────────────────────────────────────────────

def _precompute_windows(bars, asset):
    closes = bars["close"].to_numpy(dtype=float)
    opens  = bars["open"].to_numpy(dtype=float)
    n      = len(bars)
    out    = []

    no_only = P_MODEL[asset]["yes"] is None

    for si in range(0, n - STEP + 1, STEP):
        ei = si + ENTRY_MIN
        xi = si + STEP - 1
        ki = max(0, si - STRIKE_LB)

        strike = closes[ki]
        cur    = closes[ei]
        ex     = closes[xi]

        if strike <= 0 or cur <= 0:
            continue

        abs_pct   = abs(cur - strike) / cur
        mins_left = float(STEP - ENTRY_MIN)

        hs  = max(0, ei - 120)
        cl  = closes[hs:ei]
        op  = opens[hs:ei].clip(1e-10)
        rv  = float(np.std(np.log(cl.clip(1e-10) / op))) if len(cl) >= 2 else 0.002

        eff = max(abs_pct, ABS_FLOOR)
        vr  = rv * math.sqrt(mins_left) / eff

        prices_list = list(cl)
        side = _d3_vote(prices_list, asset)

        if side is None:
            continue
        if no_only and side == "yes":
            continue

        p_yes = _binary_price(cur, strike, rv, mins_left)
        yes_c = p_yes * 100.0
        no_c  = (1.0 - p_yes) * 100.0
        label = 1 if ex > strike else 0

        out.append({
            "side":  side,
            "vr":    vr,
            "yes_c": yes_c,
            "no_c":  no_c,
            "label": label,
        })

    return out


# ── single EV threshold evaluation ───────────────────────────────────────────

def _eval(windows, asset, min_ev):
    pm = P_MODEL[asset]
    recs = []

    for w in windows:
        if w["vr"] >= VOL_THRESH:
            continue

        side   = w["side"]
        fill_c = w["yes_c"] if side == "yes" else w["no_c"]

        if fill_c < MIN_PRICE_C or fill_c > MAX_PRICE_C:
            continue

        fp       = fill_c / 100.0
        fee      = _fee(fp)
        p_model  = pm["yes"] if side == "yes" else pm["no"]
        if p_model is None:
            continue
        ev = p_model - fp - fee

        if ev < min_ev:
            continue

        lbl = w["label"]
        pnl = ((lbl - fp) - fee) if side == "yes" else (((1 - lbl) - fp) - fee)
        win = int((side == "yes" and lbl == 1) or (side == "no" and lbl == 0))
        recs.append({"pnl": pnl, "win": win})

    return pd.DataFrame(recs) if recs else pd.DataFrame(columns=["pnl", "win"])


def _wfa(folds, asset, min_ev):
    rows = []
    for i, fold in enumerate(folds):
        df = _eval(fold, asset, min_ev)
        if df.empty or len(df) < 5:
            rows.append({"fold": i, "n": 0, "wr": 0.0, "pnl": 0.0, "sharpe": 0.0})
            continue
        p = df["pnl"].values
        rows.append({
            "fold":   i,
            "n":      len(p),
            "wr":     round(float((p > 0).mean()), 4),
            "pnl":    round(float(p.sum()), 4),
            "sharpe": round(float(p.mean() / (p.std() + 1e-10)), 4),
        })
    return pd.DataFrame(rows)


def run_asset(asset, win_full, folds):
    no_only = P_MODEL[asset]["yes"] is None
    print(f"\n{'='*66}")
    print(f"  {asset}  windows={len(win_full):,}  {'NO-only' if no_only else 'YES+NO'}")
    print(f"  P_MODEL: YES={P_MODEL[asset]['yes']}  NO={P_MODEL[asset]['no']}")
    print(f"{'='*66}")

    results = []
    for ev in EV_VALUES:
        df = _eval(win_full, asset, ev)
        n  = len(df)
        if n < MIN_TRADES:
            print(f"  ev={ev:.2f}  n={n:>5}  (< {MIN_TRADES}, skip)")
            continue
        pnl = df["pnl"].values
        wr  = float(df["win"].mean())
        sp  = float(pnl.mean() / (pnl.std() + 1e-10))
        tot = float(pnl.sum() * 25)
        print(f"  ev={ev:.2f}  n={n:>5}  WR={wr:.1%}  sharpe={sp:+.3f}  total=${tot:+.1f}")
        results.append({
            "asset": asset, "min_ev": ev, "n_trades": n,
            "win_rate": round(wr, 4), "sharpe": round(sp, 4),
            "total_pnl_dollars": round(tot, 2),
        })

    if not results:
        return pd.DataFrame()

    df_res    = pd.DataFrame(results)
    qualified = df_res[df_res["win_rate"] >= 0.50] if (df_res["win_rate"] >= 0.50).any() else df_res
    top       = qualified.nlargest(TOP_N, "sharpe")

    print(f"\n  {asset} -- WFA + MC  (top {len(top)} thresholds)")
    for _, row in top.iterrows():
        ev_val = float(row["min_ev"])

        wfa = _wfa(folds, asset, ev_val)
        pos = int((wfa["pnl"] > 0).sum())
        print(f"    WFA  ev={ev_val:.2f}  folds_pos={pos}/{len(wfa)}  "
              f"avg_WR={wfa['wr'].mean():.1%}  avg_sharpe={wfa['sharpe'].mean():+.3f}")

        full_df = _eval(win_full, asset, ev_val)
        if len(full_df) >= MIN_TRADES:
            pnl_arr = full_df["pnl"].values * 25
            rng     = np.random.default_rng(42)
            mc      = np.array([
                rng.choice(pnl_arr, size=len(pnl_arr), replace=True).sum()
                for _ in range(1000)
            ])
            lo, hi = float(np.quantile(mc, 0.025)), float(np.quantile(mc, 0.975))
            pp     = float((mc > 0).mean())
            print(f"    MC   ev={ev_val:.2f}  95CI=[${lo:+.1f}, ${hi:+.1f}]  P(profit)={pp:.1%}")

        wfa.to_csv(
            os.path.join(OUTPUT_DIR, f"ev_wfa_d3_{asset.lower()}_ev{ev_val:.2f}.csv"),
            index=False,
        )

    return df_res


def run():
    all_results = []
    t0_global   = _time.time()

    for asset in ["BTC", "ETH", "SOL", "XRP"]:
        print(f"\nLoading {asset}  {LOAD_START} to {LOAD_END}...")
        try:
            bars = load_bars(asset, start_date=LOAD_START, end_date=LOAD_END,
                             check_min_history=False)
        except Exception as exc:
            print(f"  SKIP: {exc}"); continue

        bars["timestamp"] = pd.to_datetime(bars["timestamp"], utc=True)
        print(f"  {len(bars):,} bars loaded")

        t0 = _time.time()
        print("  Precomputing windows...", end=" ", flush=True)
        win_full = _precompute_windows(bars, asset)
        print(f"{len(win_full):,}  ({_time.time()-t0:.1f}s)")

        if not win_full:
            print(f"  No windows for {asset}"); continue

        nf    = 6
        fs    = len(win_full) // nf
        folds = [
            win_full[i*fs: (i+1)*fs if i < nf-1 else len(win_full)]
            for i in range(nf)
        ]

        df_res = run_asset(asset, win_full, folds)
        if df_res.empty:
            print(f"  No valid EV thresholds for {asset}"); continue

        df_res.to_csv(os.path.join(OUTPUT_DIR, f"ev_sweep_d3_{asset.lower()}.csv"), index=False)
        all_results.append(df_res)

    if not all_results:
        print("\nNo results."); return

    combined = pd.concat(all_results, ignore_index=True)
    combined.to_csv(os.path.join(OUTPUT_DIR, "ev_sweep_d3_all.csv"), index=False)

    elapsed = _time.time() - t0_global
    print(f"\n\n{'='*66}")
    print(f"RECOMMENDATIONS  (best Sharpe, WR >= 50%)  runtime: {elapsed/60:.1f} min")
    print(f"{'='*66}")
    for asset in ["BTC", "ETH", "SOL", "XRP"]:
        sub = combined[combined["asset"] == asset]
        if sub.empty:
            print(f"  {asset}: no data"); continue
        q    = sub[sub["win_rate"] >= 0.50] if (sub["win_rate"] >= 0.50).any() else sub
        best = q.nlargest(1, "sharpe").iloc[0]
        print(f"  {asset}  min_ev={best['min_ev']:.2f}  "
              f"n={int(best['n_trades']):>5}  WR={best['win_rate']:.1%}  "
              f"sharpe={best['sharpe']:+.3f}  total=${best['total_pnl_dollars']:+.1f}")
    print()


if __name__ == "__main__":
    run()
