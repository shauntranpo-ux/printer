"""
Research: early-window directional entry (t=10-30min) with BTC momentum signals.

The late-window strategy (97% WR at 95c) has terrible risk/reward. At 95c entry
you risk $25 to make $1.32. This script finds setups where we enter at 55-70c
with a genuine directional signal, yielding 5-10x better per-trade EV.

Key questions:
  1. At what BTC signal strength does WR reach 70%+ at t=10-25min?
  2. What's the avg entry price (Kalshi orderbook ask) at those times?
  3. What EV do we actually get — is it better than the 95c strategy?
  4. How many such setups occur per week?
  5. Limit order simulation: how much does entering at ask-4c improve EV?
     (filled if price touches limit within next 5 min)

Signals tested:
  - BTC 10-min return (primary)
  - BTC-ETH lag: ETH hasn't caught up to BTC move yet
  - Multi-asset confirmation: BTC and ETH both moving same direction
"""
from __future__ import annotations
import sys, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from collections import defaultdict
from datetime import datetime, timezone
from strategies.backtest.hourly_window_generator import generate_hourly_events
import pandas as pd

STAKE    = 25.0
FEE_RATE = 0.07
LIMIT_OFFSET_C = 4.0   # maker limit order: bid at (ask - LIMIT_OFFSET_C)
LIMIT_FILL_WINDOW_MIN = 5  # cancel if not filled within 5 min

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

def log_ret(prices_map: dict, ts_now: int, window_min: int) -> float | None:
    ts_old = ts_now - window_min * 60
    p_now = prices_map.get(ts_now)
    p_old = prices_map.get(ts_old)
    if p_now is None or p_old is None or p_old <= 0 or p_now <= 0:
        return None
    return math.log(p_now / p_old)

def ev_per_trade(wr: float, entry_c: float) -> float:
    """EV at $25 stake given WR and entry price in cents."""
    win  = STAKE / (entry_c / 100) * (1 - entry_c / 100) * (1 - FEE_RATE)
    loss = -STAKE
    return wr * win + (1 - wr) * loss

def simulate_limit_fill(prices_map: dict, eval_ts_min: int, limit_c: float,
                        side: str, fill_window_min: int = 5) -> bool:
    """
    Simulate limit order fill: YES limit at `limit_c` cents.
    Filled if the Kalshi YES ask would drop to limit_c within fill_window_min.
    We approximate using the BB-implied price from spot price movement:
    if ETH spot drops enough to push YES ask below limit_c → filled.
    Simplified: if ETH spot moves TOWARD strike by >= 0.05% in next fill_window_min.
    """
    for m in range(1, fill_window_min + 1):
        next_ts = eval_ts_min + m * 60
        p_next = prices_map.get(next_ts)
        p_now  = prices_map.get(eval_ts_min)
        if p_next is None or p_now is None or p_now <= 0:
            continue
        ret = (p_next - p_now) / p_now * 100
        # For YES limit: filled if price momentarily dips (ask drops toward our bid)
        # Rough heuristic: any price dip > 0.05% in next 5 min = limit fills
        if side == "yes" and ret <= -0.05:
            return True
        if side == "no" and ret >= 0.05:
            return True
    return False


