"""
Distance-from-strike sweep for t=10min dual-cross=0 same-direction signal.

Hypothesis: at t=10min, price barely above/below strike is near-random noise.
A meaningful distance (like the late-window 0.3% filter) should be required —
but scaled for the remaining 50min window vs 15min.

Tests thresholds: 0.0, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0%
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

EVAL_MIN_SEC = 550
EVAL_MAX_SEC = 650
SKIP_HOURS   = {12, 13}
MAX_ENTRY    = 79.9

DIST_THRESHOLDS = [0.0, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0]


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
    ts = df["timestamp"].values.astype(np.int64)
    cl = df["close"].values.astype(np.float64)
    idx = np.argsort(ts)
    return ts[idx], cl[idx]


def get_prices(ts_arr, cl_arr, t0, t1):
    lo = int(np.searchsorted(ts_arr, int(t0), side="left"))
    hi = int(np.searchsorted(ts_arr, int(t1), side="right"))
    return cl_arr[lo:hi]


def cross_count(prices, strike: float) -> int:
    if len(prices) < 2:
        return 999
    above = prices > strike
    return int(np.sum(np.diff(above.astype(np.int8)) != 0))


def utc_hour(ts: float) -> int:
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).hour


def pnl_for(won: bool, entry_c: float) -> float:
    frac = entry_c / 100.0
    if won:
        return (STAKE / frac) * (1 - frac) * (1 - FEE_RATE)
    return -STAKE


def analyse(eth_events, eth_ts, eth_cl, btc_ts, btc_cl, period_label):
    eth_lookup = defaultdict(list)
    for ev in eth_events:
        eth_lookup[ev.window_start_ts].append(ev)

    rows = []
    for wts, evs in eth_lookup.items():
        if utc_hour(wts) in SKIP_HOURS:
            continue

        mid_ev = None
        for ev in evs:
            if EVAL_MIN_SEC <= ev.elapsed_seconds <= EVAL_MAX_SEC:
                mid_ev = ev
                break
        if mid_ev is None:
            continue

        eth_w = get_prices(eth_ts, eth_cl, wts, mid_ev.eval_ts)
        if len(eth_w) < 5:
            continue
        if cross_count(eth_w, mid_ev.strike) > 0:
            continue
        eth_itm = bool(eth_w[-1] > mid_ev.strike)
        eth_dist_pct = abs(eth_w[-1] - mid_ev.strike) / mid_ev.strike * 100.0

        btc_at_start = get_prices(btc_ts, btc_cl, wts - 60, wts + 120)
        if len(btc_at_start) == 0:
            continue
        btc_strike = float(btc_at_start[0])
        btc_w = get_prices(btc_ts, btc_cl, wts, mid_ev.eval_ts)
        if len(btc_w) < 3:
            continue
        if cross_count(btc_w, btc_strike) > 0:
            continue
        btc_itm = bool(btc_w[-1] > btc_strike)

        if eth_itm != btc_itm:
            continue

        side    = "YES" if eth_itm else "NO"
        entry_c = mid_ev.orderbook.yes_ask if eth_itm else mid_ev.orderbook.no_ask
        if entry_c > MAX_ENTRY:
            continue

        won_yes = mid_ev.close_price > mid_ev.strike
        won     = (side == "YES" and won_yes) or (side == "NO" and not won_yes)

        rows.append({
            "dist":  eth_dist_pct,
            "entry": entry_c,
            "won":   won,
            "pnl":   pnl_for(won, entry_c),
        })

    if not rows:
        return

    df = pd.DataFrame(rows)
    ts_min = min(e.eval_ts for e in eth_events)
    ts_max = max(e.eval_ts for e in eth_events)
    weeks  = (ts_max - ts_min) / (86400 * 7)

    print(f"\n{'='*80}")
    print(f"  {period_label}  —  t=10min dual-zero same-dir, entry<80c")
    print(f"{'='*80}")
    print(f"  {'Threshold':<14}  {'n':>6}  {'n/wk':>6}  {'WR':>7}  {'AvgE':>6}  {'EV/t':>8}  {'$/wk':>9}  {'AvgDist':>8}")
    print(f"  {'-'*80}")

    for thr in DIST_THRESHOLDS:
        sub = df[df["dist"] >= thr]
        n   = len(sub)
        if n < 5:
            print(f"  dist>={thr:.1f}%       n too small")
            continue
        wr   = sub["won"].mean()
        avgE = sub["entry"].mean()
        totP = sub["pnl"].sum()
        ev   = totP / n
        wkP  = totP / weeks
        wkN  = n / weeks
        avgD = sub["dist"].mean()
        print(f"  dist>={thr:.1f}%       {n:>6}  {wkN:>6.1f}  {wr*100:>6.1f}%  {avgE:>6.1f}c  {ev:>+8.2f}  {wkP:>+9.1f}  {avgD:>7.2f}%")

    # Also show dist buckets for the no-threshold case
    print(f"\n  Distance buckets (no threshold):")
    print(f"  {'Dist bucket':<18}  {'n':>6}  {'n/wk':>6}  {'WR':>7}  {'AvgE':>6}  {'EV/t':>8}")
    for lo, hi in [(0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0), (1.0, 1.5), (1.5, 5.0)]:
        sub = df[(df["dist"] >= lo) & (df["dist"] < hi)]
        if len(sub) < 5:
            continue
        wr   = sub["won"].mean()
        avgE = sub["entry"].mean()
        ev   = sub["pnl"].sum() / len(sub)
        wkN  = len(sub) / weeks
        print(f"  [{lo:.1f}%, {hi:.1f}%)        {len(sub):>6}  {wkN:>6.1f}  {wr*100:>6.1f}%  {avgE:>6.1f}c  {ev:>+8.2f}")


def main():
    print("Loading data...")
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
