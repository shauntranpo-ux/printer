"""
BTC-led ETH direction model: can BTC's path at t=10min predict ETH's close
direction, independent of where ETH currently sits?

Key question: when BTC is strongly positive (dist>0.5%) but ETH is near/below
its strike (YES~40-50c), does ETH eventually close above strike?
If yes -> we can enter at cheap prices based on BTC signal, not ETH's current side.

Features at t=10min:
  btc_dist_pct   : signed % from BTC window-open price (BTC_strike proxy)
  btc_cross      : number of BTC strike crossings in [0, 10min]
  eth_dist_pct   : signed % from ETH strike
  eth_cross      : number of ETH strike crossings in [0, 10min]
  eth_rvol       : realized vol (std of log-returns) * 100 in first 10 min
  same_dir       : ETH and BTC on same side of their respective strikes
  btc_vel        : BTC velocity (net log-return / elapsed) * 1000 per-second
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from collections import defaultdict
from datetime import datetime, timezone
from strategies.backtest.hourly_window_generator import generate_hourly_events
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


def rvol(prices) -> float:
    if len(prices) < 3:
        return 0.0
    return float(np.std(np.diff(np.log(prices + 1e-12)))) * 100.0


def velocity(prices, elapsed_sec: float) -> float:
    """Log-return over elapsed time, in units per 1000 seconds."""
    if len(prices) < 2 or elapsed_sec <= 0:
        return 0.0
    return float(np.log(prices[-1] / (prices[0] + 1e-12)) / elapsed_sec) * 1000.0


def pnl_for(won: bool, entry_c: float) -> float:
    frac = entry_c / 100.0
    if won:
        return (STAKE / frac) * (1 - frac) * (1 - FEE_RATE)
    return -STAKE


def utc_hour(ts: float) -> int:
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).hour


def analyse(eth_events, eth_ts, eth_cl, btc_ts, btc_cl, label):
    eth_lookup = defaultdict(list)
    for ev in eth_events:
        eth_lookup[ev.window_start_ts].append(ev)

    rows = []
    for wts, evs in eth_lookup.items():
        mid_ev = None
        for ev in evs:
            if EVAL_MIN_SEC <= ev.elapsed_seconds <= EVAL_MAX_SEC:
                mid_ev = ev
                break
        if mid_ev is None:
            continue

        elapsed = mid_ev.elapsed_seconds

        eth_w = get_prices(eth_ts, eth_cl, wts, mid_ev.eval_ts)
        if len(eth_w) < 5:
            continue

        eth_xc     = cross_count(eth_w, mid_ev.strike)
        eth_dist   = (eth_w[-1] - mid_ev.strike) / mid_ev.strike * 100.0
        eth_rv     = rvol(eth_w)
        eth_vel    = velocity(eth_w, elapsed)
        eth_itm    = eth_dist > 0
        eth_entry  = mid_ev.orderbook.yes_ask if eth_itm else mid_ev.orderbook.no_ask
        eth_won_yes = mid_ev.close_price > mid_ev.strike

        btc_at_start = get_prices(btc_ts, btc_cl, wts - 60, wts + 120)
        if len(btc_at_start) == 0:
            continue
        btc_strike = float(btc_at_start[0])
        btc_w = get_prices(btc_ts, btc_cl, wts, mid_ev.eval_ts)
        if len(btc_w) < 5:
            continue

        btc_xc   = cross_count(btc_w, btc_strike)
        btc_dist = (btc_w[-1] - btc_strike) / btc_strike * 100.0
        btc_vel  = velocity(btc_w, elapsed)
        btc_itm  = btc_dist > 0

        # BTC-direction signal: trade ETH in BTC's direction
        btc_says_yes = btc_itm   # BTC predicts ETH should close YES
        eth_entry_btc = mid_ev.orderbook.yes_ask if btc_says_yes else mid_ev.orderbook.no_ask
        won_btc_signal = eth_won_yes if btc_says_yes else (not eth_won_yes)

        rows.append({
            "eth_xc":        eth_xc,
            "eth_dist":      eth_dist,
            "eth_itm":       eth_itm,
            "eth_rv":        eth_rv,
            "eth_vel":       eth_vel,
            "eth_entry":     eth_entry,
            "eth_won_yes":   eth_won_yes,
            "btc_xc":        btc_xc,
            "btc_dist":      btc_dist,
            "btc_itm":       btc_itm,
            "btc_vel":       btc_vel,
            "same_dir":      eth_itm == btc_itm,
            "btc_entry":     eth_entry_btc,   # entry when following BTC direction
            "btc_won":       won_btc_signal,
            "pnl_btc":       pnl_for(won_btc_signal, eth_entry_btc),
        })

    if not rows:
        return

    df = pd.DataFrame(rows)
    ts_min = min(e.eval_ts for e in eth_events)
    ts_max = max(e.eval_ts for e in eth_events)
    weeks  = (ts_max - ts_min) / (86400 * 7)

    def stat(sub, lbl, indent=2):
        n = len(sub)
        if n < 10:
            return
        wr   = sub["btc_won"].mean()
        avgE = sub["btc_entry"].mean()
        totP = sub["pnl_btc"].sum()
        ev   = totP / n
        wkP  = totP / weeks
        wkN  = n / weeks
        avgD_btc = sub["btc_dist"].abs().mean()
        avgD_eth = sub["eth_dist"].abs().mean()
        print(f"{'  '*indent}{lbl:<56}  n={n:>5} ({wkN:>4.1f}/wk)"
              f"  WR={wr*100:>5.1f}%  E={avgE:>5.1f}c  EV={ev:>+6.2f}  ${wkP:>+7.1f}/wk"
              f"  btcD={avgD_btc:.2f}%  ethD={avgD_eth:.2f}%")

    print(f"\n{'='*90}")
    print(f"  {label}  —  ETH hourly t=10min, BTC-LED direction signal")
    print(f"{'='*90}")

    # 1. Baseline: just follow BTC direction regardless of anything
    print(f"\n  1. BASELINE — follow BTC direction at t=10min, any conditions")
    stat(df, "all windows, BTC direction")
    stat(df[df["btc_xc"] == 0], "BTC cross=0")
    stat(df[(df["btc_xc"] == 0) & (df["same_dir"])], "BTC cross=0, same_dir")
    stat(df[(df["btc_xc"] == 0) & (~df["same_dir"])], "BTC cross=0, DIVERGED (ETH!=BTC side)")

    # 2. BTC dist buckets (cross=0 only)
    print(f"\n  2. BTC DIST BUCKETS (BTC cross=0, follow BTC direction)")
    bz = df[df["btc_xc"] == 0]
    for lo, hi in [(0.0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 1.0), (1.0, 5.0)]:
        sub = bz[bz["btc_dist"].abs().between(lo, hi)]
        stat(sub, f"  |btc_dist| in [{lo:.1f}%, {hi:.1f}%)")

    # 3. Diverged case: BTC trending strongly, ETH on OTHER side (cheap entries!)
    print(f"\n  3. DIVERGED — BTC cross=0, ETH on opposite side (follow BTC)")
    div = df[(df["btc_xc"] == 0) & (~df["same_dir"])]
    stat(div, "all diverged")
    for lo, hi in [(0.0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 1.5)]:
        sub = div[div["btc_dist"].abs().between(lo, hi)]
        stat(sub, f"  |btc_dist| [{lo:.1f}%, {hi:.1f}%)  [BTC strongly away, ETH lagging]")

    # 4. ETH entry price buckets in diverged case
    print(f"\n  4. DIVERGED entry price buckets (BTC cross=0, ETH!=BTC side)")
    for lo, hi in [(30, 45), (45, 55), (55, 65), (65, 75)]:
        sub = div[(div["btc_entry"] >= lo) & (div["btc_entry"] < hi)]
        stat(sub, f"  entry [{lo}-{hi}c)")

    # 5. ETH rvol filter on diverged
    print(f"\n  5. DIVERGED + ETH rvol quartiles")
    if len(div) >= 20:
        qs = div["eth_rv"].quantile([0, 0.25, 0.5, 0.75, 1.0]).values
        labels = ["Q1 (low vol)", "Q2", "Q3", "Q4 (high vol)"]
        for i in range(4):
            sub = div[(div["eth_rv"] >= qs[i]) & (div["eth_rv"] <= qs[i+1] + 1e-9)]
            stat(sub, f"  rvol {labels[i]} [{qs[i]:.3f},{qs[i+1]:.3f}]%")

    # 6. BTC velocity filter (how fast is BTC moving?)
    print(f"\n  6. BTC VELOCITY + DIVERGED (BTC cross=0, ETH lagging)")
    bz2 = df[(df["btc_xc"] == 0) & (~df["same_dir"])]
    for thr in [0.5, 1.0, 1.5, 2.0]:
        sub = bz2[bz2["btc_vel"].abs() >= thr]
        stat(sub, f"  |btc_vel| >= {thr:.1f}/ks")

    # 7. Combined best: BTC cross=0, dist>0.3%, diverged, high rvol
    print(f"\n  7. COMBINED BEST SIGNAL (BTC cross=0, |btc_dist|>=0.3%, diverged)")
    best = df[(df["btc_xc"] == 0) & (df["btc_dist"].abs() >= 0.3) & (~df["same_dir"])]
    stat(best, "BTC_cross=0 + |btc_dist|>=0.3% + diverged")
    stat(best[best["eth_xc"] == 0], "  + ETH cross=0")
    stat(best[best["eth_rv"] > div["eth_rv"].median() if len(div) > 0 else 0],
         "  + ETH rvol above median")

    # 8. Same-direction for comparison (current strategy)
    print(f"\n  8. SAME-DIR (current strategy) vs DIVERGED (new BTC-led signal)")
    same = df[(df["btc_xc"] == 0) & (df["eth_xc"] == 0) & (df["same_dir"])]
    stat(same, "same_dir (current) — ETH entry")
    stat(div, "diverged (NEW) — enter in BTC direction at ETH price")


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