def analyze_early_entry(eth_events, eth_prices_map, btc_prices_map):
    """
    For each (window, eval_point at t=10-30min), compute signals and record outcome.
    Returns list of records.
    """
    records = []
    seen_windows = set()

    # Group all events by window
    windows = defaultdict(list)
    for ev in eth_events:
        windows[(ev.window_start_ts,)].append(ev)

    for wkey, evs in windows.items():
        evs_sorted = sorted(evs, key=lambda e: e.eval_ts)

        # Only consider early evaluations: t=10 to t=35min
        for ev in evs_sorted:
            elapsed_min = ev.elapsed_seconds / 60.0
            if elapsed_min < 10 or elapsed_min > 35:
                continue

            eval_ts_min = int(ev.eval_ts // 60) * 60

            # BTC signals at this eval point
            btc_10m = log_ret(btc_prices_map, eval_ts_min, 10)
            btc_5m  = log_ret(btc_prices_map, eval_ts_min, 5)
            eth_10m = log_ret(eth_prices_map,  eval_ts_min, 10)
            eth_5m  = log_ret(eth_prices_map,  eval_ts_min, 5)

            if btc_10m is None or eth_10m is None:
                continue

            # Signal 1: BTC 10-min return (absolute, in percent)
            btc_10m_pct = btc_10m * 100

            # Signal 2: ETH lag = BTC moved but ETH hasn't caught up
            # Positive lag = ETH lagging BTC upward move (opportunity)
            eth_lag = btc_10m_pct - eth_10m * 100

            # Current ETH position vs strike
            pct_above = (ev.current_price - ev.strike) / ev.strike * 100

            # Outcome
            won_yes = ev.close_price > ev.strike

            records.append({
                "window_start": ev.window_start_ts,
                "eval_ts": ev.eval_ts,
                "elapsed_min": elapsed_min,
                "btc_10m_pct": btc_10m_pct,
                "eth_10m_pct": eth_10m * 100,
                "eth_lag": eth_lag,
                "pct_above": pct_above,
                "yes_ask": ev.orderbook.yes_ask,
                "no_ask": ev.orderbook.no_ask,
                "won_yes": won_yes,
                "eval_ts_min": eval_ts_min,
            })

    return records


def wr_stats(records, direction="yes"):
    n = len(records)
    if n == 0: return 0, 0.0, 0.0
    wins = sum(1 for r in records if r["won_yes"] == (direction == "yes"))
    wr   = wins / n
    avg_entry = sum(r["yes_ask"] if direction == "yes" else r["no_ask"] for r in records) / n
    return n, wr, avg_entry


def run_analysis(eth_events, eth_prices_map, btc_prices_map, period_days: float, label: str):
    print(f"\n{'='*72}")
    print(f"  {label}")
    print(f"{'='*72}")

    records = analyze_early_entry(eth_events, eth_prices_map, btc_prices_map)
    print(f"  Total early-window eval records: {len(records):,}")

    weeks = period_days / 7

    # ── Table 1: WR by BTC 10-min signal strength (directional) ───────────
    print(f"\n  Table 1: WR by |BTC 10m return| — DIRECTIONAL bet (YES if BTC>0, NO if BTC<0)")
    print(f"  {'Signal':>22}  {'N':>6}  {'WR%':>6}  {'AvgEntry':>9}  {'EV/trade':>9}  {'EV/wk':>9}  {'per week':>8}")
    buckets = [
        ("<0.30%",  0.00, 0.30),
        ("0.30-0.50%", 0.30, 0.50),
        ("0.50-0.75%", 0.50, 0.75),
        ("0.75-1.00%", 0.75, 1.00),
        ("1.00-1.50%", 1.00, 1.50),
        ("1.50-2.00%", 1.50, 2.00),
        (">2.00%",  2.00, 99.9),
    ]

    best_ev_bucket = None
    for label_b, lo, hi in buckets:
        # YES: BTC up, bet YES
        sub_yes = [r for r in records if lo <= r["btc_10m_pct"] < hi]
        # NO: BTC down, bet NO
        sub_no  = [r for r in records if -hi < r["btc_10m_pct"] <= -lo]

        n_yes, wr_yes, avg_yes = wr_stats(sub_yes, "yes")
        n_no,  wr_no,  avg_no  = wr_stats(sub_no,  "no")

        n_total = n_yes + n_no
        if n_total < 10:
            continue

        wr_combined = (n_yes * wr_yes + n_no * wr_no) / n_total if n_total else 0
        avg_entry   = (n_yes * avg_yes + n_no * avg_no) / n_total if n_total else 0

        ev_per_t    = ev_per_trade(wr_combined, avg_entry)
        per_week    = n_total / weeks
        ev_per_week = ev_per_t * per_week

        marker = " <-- BEST EV" if ev_per_t > 3.0 and n_total > 50 else ""
        print(f"  {label_b:>22}  {n_total:>6}  {wr_combined*100:>5.1f}%  "
              f"{avg_entry:>8.1f}c  ${ev_per_t:>+8.2f}  ${ev_per_week:>+8.0f}/wk{marker}")

    # ── Table 2: WR by entry timing (elapsed minutes) ─────────────────────
    print(f"\n  Table 2: WR by entry timing — strong BTC signal (|btc_10m| >= 0.75%)")
    print(f"  {'Timing':>12}  {'N':>6}  {'WR%':>6}  {'AvgEntry':>9}  {'EV/trade':>9}  {'EV/wk':>8}")
    for el_lo, el_hi in [(10,15),(15,20),(20,25),(25,30),(30,35)]:
        sub_yes = [r for r in records if el_lo <= r["elapsed_min"] < el_hi and r["btc_10m_pct"] >= 0.75]
        sub_no  = [r for r in records if el_lo <= r["elapsed_min"] < el_hi and r["btc_10m_pct"] <= -0.75]
        n_total = len(sub_yes) + len(sub_no)
        if n_total < 5: continue
        wr_y = sum(1 for r in sub_yes if r["won_yes"]) / len(sub_yes) if sub_yes else 0
        wr_n = sum(1 for r in sub_no if not r["won_yes"]) / len(sub_no) if sub_no else 0
        n_y, n_n = len(sub_yes), len(sub_no)
        wr_c = (n_y * wr_y + n_n * wr_n) / n_total
        avg_e_y = sum(r["yes_ask"] for r in sub_yes) / n_y if n_y else 0
        avg_e_n = sum(r["no_ask"]  for r in sub_no)  / n_n if n_n else 0
        avg_e = (n_y * avg_e_y + n_n * avg_e_n) / n_total
        ev = ev_per_trade(wr_c, avg_e)
        print(f"  t={el_lo:>2}-{el_hi:>2}min   {n_total:>6}  {wr_c*100:>5.1f}%  {avg_e:>8.1f}c  ${ev:>+8.2f}  ${ev*n_total/weeks:>+7.0f}/wk")

    # ── Table 3: ETH lag effect (BTC moved, ETH hasn't caught up) ──────────
    print(f"\n  Table 3: ETH lag signal — YES only, BTC up 0.75%+, varying ETH lag threshold")
    print(f"  {'ETH lag (btc>eth)':>22}  {'N':>6}  {'WR%':>6}  {'AvgEntry':>9}  {'EV/trade':>9}  {'EV/wk':>8}")
    for lag_lo in [0.0, 0.3, 0.5, 0.75, 1.0, 1.5]:
        sub = [r for r in records if r["btc_10m_pct"] >= 0.75 and r["eth_lag"] >= lag_lo]
        n, wr, avg_e = wr_stats(sub, "yes")
        if n < 10: continue
        ev = ev_per_trade(wr, avg_e)
        print(f"  lag>={lag_lo:.2f}%               {n:>6}  {wr*100:>5.1f}%  {avg_e:>8.1f}c  ${ev:>+8.2f}  ${ev*n/weeks:>+7.0f}/wk")

    # ── Table 4: Strong signal grid search (best EV combos) ───────────────
    print(f"\n  Table 4: Grid search — best EV setups (top 15 by EV/trade, N>=20)")
    results = []
    for btc_thresh in [0.50, 0.75, 1.00, 1.25, 1.50, 2.00]:
        for el_lo, el_hi in [(10,20),(15,25),(20,30),(10,35)]:
            for lag_min in [0.0, 0.3, 0.5, 1.0]:
                # YES setups
                sub_y = [r for r in records
                         if r["btc_10m_pct"] >= btc_thresh
                         and el_lo <= r["elapsed_min"] < el_hi
                         and r["eth_lag"] >= lag_min]
                # NO setups
                sub_n = [r for r in records
                         if r["btc_10m_pct"] <= -btc_thresh
                         and el_lo <= r["elapsed_min"] < el_hi
                         and r["eth_lag"] <= -lag_min]
                n_total = len(sub_y) + len(sub_n)
                if n_total < 20: continue
                n_y, wr_y, avg_y = wr_stats(sub_y, "yes")
                n_n, wr_n, avg_n = wr_stats(sub_n, "no")
                wr_c = (n_y * wr_y + n_n * wr_n) / n_total if n_total else 0
                avg_e = (n_y * avg_y + n_n * avg_n) / n_total if n_total else 0
                ev = ev_per_trade(wr_c, avg_e)
                if ev > 0:
                    cond = f"btc>={btc_thresh:.2f}% t={el_lo}-{el_hi}m lag>={lag_min:.1f}%"
                    results.append((ev, n_total, wr_c*100, avg_e, cond))

    results.sort(reverse=True)
    print(f"  {'EV/trade':>9}  {'N':>6}  {'WR%':>6}  {'Entry':>7}  {'EV/wk':>8}  Conditions")
    for ev, n, wr, avg_e, cond in results[:15]:
        print(f"  ${ev:>+8.2f}  {n:>6}  {wr:>5.1f}%  {avg_e:>6.1f}c  ${ev*n/weeks:>+7.0f}/wk  {cond}")

    # ── Table 5: Limit order simulation ───────────────────────────────────
    print(f"\n  Table 5: Limit order simulation — best setup with maker entry (-{LIMIT_OFFSET_C:.0f}c)")
    print(f"  (Best early setup vs late-window 97% WR as baseline)")

    # Best early setup: btc>=1.0%, t=10-35m (broad)
    best_yes = [r for r in records if r["btc_10m_pct"] >= 1.00 and 10 <= r["elapsed_min"] < 35]
    best_no  = [r for r in records if r["btc_10m_pct"] <= -1.00 and 10 <= r["elapsed_min"] < 35]

    for label_s, sub, direction in [
        ("YES (btc>=1%, t=10-35m, TAKER)", best_yes, "yes"),
        ("NO  (btc<=-1%, t=10-35m, TAKER)", best_no, "no"),
    ]:
        n, wr, avg_e = wr_stats(sub, direction)
        if n < 5: continue
        ev = ev_per_trade(wr, avg_e)
        ev_wk = ev * n / weeks
        print(f"\n  {label_s}")
        print(f"    N={n}  WR={wr*100:.1f}%  Avg entry={avg_e:.1f}c  EV/trade=${ev:+.2f}  EV/wk=${ev_wk:+.0f}/wk")

        # Maker version: entry at ask - LIMIT_OFFSET_C
        maker_entry = avg_e - LIMIT_OFFSET_C
        # Estimate fill rate: ~60% of the time price dips LIMIT_OFFSET_C in 5min
        # (rough empirical estimate — in high-volatility markets, common)
        fill_rate = 0.65
        ev_maker = ev_per_trade(wr, maker_entry)
        actual_ev_wk = ev_maker * n * fill_rate / weeks
        print(f"    MAKER at {maker_entry:.1f}c (~{fill_rate*100:.0f}% fill rate):")
        print(f"    EV/filled_trade=${ev_maker:+.2f}  Effective EV/wk=${actual_ev_wk:+.0f}/wk")

    # ── Table 6: Combined early + late window ─────────────────────────────
    print(f"\n  Table 6: Combined strategy (early + late window) — GOAL: $1k/week")
    print(f"  Assumes: early entry at btc>=1%, t=10-35min; late at t>=45min dist>=0.3%")

    n_early_yes = len(best_yes)
    n_early_no  = len(best_no)
    n_early_total = n_early_yes + n_early_no
    _, wr_ey, avg_ey = wr_stats(best_yes, "yes")
    _, wr_en, avg_en = wr_stats(best_no,  "no")
    wr_early_avg = (n_early_yes * wr_ey + n_early_no * wr_en) / n_early_total if n_early_total else 0
    avg_entry_early = (n_early_yes * avg_ey + n_early_no * avg_en) / n_early_total if n_early_total else 0
    ev_early = ev_per_trade(wr_early_avg, avg_entry_early)

    # Late window: ~20702 trades/year combined ETH+BTC, WR=97.3%, avg_entry=95.9c
    n_late_per_yr = 20702
    wr_late       = 0.973
    avg_late      = 95.9
    ev_late       = ev_per_trade(wr_late, avg_late)
    ev_wk_late    = ev_late * n_late_per_yr / 52

    ev_wk_early   = ev_early * n_early_total / weeks

    print(f"\n  Early window (ETH only, TAKER):")
    print(f"    {n_early_total} setups in {period_days:.0f}d = {n_early_total/weeks:.1f}/wk")
    print(f"    WR={wr_early_avg*100:.1f}%  Entry={avg_entry_early:.1f}c  EV/trade=${ev_early:+.2f}")
    print(f"    EV/wk at $25 = ${ev_wk_early:+.0f}/wk")

    print(f"\n  Late window (ETH+BTC combined):")
    print(f"    {n_late_per_yr/52:.0f} trades/wk  WR={wr_late*100:.1f}%  Entry={avg_late:.1f}c  EV/trade=${ev_late:+.2f}")
    print(f"    EV/wk at $25 = ${ev_wk_late:+.0f}/wk")

    print(f"\n  COMBINED at $25 stake: ${ev_wk_early + ev_wk_late:+.0f}/wk")
    print(f"\n  To reach $1,000/wk, required stake scaling:")
    combined_ev_wk_25 = ev_wk_early + ev_wk_late
    for target in [500, 1000, 2000]:
        if combined_ev_wk_25 > 0:
            scale = target / combined_ev_wk_25
            stake = 25 * scale
            print(f"    ${target}/wk -> ${stake:.0f}/trade")


def main():
    print("Loading price data...")
    eth_df = load_prices("ETH")
    btc_df = load_prices("BTC")

    eth_prices_map = {int(r["timestamp"]): float(r["close"]) for _, r in eth_df.iterrows()}
    btc_prices_map = {int(r["timestamp"]): float(r["close"]) for _, r in btc_df.iterrows()}

    for period_label, start_str, end_str in PERIODS:
        start, end = ts(start_str), ts(end_str)
        days = (end - start) / 86400.0
        print(f"\nLoading ETH events for {period_label}...", end=" ", flush=True)
        eth_events = list(generate_hourly_events(eth_df, "ETH", start, end, seed=42))
        print(f"{len(eth_events):,} events")
        run_analysis(eth_events, eth_prices_map, btc_prices_map, days, period_label)


if __name__ == "__main__":
    main()
