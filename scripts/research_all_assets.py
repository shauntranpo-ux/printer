"""
Research script: late-window persistence bias across ALL supported assets.
Same logic as backtest_late_window — t>=40min, |dist|>=threshold, take first per window.
Reports WR, PnL, and optimal thresholds for each asset.
"""
from __future__ import annotations
import sys, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from collections import defaultdict
from datetime import datetime, timezone
# hourly_window_generator removed (dead code)
import pandas as pd

STAKE    = 25.0
FEE_RATE = 0.07

ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE"]

PERIODS = [
    ("TRAIN 2022-2023", "2022-01-01", "2023-12-31"),
    ("TEST  2024-2026", "2024-01-01", "2026-04-15"),
]

FILTERS = [
    # (elapsed_min, dist_pct, min_entry_c, label)
    (40, 0.3, 35, "t>=40m dist>=0.3%"),
    (40, 0.5, 35, "t>=40m dist>=0.5%"),
    (40, 0.3, 85, "t>=40m dist>=0.3% entry>=85c"),
    (40, 0.5, 85, "t>=40m dist>=0.5% entry>=85c"),
    (45, 0.3, 85, "t>=45m dist>=0.3% entry>=85c"),
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


def analyze(events, elapsed_min, dist_pct, min_entry_c):
    windows = defaultdict(list)
    for ev in events:
        windows[(ev.asset, ev.window_start_ts)].append(ev)

    trades = []
    for key, evs in windows.items():
        evs_sorted = sorted(evs, key=lambda e: e.eval_ts)
        for ev in evs_sorted:
            if ev.elapsed_seconds < elapsed_min * 60:
                continue
            pct = (ev.current_price - ev.strike) / ev.strike * 100.0
            if pct >= dist_pct:
                side = "YES"
                entry_c = ev.orderbook.yes_ask
            elif pct <= -dist_pct:
                side = "NO"
                entry_c = ev.orderbook.no_ask
            else:
                continue
            if entry_c < min_entry_c:
                continue
            won_yes = ev.close_price > ev.strike
            won = (side == "YES" and won_yes) or (side == "NO" and not won_yes)
            entry_frac = entry_c / 100.0
            contracts = STAKE / entry_frac
            if won:
                gross = contracts * (1.0 - entry_frac)
                pnl = gross * (1 - FEE_RATE)
            else:
                pnl = -STAKE
            trades.append({"won": won, "pnl": pnl, "entry_c": entry_c, "side": side,
                           "pct": pct, "elapsed": ev.elapsed_seconds / 60})
            break

    n = len(trades)
    if n < 10:
        return None
    wins = sum(1 for t in trades if t["won"])
    wr = wins / n * 100
    total_pnl = sum(t["pnl"] for t in trades)
    n_windows = len(windows)
    return {"n": n, "wr": wr, "pnl": total_pnl, "n_windows": n_windows}


print("Loading price data...")
dfs = {}
for asset in ASSETS:
    try:
        dfs[asset] = load_prices(asset)
        print(f"  {asset}: {len(dfs[asset]):,} rows")
    except FileNotFoundError:
        print(f"  {asset}: NOT FOUND")

print("\n" + "=" * 110)
print(f"  LATE-WINDOW PERSISTENCE BIAS — ALL ASSETS")
print("=" * 110)

for period_label, start_str, end_str in PERIODS:
    start, end = ts(start_str), ts(end_str)
    days = (end - start) / 86400.0
    print(f"\n{'─'*110}")
    print(f"  PERIOD: {period_label}  ({days:.0f} days)")
    print(f"{'─'*110}")
    print(f"  {'Asset':<8}  {'Filter':>35}  {'N':>6}  {'N_win':>5}  {'WR%':>7}  {'PnL@$25':>10}  {'Ann$/yr':>10}")
    print(f"  {'-'*100}")

    for asset in ASSETS:
        if asset not in dfs:
            continue
        print(f"  Loading {asset} events for {period_label}...", end="\r")
        try:
            events = list(generate_hourly_events(dfs[asset], asset, start, end, seed=42))
        except Exception as e:
            print(f"  {asset}: ERROR {e}")
            continue

        for el_min, dist_pct, min_entry, flabel in FILTERS:
            res = analyze(events, el_min, dist_pct, min_entry)
            if res is None:
                print(f"  {asset:<8}  {flabel:>35}  {'<10 trades':>6}")
                continue
            ann = res["pnl"] / days * 365
            n_win = int(res["n"] * res["wr"] / 100)
            print(f"  {asset:<8}  {flabel:>35}  {res['n']:>6}  {n_win:>5}  {res['wr']:>6.1f}%  ${res['pnl']:>+9,.0f}  ${ann:>+9,.0f}/yr")

print("\n")
