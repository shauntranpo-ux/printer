"""
EV threshold sweep for 15m markets (BTC, ETH, SOL, XRP).

Uses binary-option pricing to derive realistic contract fill prices per window
(matches how the live bot uses EV: P_MODEL - fill_price - fee >= min_ev).
Fixed: optimized Supertrend params from config.json.
Variable: min_ev from 0.04 to 0.25.

EV interpretation:
  p_yes_market = Phi(ln(F/K) / (rv * sqrt(T)))  # realistic Kalshi AMM price
  YES EV = P_MODEL(0.70) - p_yes_market - fee
  NO  EV = P_MODEL(0.70) - (1 - p_yes_market) - fee

High EV = contract is cheap vs model -> better edge.
Low EV  = contract is near/above model price -> less edge.

Pipeline per window:
  1. Vol-ratio gate     (rv * sqrt(T) / max(abs_pct, 0.003) < 1.80)
  2. Supertrend direction
  3. Momentum alignment (3-tick delta)
  4. Entry price range  (5c - 75c)
  5. EV gate           (side_ev >= min_ev)

WFA: 6 sequential folds on full-year windows.
MC:  1000 bootstrap resamples on full-year data.
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

LOAD_START  = "2024-04-01"
LOAD_END    = "2025-04-01"
SWEEP_SPLIT = "2024-10-01"   # 6-month sweep window; WFA/MC use full year

EV_VALUES = [round(x / 100, 2) for x in range(4, 26)]   # 0.04 ... 0.25  (22 values)
MIN_TRADES = 20
TOP_N      = 5

P_MODEL     = 0.70   # _SUPERTREND_P_MODEL in base.py
VOL_THRESH  = 1.80
ABS_FLOOR   = 0.003
STEP        = 15
ENTRY_MIN   = 5
STRIKE_LB   = 30
MIN_PRICE_C = 5.0
MAX_PRICE_C = 75.0

_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")

# Sweep-optimised params locked into config.json
ASSET_PARAMS = {
    "BTC": {"period": 14, "mult": 5.0, "lookback": 3},
    "ETH": {"period": 14, "mult": 5.0, "lookback": 3},
    "SOL": {"period": 20, "mult": 5.5, "lookback": 3},
    "XRP": {"period": 14, "mult": 5.5, "lookback": 3},
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _binary_price(cur: float, strike: float, rv: float, mins: float) -> float:
    """P(price > strike at expiry) — binary call option via normal distribution."""
    sigma = rv * math.sqrt(max(mins, 1e-6))
    if sigma < 1e-8 or strike <= 0 or cur <= 0:
        return 0.5
    d = math.log(cur / strike) / sigma
    return _norm_cdf(d)


def _fee(p: float) -> float:
    return math.ceil(0.07 * p * (1.0 - p) * 100) / 100.0


# ── window precomputation ─────────────────────────────────────────────────────

def _precompute_windows(bars: pd.DataFrame) -> list[dict]:
    from strategies.signals.supertrend import _build_1m_ohlcv

    closes = bars["close"].to_numpy(dtype=float)
    opens  = bars["open"].to_numpy(dtype=float)
    ts_u   = (pd.to_datetime(bars["timestamp"], utc=True) - _EPOCH).dt.total_seconds().to_numpy()

    n   = len(bars)
    out = []

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

        hs = max(0, ei - 120)
        cl = closes[hs:ei]
        op = opens[hs:ei].clip(1e-10)
        ts = ts_u[hs:ei]

        rv = float(np.std(np.log(cl.clip(1e-10) / op))) if len(cl) >= 2 else 0.002

        eff = max(abs_pct, ABS_FLOOR)
        vr  = rv * math.sqrt(mins_left) / eff

        prices_q   = _deque(zip(ts, cl), maxlen=3600)
        ohlcv_bars = _build_1m_ohlcv(prices_q)

        ntick = len(prices_q)
        mom   = {}
        for lb in [3, 4, 5]:
            mom[lb] = (prices_q[-1][1] - prices_q[-lb][1]) if ntick >= lb + 1 else None

        p_yes = _binary_price(cur, strike, rv, mins_left)
        yes_c = p_yes * 100.0
        no_c  = (1.0 - p_yes) * 100.0
        label = 1 if ex > strike else 0

        out.append({
            "bars":  ohlcv_bars,
            "mom":   mom,
            "vr":    vr,
            "yes_c": yes_c,
            "no_c":  no_c,
            "label": label,
        })

    return out


# ── single EV threshold evaluation ───────────────────────────────────────────

def _eval(windows: list[dict], period: int, mult: float, lookback: int,
          min_ev: float) -> pd.DataFrame:
    from strategies.signals.supertrend import supertrend_direction_from_bars

    min_bars = period + 2
    recs     = []

    for w in windows:
        if w["vr"] >= VOL_THRESH:
            continue

        st = supertrend_direction_from_bars(w["bars"], period, mult, min_bars)
        if st is None:
            continue

        side  = "yes" if st == 1 else "no"
        delta = w["mom"].get(lookback)
        if delta is None:
            continue
        if side == "yes" and delta <= 0:
            continue
        if side == "no"  and delta >= 0:
            continue

        fill_c = w["yes_c"] if side == "yes" else w["no_c"]
        if fill_c < MIN_PRICE_C or fill_c > MAX_PRICE_C:
            continue

        fp  = fill_c / 100.0
        fee = _fee(fp)
        ev  = P_MODEL - fp - fee

        if ev < min_ev:
            continue

        lbl = w["label"]
        pnl = ((lbl - fp) - fee) if side == "yes" else (((1 - lbl) - fp) - fee)
        win = int((side == "yes" and lbl == 1) or (side == "no" and lbl == 0))
        recs.append({"pnl": pnl, "win": win})

    return pd.DataFrame(recs) if recs else pd.DataFrame(columns=["pnl", "win"])


# ── WFA ───────────────────────────────────────────────────────────────────────

def _wfa(folds: list, period: int, mult: float, lookback: int,
         min_ev: float) -> pd.DataFrame:
    rows = []
    for i, fold in enumerate(folds):
        df = _eval(fold, period, mult, lookback, min_ev)
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


# ── per-asset sweep ───────────────────────────────────────────────────────────

def run_asset(asset: str, win_sweep: list, win_full: list, folds: list) -> pd.DataFrame:
    p = ASSET_PARAMS[asset]
    period, mult, lb = p["period"], p["mult"], p["lookback"]

    print(f"\n{'='*66}")
    print(f"  {asset}  sweep={len(win_sweep):,} windows  full={len(win_full):,}  "
          f"period={period} mult={mult} lb={lb}")
    print(f"{'='*66}")

    # ETA from timing the first EV value
    t0 = _time.time()
    first_df = _eval(win_sweep, period, mult, lb, EV_VALUES[0])
    t1 = _time.time()
    per_ev    = t1 - t0
    sweep_eta = per_ev * len(EV_VALUES)
    print(f"  1 EV run: {per_ev:.2f}s  |  sweep ETA ~{sweep_eta:.0f}s "
          f"({sweep_eta / 60:.1f} min)  |  WFA+MC adds <30s")

    results = []
    for i, ev in enumerate(EV_VALUES):
        df = first_df if i == 0 else _eval(win_sweep, period, mult, lb, ev)
        n  = len(df)
        if n < MIN_TRADES:
            print(f"  ev={ev:.2f}  n={n:>5}  (< {MIN_TRADES} trades, skip)")
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

    print(f"\n  {asset} -- WFA + MC  (top {len(top)} thresholds, full year)")
    for _, row in top.iterrows():
        ev_val = float(row["min_ev"])

        wfa = _wfa(folds, period, mult, lb, ev_val)
        pos = int((wfa["pnl"] > 0).sum())
        print(f"    WFA  ev={ev_val:.2f}  folds_pos={pos}/{len(wfa)}  "
              f"avg_WR={wfa['wr'].mean():.1%}  avg_sharpe={wfa['sharpe'].mean():+.3f}")

        full_df = _eval(win_full, period, mult, lb, ev_val)
        if len(full_df) >= MIN_TRADES:
            pnl_arr = full_df["pnl"].values * 25
            rng     = np.random.default_rng(42)
            mc      = np.array([
                rng.choice(pnl_arr, size=len(pnl_arr), replace=True).sum()
                for _ in range(1000)
            ])
            lo, hi = float(np.quantile(mc, 0.025)), float(np.quantile(mc, 0.975))
            pp     = float((mc > 0).mean())
            print(f"    MC   ev={ev_val:.2f}  "
                  f"95CI=[${lo:+.1f}, ${hi:+.1f}]  P(profit)={pp:.1%}")

        wfa.to_csv(
            os.path.join(OUTPUT_DIR, f"ev_wfa_{asset.lower()}_ev{ev_val:.2f}.csv"),
            index=False,
        )

    return df_res


# ── main ──────────────────────────────────────────────────────────────────────

def run():
    all_results = []
    t_global    = _time.time()

    for asset in ["BTC", "ETH", "SOL", "XRP"]:
        print(f"\nLoading {asset}  {LOAD_START} to {LOAD_END}...")
        try:
            bars = load_bars(asset, start_date=LOAD_START, end_date=LOAD_END)
        except Exception as exc:
            print(f"  SKIP: {exc}"); continue

        if "open_time" in bars.columns:
            bars = bars.rename(columns={"open_time": "timestamp"})
        bars["timestamp"] = pd.to_datetime(bars["timestamp"], utc=True)
        print(f"  {len(bars):,} bars loaded")

        bars_sweep = bars[bars["timestamp"] >= pd.Timestamp(SWEEP_SPLIT, tz="UTC")].reset_index(drop=True)

        t0 = _time.time()
        print("  Precomputing windows...", end=" ", flush=True)
        win_sweep = _precompute_windows(bars_sweep)
        win_full  = _precompute_windows(bars)
        print(f"sweep={len(win_sweep):,}  full={len(win_full):,}  ({_time.time()-t0:.1f}s)")

        nf    = 6
        fs    = len(win_full) // nf
        folds = [
            win_full[i * fs: (i + 1) * fs if i < nf - 1 else len(win_full)]
            for i in range(nf)
        ]

        df_res = run_asset(asset, win_sweep, win_full, folds)
        if df_res.empty:
            print(f"  No valid EV thresholds for {asset}")
            continue

        df_res.to_csv(os.path.join(OUTPUT_DIR, f"ev_sweep_{asset.lower()}.csv"), index=False)
        all_results.append(df_res)

    if not all_results:
        print("\nNo results."); return

    combined = pd.concat(all_results, ignore_index=True)
    combined.to_csv(os.path.join(OUTPUT_DIR, "ev_sweep_all.csv"), index=False)

    elapsed = _time.time() - t_global
    print(f"\n\n{'='*66}")
    print(f"FINAL RECOMMENDATIONS  (best Sharpe, WR >= 50%)   "
          f"total runtime: {elapsed / 60:.1f} min")
    print(f"{'='*66}")
    for asset in ["BTC", "ETH", "SOL", "XRP"]:
        sub = combined[combined["asset"] == asset]
        if sub.empty:
            print(f"  {asset}: no data"); continue
        q    = sub[sub["win_rate"] >= 0.50] if (sub["win_rate"] >= 0.50).any() else sub
        best = q.nlargest(1, "sharpe").iloc[0]
        print(f"  {asset:3s}  min_ev={best['min_ev']:.2f}  "
              f"n={int(best['n_trades']):>5}  WR={best['win_rate']:.1%}  "
              f"sharpe={best['sharpe']:+.3f}  total=${best['total_pnl_dollars']:+.1f}")
    print()


if __name__ == "__main__":
    run()
