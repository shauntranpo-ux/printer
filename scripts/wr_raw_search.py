"""
Search ALL raw eval events (before EV filter) for conditions where
actual close rate >= 90%. No strategy involved — pure empirical.

Measures: given these observable features at eval time, what % of windows
closed above/below strike? This is ground truth WR for any strategy
that would exclusively trade those conditions.
"""
import sys, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from collections import defaultdict
from datetime import datetime, timezone
from strategies.backtest.hourly_window_generator import generate_hourly_events, realized_vol_from_history
import numpy as np
import pandas as pd


def ts(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def load_prices(asset):
    extended = Path(f"data/historical/{asset}_1m_extended.parquet")
    legacy   = Path(f"data/historical/{asset}_1m_2026.parquet")
    path = extended if extended.exists() else legacy
    df = pd.read_parquet(path)
    if "open_time" in df.columns and "timestamp" not in df.columns:
        df["timestamp"] = df["open_time"].values.astype("datetime64[s]").astype("int64").astype("float64")
    return df.sort_values("timestamp").reset_index(drop=True)[["timestamp", "close"]]


def _lead_return(history, window_sec):
    if not history:
        return None
    now_ts = history[-1][0]
    cutoff = now_ts - window_sec
    anchor = None
    for ts, p in history:
        if ts >= cutoff:
            anchor = p
            break
    if anchor is None or anchor <= 0 or history[-1][1] <= 0:
        return None
    return math.log(history[-1][1] / anchor)


def main():
    eth_df = load_prices("ETH")
    btc_df = load_prices("BTC")

    test_start = ts("2024-01-01")
    test_end   = ts("2026-04-15")

    btc_map = {int(r["timestamp"]): float(r["close"]) for _, r in btc_df.iterrows()}

    print("Loading ETH hourly events (all eval points, no EV filter)...")
    events = list(generate_hourly_events(eth_df, "ETH", start_ts=test_start, end_ts=test_end, seed=42))
    print(f"Total eval events: {len(events):,}")

    # For each event, compute key observables and actual outcome
    records = []
    for ev in events:
        # Outcome: did close exceed strike?
        won_yes = int(ev.close_price > ev.strike)

        # Current distance from strike (%)
        pct_above = (ev.current_price - ev.strike) / ev.strike * 100.0

        # Elapsed minutes
        elapsed_min = ev.elapsed_seconds / 60.0

        # BTC history at eval time
        hist_start = ev.eval_ts - 3600
        btc_history = [
            (float(t), btc_map[t])
            for t in range(int(hist_start // 60 * 60), int(ev.eval_ts // 60 * 60) + 1, 60)
            if t in btc_map
        ]

        btc_15m = _lead_return(btc_history, 900)
        btc_5m  = _lead_return(btc_history, 300)

        # ETH 5-min return (repricing gap)
        eth_history = [(float(t), p) for t, p in ev.price_history]
        eth_5m = _lead_return(eth_history, 300)

        # ETH/BTC repricing gap: how much ETH is lagging BTC's 5-min move
        repricing_gap = None
        if eth_5m is not None and btc_5m is not None:
            beta = 1.10  # default ETH/BTC beta
            expected_eth_5m = btc_5m * beta
            repricing_gap = expected_eth_5m - eth_5m

        # BB probability (from orderbook which uses BB internally)
        baseline_p = ev.orderbook.yes_ask / (ev.orderbook.yes_ask + ev.orderbook.no_ask)

        records.append({
            "won_yes": won_yes,
            "pct_above": pct_above,
            "elapsed_min": elapsed_min,
            "seconds_left": ev.seconds_left,
            "btc_15m": btc_15m,
            "btc_5m": btc_5m,
            "eth_5m": eth_5m,
            "repricing_gap": repricing_gap,
            "baseline_p": baseline_p,
            "rv": ev.realized_vol_1min or 0.0,
        })

    print(f"Processed {len(records):,} records\n")

    def wr_stats(subset, direction="yes"):
        n = len(subset)
        if n == 0: return n, 0.0
        wins = sum(1 for r in subset if r["won_yes"] == (1 if direction == "yes" else 0))
        return n, wins / n * 100

    # ---- Baseline probability slices ------------------------------------
    print("WR by baseline_p (BB probability, direction = bet YES when p>0.5, NO when p<0.5):")
    for lo, hi in [(0.45, 0.55), (0.55, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 0.95), (0.95, 1.0)]:
        sub = [r for r in records if lo <= r["baseline_p"] < hi]
        n, wr = wr_stats(sub, "yes")
        print(f"  p={lo:.2f}-{hi:.2f}:  N={n:>6}  WR(YES)={wr:>5.1f}%")

    # ---- BTC 15m magnitude → YES WR (when BTC>0, bet YES; when BTC<0, bet NO) ----
    print("\nDirectional WR by |BTC 15m|:")
    for lo, hi in [(0.0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.5), (0.5, 0.75), (0.75, 1.0), (1.0, 5.0)]:
        sub_yes = [r for r in records
                   if r["btc_15m"] is not None
                   and lo <= abs(r["btc_15m"]) * 100 < hi
                   and r["btc_15m"] > 0]  # BTC up → bet YES
        sub_no  = [r for r in records
                   if r["btc_15m"] is not None
                   and lo <= abs(r["btc_15m"]) * 100 < hi
                   and r["btc_15m"] < 0]  # BTC down → bet NO (YES WR = 1-no_wr)
        ny, wr_y = wr_stats(sub_yes, "yes")
        nn, wr_n = wr_stats(sub_no, "no")
        print(f"  |btc|={lo:.1f}-{hi:.1f}%:  YES({ny:>5}) WR={wr_y:>5.1f}%  NO({nn:>5}) WR={wr_n:>5.1f}%")

    # ---- Repricing gap signal ---------------------------------------------
    print("\nDirectional WR by repricing gap (ETH underpriced vs BTC move):")
    for lo, hi in [(-5.0, -0.005), (-0.005, -0.002), (-0.002, 0.0), (0.0, 0.002), (0.002, 0.005), (0.005, 5.0)]:
        sub = [r for r in records
               if r["repricing_gap"] is not None
               and lo <= r["repricing_gap"] < hi]
        # If repricing gap > 0: ETH lagging BTC up → bet YES
        # If repricing gap < 0: ETH lagging BTC down → bet NO
        if hi <= 0:
            n, wr = wr_stats(sub, "no")
            dir_label = "NO"
        else:
            n, wr = wr_stats(sub, "yes")
            dir_label = "YES"
        print(f"  gap={lo:+.3f} to {hi:+.3f}:  {dir_label}({n:>6}) WR={wr:>5.1f}%")

    # ---- Grid search: best conditions for WR >= 90% ----------------------
    print("\n=== RAW EVENT GRID SEARCH: CONDITIONS WITH DIRECTIONAL WR >= 90% (min N=50) ===")

    thresholds = {
        "btc15_pct": [0.3, 0.5, 0.75, 1.0, 1.5],
        "dist_pct":  [0.3, 0.5, 0.75, 1.0, 1.5, 2.0],
        "elapsed_min": [0, 10, 20, 30, 40, 45, 50],
        "baseline_p": [0.5, 0.6, 0.7, 0.75, 0.8, 0.85],
    }

    hits = []
    for bt in thresholds["btc15_pct"]:
        for dist in thresholds["dist_pct"]:
            for el in thresholds["elapsed_min"]:
                for bp in thresholds["baseline_p"]:
                    # BTC UP + price above strike + late in window + high baseline
                    sub_yes = [
                        r for r in records
                        if r["btc_15m"] is not None
                        and r["btc_15m"] * 100 >= bt
                        and r["pct_above"] >= dist
                        and r["elapsed_min"] >= el
                        and r["baseline_p"] >= bp
                    ]
                    if len(sub_yes) >= 50:
                        n, wr = wr_stats(sub_yes, "yes")
                        if wr >= 85:
                            hits.append((wr, n, f"btc15>+{bt:.1f}%,dist>+{dist:.1f}%,t>={el}m,p>={bp:.2f}", "YES"))

                    # BTC DOWN + price below strike + late in window + low baseline
                    sub_no = [
                        r for r in records
                        if r["btc_15m"] is not None
                        and r["btc_15m"] * 100 <= -bt
                        and r["pct_above"] <= -dist
                        and r["elapsed_min"] >= el
                        and r["baseline_p"] <= (1.0 - bp)
                    ]
                    if len(sub_no) >= 50:
                        n, wr = wr_stats(sub_no, "no")
                        if wr >= 85:
                            hits.append((wr, n, f"btc15<-{bt:.1f}%,dist<-{dist:.1f}%,t>={el}m,p<={1-bp:.2f}", "NO"))

    hits.sort(reverse=True)
    if hits:
        print(f"  {'WR%':>6}  {'N':>6}  Conditions")
        print(f"  {'-'*70}")
        for wr, n, cond, side in hits[:30]:
            print(f"  {wr:>5.1f}%  {n:>6}  {side}: {cond}")
    else:
        print("  No combinations found with WR >= 85% and N >= 50.")

    # ---- Repricing gap combined with other filters -----------------------
    print("\n=== REPRICING GAP COMBINATIONS (min N=50) ===")
    gap_hits = []
    for gap_th in [0.001, 0.002, 0.003, 0.005, 0.0075, 0.01]:
        for bt in [0.1, 0.2, 0.3, 0.5]:
            for dist in [0.0, 0.2, 0.3, 0.5]:
                for el in [0, 10, 20, 30]:
                    # BTC up, ETH lagging (positive repricing gap), price above strike → bet YES
                    sub = [
                        r for r in records
                        if r["repricing_gap"] is not None
                        and r["repricing_gap"] >= gap_th
                        and r["btc_15m"] is not None and r["btc_15m"] * 100 >= bt
                        and r["pct_above"] >= dist
                        and r["elapsed_min"] >= el
                    ]
                    if len(sub) >= 50:
                        n, wr = wr_stats(sub, "yes")
                        if wr >= 80:
                            gap_hits.append((wr, n, f"gap>={gap_th:.3f},btc>{bt:.1f}%,dist>{dist:.1f}%,t>={el}"))

    gap_hits.sort(reverse=True)
    if gap_hits:
        print(f"  {'WR%':>6}  {'N':>6}  Conditions")
        print(f"  {'-'*70}")
        for wr, n, cond in gap_hits[:20]:
            print(f"  {wr:>5.1f}%  {n:>6}  YES: {cond}")
    else:
        print("  No combinations found with WR >= 80% and N >= 50.")


if __name__ == "__main__":
    main()
