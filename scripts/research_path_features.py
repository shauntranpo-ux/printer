"""
Research: Full-window path features for ETH/BTC hourly Kalshi binaries.

Hypothesis: The ENTIRE price path within the window contains far more signal
than just the current price at one point in time.

Analyzing:
  1. Continuous time above/below strike ("dwelling time")
  2. Price trajectory slope (is it trending toward strike or away)
  3. Cross-count: how many times has ETH crossed the strike
  4. Time-of-day conditioning (UTC hour)
  5. Volatility regime: low vs high realized vol changes outcomes
  6. Entry opportunity: at t=30-40min, find windows where we can enter
     at 60-75c with 90%+ WR using path features
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


def ts(s: str) -> float:
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


def ev(wr: float, entry_c: float) -> float:
    entry = entry_c / 100
    win_pnl  = (STAKE / entry) * (1 - entry) * (1 - FEE_RATE)
    loss_pnl = -STAKE
    return wr * win_pnl + (1 - wr) * loss_pnl


def utc_hour(ts_: float) -> int:
    return datetime.fromtimestamp(ts_, tz=timezone.utc).hour


def get_prices_in_window(eth_df: pd.DataFrame, window_start: float,
                          eval_ts: float, strike: float) -> dict:
    """
    Get all 1-min prices from window_start to eval_ts and compute path features.
    Returns dict with dwelling_time_pct, slope, cross_count, etc.
    """
    mask = (eth_df["timestamp"] >= window_start) & (eth_df["timestamp"] <= eval_ts)
    prices = eth_df[mask]["close"].values
    if len(prices) < 3:
        return {}

    above = prices > strike
    n = len(prices)

    # -- Dwelling time: fraction of window above/below strike ----------------
    dwell_above = above.mean()  # 1.0 = entire window above strike

    # -- Continuous streak: how long is the CURRENT streak (above or below) --
    current_dir = above[-1]
    streak = 0
    for i in range(n - 1, -1, -1):
        if above[i] == current_dir:
            streak += 1
        else:
            break
    streak_frac = streak / n  # fraction of window in current streak

    # -- Cross count: how many times has ETH crossed the strike -------------
    cross_count = int(np.sum(np.diff(above.astype(int)) != 0))

    # -- Slope: linear trend of price over the window (normalized by strike) -
    x = np.arange(n)
    slope_pct = np.polyfit(x, prices / strike * 100 - 100, 1)[0]  # %/min

    # -- Distance from strike at current price -------------------------------
    pct = (prices[-1] - strike) / strike * 100

    # -- Realized vol (std of 1-min returns, annualized-ish) -----------------
    if len(prices) >= 5:
        rets = np.diff(np.log(prices))
        rvol = rets.std() * 100  # in % per minute
    else:
        rvol = None

    return {
        "dwell_above":   dwell_above,
        "dwell_itm":     dwell_above if pct > 0 else (1 - dwell_above),
        "streak_frac":   streak_frac,
        "cross_count":   cross_count,
        "slope_pct":     slope_pct,
        "pct":           pct,
        "rvol":          rvol,
        "n_minutes":     n,
    }


def run_analysis(events: list, eth_df: pd.DataFrame, label: str) -> None:
    # Group events by window
    windows: dict = defaultdict(list)
    for ev_ in events:
        windows[ev_.window_start_ts].append(ev_)

    days = 0.0
    all_windows = []

    for wts, evs in windows.items():
        evs_sorted = sorted(evs, key=lambda e: e.eval_ts)
        # Use the eval at t=35-45min for analysis
        target_elapsed_min = 40  # look at the t=40min eval
        best_ev = min(evs_sorted,
                      key=lambda e: abs(e.elapsed_seconds - target_elapsed_min * 60))

        pf = get_prices_in_window(eth_df, wts, best_ev.eval_ts, best_ev.strike)
        if not pf:
            continue

        won_yes = best_ev.close_price > best_ev.strike
        itm_at_eval = pf["pct"] > 0

        all_windows.append({
            "ts":          wts,
            "hour":        utc_hour(wts),
            "pct":         pf["pct"],
            "dwell_itm":   pf["dwell_itm"],
            "streak_frac": pf["streak_frac"],
            "cross_count": pf["cross_count"],
            "slope":       pf["slope_pct"],
            "rvol":        pf.get("rvol", 0),
            "yes_ask":     best_ev.orderbook.yes_ask,
            "no_ask":      best_ev.orderbook.no_ask,
            "won_yes":     won_yes,
            "itm":         itm_at_eval,
        })

    if all_windows:
        tss = [w["ts"] for w in all_windows]
        days = (max(tss) - min(tss)) / 86400
    weeks = max(days / 7, 1)

    print(f"\n{'='*72}")
    print(f"  {label}  —  {len(all_windows):,} windows  ({days:.0f} days)")
    print(f"{'='*72}")

    # ── Table 1: WR by dwelling time (% of window ITM before t=40min) ────────
    print(f"\n  TABLE 1: WR by ITM dwelling time at t=40min")
    print(f"  (what fraction of the window has ETH been on the winning side?)")
    print(f"  {'Dwell ITM':>16}   {'N':>6}   {'WR%':>6}   {'AvgEntry':>9}   {'EV/trade':>10}   {'$/wk':>8}")
    buckets_dwell = [
        (0.0, 0.5,  "< 50% of window"),
        (0.5, 0.7,  "50-70% of window"),
        (0.7, 0.85, "70-85% of window"),
        (0.85, 0.95,"85-95% of window"),
        (0.95, 1.01,"95-100% (dominant)"),
    ]
    for lo, hi, lbl in buckets_dwell:
        subset = [w for w in all_windows
                  if lo <= w["dwell_itm"] < hi and w["itm"]]
        if not subset:
            continue
        n   = len(subset)
        wr  = sum(1 for w in subset if w["won_yes"] == w["itm"]) / n
        ae  = sum(w["yes_ask"] if w["itm"] else w["no_ask"] for w in subset) / n
        ev_ = ev(wr, ae)
        print(f"  {lbl:>16}   {n:>6}   {wr*100:>6.1f}%   {ae:>8.1f}c   "
              f"{ev_:>+10.2f}   {ev_ * n / weeks:>+8.1f}/wk")

    # ── Table 2: WR by continuous streak length ───────────────────────────────
    print(f"\n  TABLE 2: WR by continuous streak fraction at t=40min")
    print(f"  (how long has ETH been on ONE SIDE without crossing strike?)")
    print(f"  {'Streak':>18}   {'N':>6}   {'WR%':>6}   {'AvgEntry':>9}   {'EV/trade':>10}   {'$/wk':>8}")
    buckets_streak = [
        (0.0, 0.3,  "< 30% streak"),
        (0.3, 0.5,  "30-50% streak"),
        (0.5, 0.7,  "50-70% streak"),
        (0.7, 0.9,  "70-90% streak"),
        (0.9, 1.01, "90-100% (no cross)"),
    ]
    for lo, hi, lbl in buckets_streak:
        subset = [w for w in all_windows
                  if lo <= w["streak_frac"] < hi and w["itm"]]
        if not subset:
            continue
        n   = len(subset)
        wr  = sum(1 for w in subset if w["won_yes"] == w["itm"]) / n
        ae  = sum(w["yes_ask"] if w["itm"] else w["no_ask"] for w in subset) / n
        ev_ = ev(wr, ae)
        print(f"  {lbl:>18}   {n:>6}   {wr*100:>6.1f}%   {ae:>8.1f}c   "
              f"{ev_:>+10.2f}   {ev_ * n / weeks:>+8.1f}/wk")

    # ── Table 3: WR by cross count ────────────────────────────────────────────
    print(f"\n  TABLE 3: WR by strike cross count at t=40min (ITM windows only)")
    print(f"  (how many times has price crossed the strike during the window?)")
    print(f"  {'Crosses':>10}   {'N':>6}   {'WR%':>6}   {'AvgEntry':>9}   {'EV/trade':>10}   {'$/wk':>8}")
    for max_cross in [0, 1, 2, 3, 4]:
        subset = [w for w in all_windows if w["cross_count"] == max_cross and w["itm"]]
        if len(subset) < 10:
            continue
        n   = len(subset)
        wr  = sum(1 for w in subset if w["won_yes"] == w["itm"]) / n
        ae  = sum(w["yes_ask"] if w["itm"] else w["no_ask"] for w in subset) / n
        ev_ = ev(wr, ae)
        print(f"  {'0 crosses' if max_cross == 0 else f'{max_cross} cross(es)':>10}   "
              f"{n:>6}   {wr*100:>6.1f}%   {ae:>8.1f}c   "
              f"{ev_:>+10.2f}   {ev_ * n / weeks:>+8.1f}/wk")

    # ── Table 4: Time-of-day (UTC hour) ────────────────────────────────────────
    print(f"\n  TABLE 4: WR by UTC hour (ITM windows, all at t=40min)")
    print(f"  {'Hour (UTC)':>12}   {'N':>5}   {'WR%':>6}   {'AvgEntry':>9}   {'EV/trade':>10}   {'$/wk':>8}")
    by_hour: dict = defaultdict(list)
    for w in all_windows:
        if w["itm"]:
            by_hour[w["hour"]].append(w)
    for h in range(24):
        subset = by_hour[h]
        if len(subset) < 20:
            continue
        n   = len(subset)
        wr  = sum(1 for w in subset if w["won_yes"] == w["itm"]) / n
        ae  = sum(w["yes_ask"] if w["itm"] else w["no_ask"] for w in subset) / n
        ev_ = ev(wr, ae)
        print(f"  {h:>4}:00 UTC   {n:>5}   {wr*100:>6.1f}%   {ae:>8.1f}c   "
              f"{ev_:>+10.2f}   {ev_ * n / weeks:>+8.1f}/wk")

    # ── Table 5: GRID — dwell + streak + entry timing ────────────────────────
    print(f"\n  TABLE 5: Grid search — dwell>=X% AND streak>=Y% at t=30-40min")
    print(f"  (entry before t=45 with FULL PATH confirmation)")
    print(f"  {'Dwell>=':>8}  {'Streak>=':>8}   {'N':>6}   {'WR%':>6}   "
          f"{'AvgEntry':>9}   {'EV/trade':>10}   {'$/wk':>8}")
    best_grid = []
    for dwell_th in [0.70, 0.80, 0.90, 0.95]:
        for streak_th in [0.50, 0.65, 0.75, 0.85]:
            subset = [w for w in all_windows
                      if w["dwell_itm"] >= dwell_th
                      and w["streak_frac"] >= streak_th
                      and w["itm"]]
            if len(subset) < 20:
                continue
            n   = len(subset)
            wr  = sum(1 for w in subset if w["won_yes"] == w["itm"]) / n
            ae  = sum(w["yes_ask"] if w["itm"] else w["no_ask"] for w in subset) / n
            ev_ = ev(wr, ae)
            best_grid.append((ev_, n, wr, ae, dwell_th, streak_th))
    best_grid.sort(key=lambda x: -x[0])
    for ev_, n, wr, ae, d, s in best_grid[:12]:
        ev_wk = ev_ * n / weeks
        print(f"  {d*100:>7.0f}%  {s*100:>7.0f}%   {n:>6}   {wr*100:>6.1f}%   "
              f"{ae:>8.1f}c   {ev_:>+10.2f}   {ev_wk:>+8.1f}/wk")

    # ── Table 6: Early entry with path confirmation (t=30-40min, not t=45) ───
    print(f"\n  TABLE 6: Entry at t=30-40min with dwell>=85% + streak>=65%")
    print(f"  (earlier entry = lower price, same high confidence)")
    # Re-run using t=35min eval instead of t=40min
    windows2: dict = defaultdict(list)
    for ev_ in events:
        windows2[ev_.window_start_ts].append(ev_)
    early_windows = []
    for wts, evs in windows2.items():
        evs_sorted = sorted(evs, key=lambda e: e.eval_ts)
        target = 35  # t=35min
        best = min(evs_sorted, key=lambda e: abs(e.elapsed_seconds - target * 60))
        pf = get_prices_in_window(eth_df, wts, best.eval_ts, best.strike)
        if not pf:
            continue
        won_yes = best.close_price > best.strike
        itm = pf["pct"] > 0
        if not itm:
            continue
        early_windows.append({
            "dwell_itm":   pf["dwell_itm"],
            "streak_frac": pf["streak_frac"],
            "cross_count": pf["cross_count"],
            "yes_ask":     best.orderbook.yes_ask,
            "no_ask":      best.orderbook.no_ask,
            "won_yes":     won_yes,
            "itm":         itm,
            "pct":         pf["pct"],
        })
    for dwell_th in [0.80, 0.85, 0.90]:
        for streak_th in [0.60, 0.70, 0.80]:
            subset = [w for w in early_windows
                      if w["dwell_itm"] >= dwell_th
                      and w["streak_frac"] >= streak_th]
            if len(subset) < 20:
                continue
            n   = len(subset)
            wr  = sum(1 for w in subset if w["won_yes"]) / n
            ae  = sum(w["yes_ask"] for w in subset) / n
            ev_ = ev(wr, ae)
            ev_wk = ev_ * n / weeks
            print(f"  dwell>={dwell_th*100:.0f}% streak>={streak_th*100:.0f}%  "
                  f"N={n:>5}  WR={wr*100:>5.1f}%  entry={ae:>5.1f}c  "
                  f"EV={ev_:>+6.2f}  {ev_wk:>+7.1f}/wk")


def main():
    print("Loading ETH prices...")
    eth_df = load_prices("ETH")

    for label, start_str, end_str in PERIODS:
        start, end = ts(start_str), ts(end_str)
        print(f"\nGenerating events for {label}...", end=" ", flush=True)
        events = list(generate_hourly_events(eth_df, "ETH", start, end, seed=42))
        print(f"{len(events):,} events")
        run_analysis(events, eth_df, label)


if __name__ == "__main__":
    main()
