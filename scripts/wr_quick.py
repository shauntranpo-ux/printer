"""Quick empirical WR search on raw events — no EV filter, no strategy."""
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

def lead_return(history, window_sec):
    if not history: return None
    now_ts = history[-1][0]
    cutoff = now_ts - window_sec
    anchor = next((p for ts, p in history if ts >= cutoff), None)
    if anchor is None or anchor <= 0 or history[-1][1] <= 0: return None
    return math.log(history[-1][1] / anchor)

eth_df = load_prices("ETH")
btc_df = load_prices("BTC")
btc_map = {int(r["timestamp"]): float(r["close"]) for _, r in btc_df.iterrows()}

print("Loading events...")
events = list(generate_hourly_events(eth_df, "ETH", ts("2024-01-01"), ts("2026-04-15"), 42))
print(f"{len(events):,} eval events\n")

records = []
for ev in events:
    won_yes = int(ev.close_price > ev.strike)
    pct_above = (ev.current_price - ev.strike) / ev.strike * 100.0
    elapsed_min = ev.elapsed_seconds / 60.0
    baseline_p = ev.orderbook.yes_ask / (ev.orderbook.yes_ask + ev.orderbook.no_ask)

    hist_start = ev.eval_ts - 3600
    btc_hist = [(float(t), btc_map[t])
                for t in range(int(hist_start // 60 * 60), int(ev.eval_ts // 60 * 60) + 1, 60)
                if t in btc_map]
    btc_15m = lead_return(btc_hist, 900)

    eth_hist = [(float(t), p) for t, p in ev.price_history]
    eth_5m = lead_return(eth_hist, 300)
    btc_5m = lead_return(btc_hist, 300)
    repricing_gap = None
    if eth_5m is not None and btc_5m is not None:
        repricing_gap = (btc_5m * 1.10) - eth_5m  # expected - actual ETH 5m

    records.append({
        "won_yes": won_yes, "pct_above": pct_above, "elapsed_min": elapsed_min,
        "baseline_p": baseline_p, "btc_15m": btc_15m, "repricing_gap": repricing_gap,
    })

def wr(sub, side="yes"):
    n = len(sub)
    if n == 0: return 0, 0.0
    w = sum(1 for r in sub if r["won_yes"] == (1 if side == "yes" else 0))
    return n, w/n*100

print("=== BASELINE PROBABILITY BUCKETS (raw empirical, no filter) ===")
print(f"{'Baseline p':>15}  {'N YES':>8}  {'WR YES':>8}  {'N NO':>8}  {'WR NO':>8}")
for lo, hi in [(0.45,0.55),(0.55,0.65),(0.65,0.75),(0.75,0.80),(0.80,0.85),(0.85,0.90),(0.90,0.95),(0.95,1.0)]:
    sub = [r for r in records if lo <= r["baseline_p"] < hi]
    # YES bets: trade when above (price > strike => bet YES that it stays above)
    sub_yes = [r for r in sub if r["pct_above"] > 0]
    sub_no  = [r for r in sub if r["pct_above"] < 0]
    ny, wy = wr(sub_yes, "yes")
    nn, wn = wr(sub_no, "no")
    print(f"  p={lo:.2f}-{hi:.2f}:  {ny:>7}  {wy:>7.1f}%  {nn:>7}  {wn:>7.1f}%")

print("\n=== LATE WINDOW + DEEP ITM (raw empirical, t>=40min, pct_above>=1%) ===")
print(f"{'Filter':>45}  {'N':>6}  {'WR%':>7}")
for t_thresh in [40, 45, 48, 50]:
    for d_thresh in [0.5, 1.0, 1.5, 2.0]:
        # YES bets: above strike, late window
        sub = [r for r in records
               if r["elapsed_min"] >= t_thresh
               and r["pct_above"] >= d_thresh]
        n, w = wr(sub, "yes")
        if n >= 10:
            label = f"YES: t>={t_thresh}min, dist>+{d_thresh:.1f}%"
            print(f"  {label:>45}  {n:>6}  {w:>6.1f}%")
        # NO bets: below strike, late window
        sub2 = [r for r in records
                if r["elapsed_min"] >= t_thresh
                and r["pct_above"] <= -d_thresh]
        n2, w2 = wr(sub2, "no")
        if n2 >= 10:
            label2 = f"NO:  t>={t_thresh}min, dist<-{d_thresh:.1f}%"
            print(f"  {label2:>45}  {n2:>6}  {w2:>6.1f}%")

print("\n=== REPRICING GAP + LATE WINDOW (raw) ===")
for gap_t in [0.002, 0.003, 0.005, 0.01]:
    for el_t in [0, 20, 30]:
        sub = [r for r in records
               if r["repricing_gap"] is not None
               and r["repricing_gap"] >= gap_t
               and r["elapsed_min"] >= el_t]
        n, w = wr(sub, "yes")
        if n >= 30:
            print(f"  YES gap>={gap_t:.3f} t>={el_t}m: N={n}, WR={w:.1f}%")

print("\n=== BEST LATE-WINDOW COMBINATIONS (empirical ceiling search) ===")
results = []
for el in [40, 43, 45, 48, 50]:
    for dist in [0.3, 0.5, 0.75, 1.0, 1.5, 2.0]:
        for bp in [0.75, 0.80, 0.85, 0.90]:
            for btc_t in [0.0, 0.002, 0.003, 0.005]:
                # YES
                sub = [r for r in records
                       if r["elapsed_min"] >= el
                       and r["pct_above"] >= dist
                       and r["baseline_p"] >= bp
                       and (btc_t == 0 or (r["btc_15m"] is not None and r["btc_15m"] >= btc_t))]
                n, w = wr(sub, "yes")
                if n >= 15:
                    results.append((w, n, f"YES t>={el} dist>+{dist:.1f}% p>={bp:.2f} btc>={btc_t:.3f}"))

results.sort(reverse=True)
print(f"  {'WR%':>6}  {'N':>5}  Conditions")
for w, n, cond in results[:25]:
    print(f"  {w:>5.1f}%  {n:>5}  {cond}")
