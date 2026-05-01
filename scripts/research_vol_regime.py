"""
Volatility regime early entry research for ETH hourly Kalshi markets.

Hypothesis: low-volatility, clean-direction windows identified at t=10-25min
give entries at 60-75c with WR 80-88% — far better EV than waiting for
the 80-95c dwell/late entries.

Features tested at t=10, 15, 20, 25min:
  - ETH cross_count    (times price crossed the strike)
  - ETH realized vol   (std of minute log returns, %)
  - ETH candle quality (|net_move| / high_low_range)
  - BTC cross_count    (confirmation — computed via direct array lookup)
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

EVAL_TIMES_SEC = [600, 900, 1200, 1500]   # t=10, 15, 20, 25min


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


def build_ts_index(df: pd.DataFrame):
    """Return (timestamps_array, closes_array) sorted for binary search."""
    ts_arr  = df["timestamp"].values.astype(np.int64)
    cl_arr  = df["close"].values.astype(np.float64)
    idx     = np.argsort(ts_arr)
    return ts_arr[idx], cl_arr[idx]


def window_prices_fast(ts_arr, cl_arr, window_start: float, eval_ts: float):
    """Extract prices in [window_start, eval_ts] using binary search."""
    lo = int(np.searchsorted(ts_arr, int(window_start), side="left"))
    hi = int(np.searchsorted(ts_arr, int(eval_ts),      side="right"))
    return cl_arr[lo:hi]


def vol_features(ts_arr, cl_arr, window_start: float, eval_ts: float,
                 strike: float) -> dict | None:
    prices = window_prices_fast(ts_arr, cl_arr, window_start, eval_ts)
    if len(prices) < 5:
        return None

    above     = prices > strike
    n         = len(above)
    cross_cnt = int(np.sum(np.diff(above.astype(np.int8)) != 0))
    current_itm = bool(above[-1])

    if len(prices) > 1:
        log_rets = np.diff(np.log(prices + 1e-12))
        rvol = float(np.std(log_rets)) * 100.0   # percent per 1-min bar
    else:
        rvol = 0.0

    price_range = float(np.max(prices) - np.min(prices))
    net_move    = float(prices[-1] - prices[0])
    candle_q    = abs(net_move) / price_range if price_range > 1e-10 else 0.0
    candle_dir  = net_move / price_range       if price_range > 1e-10 else 0.0
    if not current_itm:
        candle_dir = -candle_dir   # positive = directional toward current ITM side

    return {
        "cross_cnt":   cross_cnt,
        "current_itm": current_itm,
        "rvol":        rvol,
        "candle_q":    candle_q,
        "candle_dir":  candle_dir,
    }


def pnl_for(won: bool, entry_c: float) -> float:
    frac      = entry_c / 100.0
    contracts = STAKE / frac
    if won:
        return contracts * (1 - frac) * (1 - FEE_RATE)
    return -STAKE


def analyse_period(eth_events: list, eth_ts, eth_cl, btc_ts, btc_cl) -> None:
    # Group ETH events by window_start -> elapsed -> event
    eth_lookup: dict = defaultdict(dict)
    for ev in eth_events:
        bucket = round(ev.elapsed_seconds / 300) * 300
        eth_lookup[ev.window_start_ts][bucket] = ev

    # For BTC strike: estimate as the BTC price at window_start
    # We'll compute it on-the-fly from the BTC ts_arr

    rows = []
    for wts, eth_buckets in eth_lookup.items():
        # BTC strike = BTC price at window start
        btc_at_start = window_prices_fast(btc_ts, btc_cl, wts - 60, wts + 60)
        btc_strike = float(btc_at_start[0]) if len(btc_at_start) > 0 else None

        for target_elapsed in EVAL_TIMES_SEC:
            ev = eth_buckets.get(target_elapsed)
            if ev is None:
                continue

            ef = vol_features(eth_ts, eth_cl, wts, ev.eval_ts, ev.strike)
            if ef is None:
                continue

            side    = "YES" if ef["current_itm"] else "NO"
            entry_c = ev.orderbook.yes_ask if ef["current_itm"] else ev.orderbook.no_ask
            won_yes = ev.close_price > ev.strike
            won     = (side == "YES" and won_yes) or (side == "NO" and not won_yes)

            # BTC cross_count at same elapsed
            btc_cross = None
            if btc_strike is not None:
                bf = vol_features(btc_ts, btc_cl, wts, ev.eval_ts, btc_strike)
                if bf is not None:
                    btc_cross = bf["cross_cnt"]

            rows.append({
                "t":          target_elapsed // 60,
                "entry":      entry_c,
                "won":        won,
                "cross":      ef["cross_cnt"],
                "rvol":       ef["rvol"],
                "candle_q":   ef["candle_q"],
                "candle_dir": ef["candle_dir"],
                "btc_cross":  btc_cross,
                "pnl":        pnl_for(won, entry_c),
            })

    if not rows:
        print("  No rows.")
        return

    df = pd.DataFrame(rows)
    ts_min  = min(e.eval_ts for e in eth_events)
    ts_max  = max(e.eval_ts for e in eth_events)
    weeks   = (ts_max - ts_min) / (86400 * 7)

    def stats(subset, label, indent=4):
        n = len(subset)
        if n < 5:
            return
        wr    = subset["won"].mean()
        avgE  = subset["entry"].mean()
        totP  = subset["pnl"].sum()
        wkPnl = totP / weeks
        evT   = totP / n
        wk_n  = n / weeks
        print(f"{' '*indent}{label:<46}  n={n:>6} ({wk_n:>5.1f}/wk)"
              f"  WR={wr*100:>5.1f}%  E={avgE:>5.1f}c"
              f"  EV=${evT:>+6.2f}  ${wkPnl:>+7.1f}/wk")

    # ── Table 1: cross_count vs eval time (all windows) ────────────────────
    print("\n  TABLE 1: ETH cross_count vs eval time")
    print(f"  {'Label':<46}  {'n':>8}  {'n/wk':>6}  {'WR':>6}  {'E':>5}  {'EV/t':>8}  {'$/wk':>8}")
    print("  " + "-"*90)
    for t_min in [10, 15, 20, 25]:
        for xc in [0, 1, 2, "3+"]:
            if xc == "3+":
                sub = df[(df["t"] == t_min) & (df["cross"] >= 3)]
            else:
                sub = df[(df["t"] == t_min) & (df["cross"] == xc)]
            if len(sub) < 10:
                continue
            stats(sub, f"t={t_min}min  ETH_cross={xc}")

    # ── Table 2: BTC cross=0 confirmation at t=15, 20 ─────────────────────
    print("\n  TABLE 2: BTC confirmation (ETH cross=0, t=15 and t=20)")
    print(f"  {'Label':<46}  {'n':>8}  {'n/wk':>6}  {'WR':>6}  {'E':>5}  {'EV/t':>8}  {'$/wk':>8}")
    print("  " + "-"*90)
    for t_min in [15, 20]:
        base = df[(df["t"] == t_min) & (df["cross"] == 0)]
        stats(base, f"t={t_min}  ETH_cross=0")
        for bc in [0, 1]:
            sub = base[base["btc_cross"] == bc]
            if len(sub) < 5:
                continue
            stats(sub, f"  t={t_min}  ETH_cross=0  BTC_cross={bc}", indent=6)

    # ── Table 3: Realized vol buckets (cross=0, t=15) ─────────────────────
    print("\n  TABLE 3: Realized vol quartiles (t=15, ETH cross=0)")
    print(f"  {'Label':<46}  {'n':>8}  {'n/wk':>6}  {'WR':>6}  {'E':>5}  {'EV/t':>8}  {'$/wk':>8}")
    print("  " + "-"*90)
    sub15 = df[(df["t"] == 15) & (df["cross"] == 0)]
    if len(sub15) >= 20:
        qs     = sub15["rvol"].quantile([0, 0.25, 0.5, 0.75, 1.0]).values
        labels = ["Q1 (lowest)", "Q2", "Q3", "Q4 (highest)"]
        for i in range(4):
            lo, hi = qs[i], qs[i+1]
            cut = sub15[(sub15["rvol"] >= lo) & (sub15["rvol"] <= hi + 1e-9)]
            stats(cut, f"t=15  cross=0  rvol {labels[i]} [{lo:.3f},{hi:.3f}]%")

    # ── Table 4: Candle quality at t=15 and t=20 (cross=0) ────────────────
    print("\n  TABLE 4: Candle quality (cross=0)")
    print(f"  {'Label':<46}  {'n':>8}  {'n/wk':>6}  {'WR':>6}  {'E':>5}  {'EV/t':>8}  {'$/wk':>8}")
    print("  " + "-"*90)
    for t_min in [15, 20]:
        for cq in [0.4, 0.5, 0.6, 0.7, 0.8]:
            sub = df[(df["t"] == t_min) & (df["cross"] == 0) & (df["candle_q"] >= cq)]
            if len(sub) < 10:
                continue
            stats(sub, f"t={t_min}  cross=0  candle_q>={cq:.1f}")

    # ── Table 5: Candle direction (price moved toward ITM side) ───────────
    print("\n  TABLE 5: Directional candle (cross=0, candle moved INTO ITM)")
    print(f"  {'Label':<46}  {'n':>8}  {'n/wk':>6}  {'WR':>6}  {'E':>5}  {'EV/t':>8}  {'$/wk':>8}")
    print("  " + "-"*90)
    for t_min in [10, 15, 20]:
        for cdir in [0.0, 0.3, 0.5, 0.7]:
            sub = df[(df["t"] == t_min) & (df["cross"] == 0) & (df["candle_dir"] >= cdir)]
            if len(sub) < 10:
                continue
            stats(sub, f"t={t_min}  cross=0  candle_dir>={cdir:.1f}")

    # ── Table 6: Summary cross=0 baseline across eval times ───────────────
    print("\n  TABLE 6: Summary — cross=0 baseline")
    print(f"  {'Label':<46}  {'n':>8}  {'n/wk':>6}  {'WR':>6}  {'E':>5}  {'EV/t':>8}  {'$/wk':>8}")
    print("  " + "-"*90)
    for t_min in [10, 15, 20, 25]:
        sub = df[df["t"] == t_min]
        stats(sub, f"t={t_min}min  ALL (baseline)")
    print()
    for t_min in [10, 15, 20, 25]:
        sub = df[(df["t"] == t_min) & (df["cross"] == 0)]
        if len(sub) < 5:
            continue
        stats(sub, f"t={t_min}min  cross=0")


def main():
    print("Loading price data...")
    eth_df = load_prices("ETH")
    btc_df = load_prices("BTC")
    print("Building fast indices...")
    eth_ts, eth_cl = build_ts_index(eth_df)
    btc_ts, btc_cl = build_ts_index(btc_df)

    for label, start_str, end_str in PERIODS:
        start, end = ts_from(start_str), ts_from(end_str)
        print(f"\nGenerating ETH events {label}...", end=" ", flush=True)
        eth_events = list(generate_hourly_events(eth_df, "ETH", start, end, seed=42))
        print(f"{len(eth_events):,}")

        print(f"\n{'='*72}")
        print(f"  {label}")
        print(f"{'='*72}")
        print("  Analyzing...", flush=True)
        analyse_period(eth_events, eth_ts, eth_cl, btc_ts, btc_cl)


if __name__ == "__main__":
    main()
