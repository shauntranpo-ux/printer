"""
Extended 15m parameter sweep: Supertrend period × multiplier × momentum_lookback.

Optimised: OHLCV bars are precomputed once per window per asset. Each combo
only runs the Supertrend math + momentum/vol checks on precomputed data.

Date range: 6 months (passes 180-day minimum, recent market conditions).
WFA uses the full year loaded from disk (sliced after sweep completes).

Usage:  py run_15m_sweep_extended.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backtesting"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import warnings; warnings.filterwarnings("ignore")
import math, time as _time
import numpy as np
import pandas as pd
from collections import deque as _deque

from backtesting.data.loaders import load_bars
from backtesting.simulation.fifteen_min_backtest import run_monte_carlo, run_wfa

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "backtesting", "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Date ranges ───────────────────────────────────────────────────────────────
# Load 1 full year (needed for WFA and to pass 180-day check).
# Sweep uses last 6 months of that year for speed; WFA uses all 12.
LOAD_START  = "2024-04-01"
LOAD_END    = "2025-04-01"
SWEEP_SPLIT = "2024-10-01"   # sweep uses bars >= this date (~6 months)

# ── Focused sweep grids ───────────────────────────────────────────────────────
GRIDS = {
    "BTC": {"period": [7, 10, 14], "mult": [3.5, 4.0, 4.5, 5.0], "lookback": [3, 4, 5]},
    "ETH": {"period": [7, 10, 14], "mult": [3.5, 4.0, 4.5, 5.0], "lookback": [3, 4, 5]},
    "SOL": {"period": [10, 14, 20], "mult": [4.0, 4.5, 5.0, 5.5], "lookback": [3, 4, 5]},
    "XRP": {"period": [10, 14, 20], "mult": [4.0, 4.5, 5.0, 5.5], "lookback": [3, 4, 5]},
}
MIN_TRADES = 20
TOP_N      = 5

STEP              = 15
ENTRY_MINUTE      = 5
STRIKE_LOOKBACK   = 30
YES_ASK           = 50.0
NO_ASK            = 50.0
VOL_THRESH        = 1.80
ABS_PCT_FLOOR     = 0.003

_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


def _kalshi_fee(p: float) -> float:
    return math.ceil(0.07 * p * (1.0 - p) * 100) / 100.0


def _precompute_windows(bars: pd.DataFrame) -> list[dict]:
    """Build per-window data structures once. OHLCV bars precomputed here."""
    from strategies.signals.supertrend import _build_1m_ohlcv

    closes    = bars["close"].to_numpy(dtype=float)
    opens_arr = bars["open"].to_numpy(dtype=float)
    ts_unix   = (pd.to_datetime(bars["timestamp"], utc=True) - _EPOCH).dt.total_seconds().to_numpy()

    n = len(bars)
    windows = []

    for start_idx in range(0, n - STEP + 1, STEP):
        if start_idx + STEP > n:
            break

        entry_idx = start_idx + ENTRY_MINUTE
        exit_idx  = start_idx + STEP - 1

        strike_idx   = max(0, start_idx - STRIKE_LOOKBACK)
        strike_price = closes[strike_idx]
        cur_price    = closes[entry_idx]
        exit_price   = closes[exit_idx]

        if strike_price <= 0 or cur_price <= 0:
            continue

        abs_pct   = abs(cur_price - strike_price) / cur_price
        mins_left = float(STEP - ENTRY_MINUTE)

        hist_start = max(0, entry_idx - 120)
        ts_sl  = ts_unix[hist_start:entry_idx]
        cl_sl  = closes[hist_start:entry_idx]
        op_sl  = opens_arr[hist_start:entry_idx].clip(1e-10)

        if len(cl_sl) >= 2:
            rv_1min = float(np.std(np.log(cl_sl.clip(1e-10) / op_sl)))
        else:
            rv_1min = 0.002

        # Vol-ratio gate pre-check (rejects window regardless of combo params)
        eff_pct = max(abs_pct, ABS_PCT_FLOOR)
        vol_ratio = rv_1min * (mins_left ** 0.5) / eff_pct

        # Precompute OHLCV bars (same for every combo on this window)
        prices_60m = _deque(zip(ts_sl, cl_sl), maxlen=3600)
        ohlcv_bars = _build_1m_ohlcv(prices_60m)

        # Precompute momentum deltas for all lookback values [3,4,5,6]
        # prices_60m[-k] → index -(k) in prices_60m
        n_ticks = len(prices_60m)
        momentum = {}
        for lb in [3, 4, 5, 6]:
            if n_ticks >= lb + 1:
                momentum[lb] = prices_60m[-1][1] - prices_60m[-lb][1]
            else:
                momentum[lb] = None

        label = 1 if exit_price > strike_price else 0

        windows.append({
            "ohlcv_bars":  ohlcv_bars,
            "momentum":    momentum,
            "vol_ratio":   vol_ratio,
            "abs_pct":     abs_pct,
            "mins_left":   mins_left,
            "rv_1min":     rv_1min,
            "label":       label,
        })

    return windows


def _eval_combo(windows: list[dict], period: int, mult: float, lookback: int) -> pd.DataFrame:
    """Evaluate one parameter combo on precomputed windows."""
    from strategies.signals.supertrend import supertrend_direction_from_bars

    min_bars = period + 2
    fill_yes = YES_ASK / 100.0
    fill_no  = NO_ASK / 100.0
    fee_yes  = _kalshi_fee(fill_yes)
    fee_no   = _kalshi_fee(fill_no)

    records = []
    for w in windows:
        # Vol-ratio gate
        if w["vol_ratio"] >= VOL_THRESH:
            continue

        # Supertrend on precomputed bars
        st = supertrend_direction_from_bars(w["ohlcv_bars"], period, mult, min_bars)
        if st is None:
            continue

        st_side = "yes" if st == 1 else "no"

        # Momentum alignment gate
        delta = w["momentum"].get(lookback)
        if delta is None:
            continue
        if st_side == "yes" and delta <= 0:
            continue
        if st_side == "no" and delta >= 0:
            continue

        label = w["label"]
        if st_side == "yes":
            pnl = (label - fill_yes) - fee_yes
            win = int(label == 1)
        else:
            pnl = ((1 - label) - fill_no) - fee_no
            win = int(label == 0)

        records.append({"pnl": pnl, "win": win})

    return pd.DataFrame(records) if records else pd.DataFrame(columns=["pnl", "win"])


def run_sweep_asset(asset: str, windows: list[dict]) -> pd.DataFrame:
    grid  = GRIDS[asset]
    total = len(grid["period"]) * len(grid["mult"]) * len(grid["lookback"])
    print(f"\n{'='*64}\n  {asset}  |  {len(windows):,} windows  |  {total} combos\n{'='*64}")

    results = []
    for period in grid["period"]:
        for mult in grid["mult"]:
            for lookback in grid["lookback"]:
                df = _eval_combo(windows, period, mult, lookback)
                n  = len(df)
                if n < MIN_TRADES:
                    continue
                pnl = df["pnl"].values
                wr  = float(df["win"].mean())
                sp  = float(pnl.mean() / (pnl.std() + 1e-10))
                print(f"  p={period:2d} m={mult:.1f} lb={lookback}  "
                      f"n={n:>6}  WR={wr:.1%}  sharpe={sp:+.3f}")
                results.append({"asset": asset, "period": period, "mult": mult,
                                 "lookback": lookback, "n_trades": n,
                                 "win_rate": round(wr, 4),
                                 "sharpe": round(sp, 4)})

    return pd.DataFrame(results) if results else pd.DataFrame()


def run_wfa_mc(asset: str, bars_full: pd.DataFrame, top: pd.DataFrame) -> None:
    print(f"\n  {asset} WFA + MC  (top {len(top)} combos, full year)")
    for _, row in top.iterrows():
        label  = f"p={int(row['period'])} m={row['mult']:.1f} lb={int(row['lookback'])}"
        kwargs = dict(min_ev=0.0, stake_dollars=25.0, entry_minute=5,
                      min_entry_price_cents=5.0, max_entry_price_cents=75.0,
                      supertrend_atr_period=int(row["period"]),
                      supertrend_atr_multiplier=float(row["mult"]),
                      momentum_lookback=int(row["lookback"]))

        wfa_df = run_wfa(bars_full, asset, n_folds=6, **kwargs)
        pos    = int((wfa_df["total_pnl"] > 0).sum())
        print(f"    WFA  {label:22s}  folds_pos={pos}/{len(wfa_df)}  "
              f"avg_WR={wfa_df['win_rate'].mean():.1%}  avg_sharpe={wfa_df['sharpe'].mean():+.3f}")

        from backtesting.simulation.fifteen_min_backtest import run_fifteen_min_backtest
        full_df = run_fifteen_min_backtest(bars_full, asset, **kwargs)
        if not full_df.empty:
            mc = run_monte_carlo(full_df, n_iterations=1000)
            lo = float(np.quantile(mc["total_pnl"], 0.025))
            hi = float(np.quantile(mc["total_pnl"], 0.975))
            pp = float((mc["total_pnl"] > 0).mean())
            print(f"    MC   {label:22s}  "
                  f"95CI=[{lo*25:+.1f}$, {hi*25:+.1f}$]  P(profit)={pp:.1%}")

        wfa_df.to_csv(os.path.join(OUTPUT_DIR, f"wfa_{asset.lower()}_p{int(row['period'])}_m{row['mult']:.1f}_lb{int(row['lookback'])}.csv"), index=False)


def run():
    all_results = []

    for asset in ["BTC", "ETH", "SOL", "XRP"]:
        print(f"\nLoading {asset} bars {LOAD_START} to {LOAD_END}...")
        try:
            bars_full = load_bars(asset, start_date=LOAD_START, end_date=LOAD_END)
        except Exception as exc:
            print(f"  SKIP: {exc}"); continue

        bars_full = bars_full.rename(columns={"open_time": "timestamp"}) if "open_time" in bars_full.columns else bars_full
        bars_full["timestamp"] = pd.to_datetime(bars_full["timestamp"], utc=True)
        print(f"  Loaded {len(bars_full):,} bars")

        # Sweep on last 6 months only
        bars_sweep = bars_full[bars_full["timestamp"] >= pd.Timestamp(SWEEP_SPLIT, tz="UTC")].reset_index(drop=True)
        print(f"  Sweep window: {len(bars_sweep):,} bars")

        t0 = _time.time()
        print("  Precomputing windows...", end=" ", flush=True)
        windows = _precompute_windows(bars_sweep)
        print(f"{len(windows):,} windows in {_time.time()-t0:.1f}s")

        sweep_df = run_sweep_asset(asset, windows)
        if sweep_df.empty:
            print(f"  No valid combos for {asset}"); continue

        sweep_df.to_csv(os.path.join(OUTPUT_DIR, f"sweep_{asset.lower()}.csv"), index=False)
        all_results.append(sweep_df)

        qualified = sweep_df[sweep_df["win_rate"] >= 0.50] if (sweep_df["win_rate"] >= 0.50).any() else sweep_df
        top = qualified.nlargest(TOP_N, "sharpe")
        run_wfa_mc(asset, bars_full, top)

    if not all_results:
        print("\nNo results."); return

    combined = pd.concat(all_results, ignore_index=True)
    combined.to_csv(os.path.join(OUTPUT_DIR, "sweep_all.csv"), index=False)

    print(f"\n\n{'='*64}")
    print("FINAL RECOMMENDATIONS  (best Sharpe, WR >= 50%)")
    print(f"{'='*64}")
    for asset in ["BTC", "ETH", "SOL", "XRP"]:
        sub = combined[combined["asset"] == asset]
        if sub.empty: print(f"  {asset}: no data"); continue
        q = sub[sub["win_rate"] >= 0.50] if (sub["win_rate"] >= 0.50).any() else sub
        best = q.nlargest(1, "sharpe").iloc[0]
        print(f"  {asset:3s}  period={int(best['period']):2d}  mult={best['mult']:.1f}  "
              f"lookback={int(best['lookback'])}  "
              f"n={int(best['n_trades']):>5}  WR={best['win_rate']:.1%}  "
              f"sharpe={best['sharpe']:+.3f}")
    print()


if __name__ == "__main__":
    run()
