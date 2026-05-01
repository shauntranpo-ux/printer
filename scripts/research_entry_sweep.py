"""
Sweep all eval times t=5,10,15,20,25min for the dual cross=0 + same direction signal.
Shows entry price, WR, and EV at each time point to find optimal cheap-entry window.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from collections import defaultdict
from datetime import datetime, timezone
# hourly_window_generator removed (dead code)
import pandas as pd
import numpy as np

STAKE    = 25.0
FEE_RATE = 0.07

PERIODS = [
    ("TRAIN 2022-2023", "2022-01-01", "2023-12-31"),
    ("TEST  2024-2026", "2024-01-01", "2026-04-15"),
]

EVAL_TIMES_SEC = [300, 600, 900, 1200, 1500]   # t=5,10,15,20,25min


def ts_from(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def load_prices(asset: str) -> pd.DataFrame:
    for name in [f"{asset}_1m_extended.parquet", f"{asset}_1m_2026.parquet"]:
        p = Path(f"data/historical/{name}")
        if p.exists():
            df = pd.read_parquet(p)
            if "open_time" in df.columns and "timestamp" not in df.columns:
                df["timestamp"] = (
                    df["open_time"].values.astype("datetime64[s]").astype("int64").astype("float64")
                )
            return df.sort_values("timestamp").reset_index(drop=True)[["timestamp", "close"]]
    raise FileNotFoundError(asset)


def build_index(df: pd.DataFrame):
    ts_arr = df["timestamp"].values.astype(np.int64)
    cl_arr = df["close"].values.astype(np.float64)
    idx    = np.argsort(ts_arr)
    return ts_arr[idx], cl_arr[idx]


def window_prices(ts_arr, cl_arr, t_start, t_end):
    lo = int(np.searchsorted(ts_arr, int(t_start), side="left"))
    hi = int(np.searchsorted(ts_arr, int(t_end),   side="right"))
    return cl_arr[lo:hi]


def cross_count(prices, strike):
    if len(prices) < 2:
        return 999
    above = prices > strike
    return int(np.sum(np.diff(above.astype(np.int8)) != 0))


def rvol(prices):
    if len(prices) < 2:
        return 0.0
    return float(np.std(np.diff(np.log(prices + 1e-12)))) * 100.0


def pnl_for(won, entry_c):
    frac = entry_c / 100.0
    if won:
        return (STAKE / frac) * (1 - frac) * (1 - FEE_RATE)
    return -STAKE


def analyse(eth_events, eth_ts, eth_cl, btc_ts, btc_cl, period_label):
    eth_lookup = defaultdict(dict)
    for ev in eth_events:
        bucket = round(ev.elapsed_seconds / 300) * 300
        eth_lookup[ev.window_start_ts][bucket] = ev

    rows = []
    for wts, eth_buckets in eth_lookup.items():
        btc_at_start = window_prices(btc_ts, btc_cl, wts - 60, wts + 120)
        btc_strike   = float(btc_at_start[0]) if len(btc_at_start) > 0 else None

        for target in EVAL_TIMES_SEC:
            ev = eth_buckets.get(target)
            if ev is None:
                continue

            eth_w    = window_prices(eth_ts, eth_cl, wts, ev.eval_ts)
            eth_xc   = cross_count(eth_w, ev.strike)
            eth_itm  = bool(len(eth_w) > 0 and eth_w[-1] > ev.strike)
            eth_rv   = rvol(eth_w)

            entry_c  = ev.orderbook.yes_ask if eth_itm else ev.orderbook.no_ask
            won_yes  = ev.close_price > ev.strike
            won      = (eth_itm and won_yes) or (not eth_itm and not won_yes)

            btc_xc  = None
            btc_itm = None
            if btc_strike is not None:
                btc_w   = window_prices(btc_ts, btc_cl, wts, ev.eval_ts)
                if len(btc_w) >= 3:
                    btc_xc  = cross_count(btc_w, btc_strike)
                    btc_itm = bool(btc_w[-1] > btc_strike)

            rows.append({
                "t":       target // 60,
                "entry":   entry_c,
                "won":     won,
                "eth_xc":  eth_xc,
                "eth_rv":  eth_rv,
                "eth_itm": eth_itm,
                "btc_xc":  btc_xc,
                "btc_itm": btc_itm,
                "pnl":     pnl_for(won, entry_c),
            })

    if not rows:
        return
    df  = pd.DataFrame(rows)
    ts_min = min(e.eval_ts for e in eth_events)
    ts_max = max(e.eval_ts for e in eth_events)
    weeks  = (ts_max - ts_min) / (86400 * 7)

    def stat(sub, label, indent=2):
        n = len(sub)
        if n < 10:
            return
        wr    = sub["won"].mean()
        avgE  = sub["entry"].mean()
        totP  = sub["pnl"].sum()
        evT   = totP / n
        wkP   = totP / weeks
        wk_n  = n / weeks
        print(f"{' '*indent}{label:<52}  n={n:>6} ({wk_n:>5.1f}/wk)"
              f"  WR={wr*100:>5.1f}%  E={avgE:>5.1f}c  EV=${evT:>+6.2f}  ${wkP:>+7.1f}/wk")

    print(f"\n{'='*80}")
    print(f"  {period_label}")
    print(f"{'='*80}")

    same_dir = df["eth_itm"] == df["btc_itm"]
    both_zero = (df["eth_xc"] == 0) & (df["btc_xc"] == 0)

    print(f"\n  BASELINE (all cross=0, any direction):")
    for t in [5, 10, 15, 20, 25]:
        sub = df[(df["t"] == t) & (df["eth_xc"] == 0)]
        stat(sub, f"t={t:2d}min  ETH_cross=0  (all)")

    print(f"\n  DUAL ZERO + SAME DIRECTION (primary signal):")
    for t in [5, 10, 15, 20, 25]:
        sub = df[(df["t"] == t) & both_zero & same_dir]
        stat(sub, f"t={t:2d}min  ETH+BTC cross=0  same_dir")

    # rvol quartiles within dual-zero same-dir at t=10
    print(f"\n  DUAL ZERO + SAME DIR + RVOL BUCKETS (t=10min):")
    sub10 = df[(df["t"] == 10) & both_zero & same_dir]
    if len(sub10) >= 20:
        qs = sub10["eth_rv"].quantile([0, 0.25, 0.5, 0.75, 1.0]).values
        labels = ["Q1-low", "Q2", "Q3", "Q4-high"]
        for i in range(4):
            lo, hi = qs[i], qs[i+1]
            cut = sub10[(sub10["eth_rv"] >= lo) & (sub10["eth_rv"] <= hi + 1e-9)]
            stat(cut, f"t=10  dual_zero  rvol {labels[i]} [{lo:.3f},{hi:.3f}]%")

    # entry price buckets at t=10min dual-zero
    print(f"\n  ENTRY PRICE BUCKETS at t=10min (dual zero, same dir):")
    sub10 = df[(df["t"] == 10) & both_zero & same_dir]
    for lo, hi in [(50, 60), (60, 65), (65, 70), (70, 75), (75, 80), (80, 90)]:
        cut = sub10[(sub10["entry"] >= lo) & (sub10["entry"] < hi)]
        if len(cut) < 5:
            continue
        stat(cut, f"t=10  dual_zero  entry [{lo}-{hi}c)")

    # entry price buckets at t=5min
    print(f"\n  ENTRY PRICE BUCKETS at t=5min (dual zero, same dir):")
    sub5 = df[(df["t"] == 5) & both_zero & same_dir]
    for lo, hi in [(45, 55), (55, 60), (60, 65), (65, 70), (70, 80)]:
        cut = sub5[(sub5["entry"] >= lo) & (sub5["entry"] < hi)]
        if len(cut) < 5:
            continue
        stat(cut, f"t=5   dual_zero  entry [{lo}-{hi}c)")


def main():
    print("Loading data and building indices...")
    eth_df = load_prices("ETH")
    btc_df = load_prices("BTC")
    eth_ts, eth_cl = build_index(eth_df)
    btc_ts, btc_cl = build_index(btc_df)

    for label, start_str, end_str in PERIODS:
        start, end = ts_from(start_str), ts_from(end_str)
        print(f"Generating ETH events {label}...", end=" ", flush=True)
        eth_events = list(generate_hourly_events(eth_df, "ETH", start, end, seed=42))
        print(f"{len(eth_events):,}")
        analyse(eth_events, eth_ts, eth_cl, btc_ts, btc_cl, label)


if __name__ == "__main__":
    main()
