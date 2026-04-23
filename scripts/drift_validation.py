"""
Validate that the BB underpricing at high baseline_p holds in TRAIN data (2023),
not just test data (2024-2026). If it's real alpha, it must hold out-of-sample.

Also compute the ETH price drift signal empirically.
"""
import sys, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from datetime import datetime, timezone
from strategies.backtest.hourly_window_generator import generate_hourly_events
import pandas as pd


def ts(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def load_prices(asset):
    for name in [f"{asset}_1m_extended.parquet", f"{asset}_1m_2026.parquet"]:
        p = Path(f"data/historical/{name}")
        if p.exists():
            df = pd.read_parquet(p)
            if "open_time" in df.columns and "timestamp" not in df.columns:
                df["timestamp"] = df["open_time"].values.astype("datetime64[s]").astype("int64").astype("float64")
            return df.sort_values("timestamp").reset_index(drop=True)[["timestamp", "close"]]
    raise FileNotFoundError(asset)


def analyze_period(events, label):
    print(f"\n{label}: {len(events):,} eval events")
    records = []
    for ev in events:
        won_yes = int(ev.close_price > ev.strike)
        pct_above = (ev.current_price - ev.strike) / ev.strike * 100.0
        elapsed_min = ev.elapsed_seconds / 60.0
        baseline_p = ev.orderbook.yes_ask / (ev.orderbook.yes_ask + ev.orderbook.no_ask)

        # ETH 60-min drift: log return over last 60 min
        eth_hist = [(float(t), p) for t, p in ev.price_history]
        eth_drift = None
        if len(eth_hist) >= 30:
            p_old = eth_hist[0][1]
            p_now = eth_hist[-1][1]
            if p_old > 0 and p_now > 0:
                eth_drift = math.log(p_now / p_old) / 60.0  # per minute

        records.append({
            "won_yes": won_yes,
            "pct_above": pct_above,
            "elapsed_min": elapsed_min,
            "baseline_p": baseline_p,
            "eth_drift": eth_drift,
        })

    # Baseline bucket WR
    print(f"  Baseline p range  |  N (YES)  |  WR YES  |  BB implied  |  Bias(+=market underprices)")
    for lo, hi in [(0.65,0.70),(0.70,0.75),(0.75,0.80),(0.80,0.85),(0.85,0.90),(0.90,0.95),(0.95,1.0)]:
        sub_yes = [r for r in records if lo <= r["baseline_p"] < hi and r["pct_above"] > 0]
        n = len(sub_yes)
        if n == 0:
            continue
        wr = sum(r["won_yes"] for r in sub_yes) / n * 100
        avg_implied = (lo + hi) / 2 * 100  # avg entry price (yes_ask ≈ mid of range)
        bias = wr - avg_implied
        print(f"  p={lo:.2f}-{hi:.2f}  |  {n:>7}  |  {wr:>6.1f}%  |  {avg_implied:>7.1f}%  |  {bias:>+6.1f}%")

    # Late window (t>=40min) bucket WR
    print(f"\n  t>=40min analysis:")
    for lo, hi in [(0.80,0.85),(0.85,0.90),(0.90,0.95),(0.95,1.0)]:
        sub = [r for r in records if lo <= r["baseline_p"] < hi and r["pct_above"] > 0 and r["elapsed_min"] >= 40]
        n = len(sub)
        if n < 5:
            continue
        wr = sum(r["won_yes"] for r in sub) / n * 100
        avg_implied = (lo + hi) / 2 * 100
        bias = wr - avg_implied
        print(f"    p={lo:.2f}-{hi:.2f} t>=40m  |  N={n:>5}  |  WR={wr:>6.1f}%  |  Implied={avg_implied:.1f}%  |  Bias={bias:>+5.1f}%")

    # Late window (t>=40min) + min dist
    print(f"\n  Best late-window combos:")
    for t_t in [35, 40, 45]:
        for dist in [0.3, 0.5, 0.75, 1.0]:
            sub_y = [r for r in records if r["elapsed_min"] >= t_t and r["pct_above"] >= dist]
            sub_n = [r for r in records if r["elapsed_min"] >= t_t and r["pct_above"] <= -dist]
            for sub, side in [(sub_y, "YES"), (sub_n, "NO")]:
                n = len(sub)
                if n < 10:
                    continue
                if side == "YES":
                    wr = sum(r["won_yes"] for r in sub) / n * 100
                else:
                    wr = sum(1 - r["won_yes"] for r in sub) / n * 100
                print(f"    {side} t>={t_t}m dist>{dist:.1f}%: N={n}, WR={wr:.1f}%")


def main():
    eth_df = load_prices("ETH")

    # TRAINING period: 2022-01-01 to 2023-12-31
    print("Loading TRAINING events (2022-2023)...")
    train_events = list(generate_hourly_events(eth_df, "ETH", ts("2022-01-01"), ts("2023-12-31"), 42))
    analyze_period(train_events, "=== TRAINING 2022-2023 ===")

    # TEST period: 2024-01-01 to 2026-04-15
    print("\n\nLoading TEST events (2024-2026)...")
    test_events  = list(generate_hourly_events(eth_df, "ETH", ts("2024-01-01"), ts("2026-04-15"), 42))
    analyze_period(test_events, "=== TEST 2024-2026 ===")


if __name__ == "__main__":
    main()
