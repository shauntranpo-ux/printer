#!/usr/bin/env python3
"""Probe threshold candidates - find fire rates for ETH, DOGE, SOL, XRP."""
import sys, random
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import backtest as bt
import scripts.backtest_s1s2 as b

rng = random.Random(42)

print("=== DOGE strike math ===")
print("DOGE ~$0.15, increment=$0.001 -> max dist = 0.0005/0.15 =", round(0.0005/0.15,5))
print("v2 min_dist=0.0040 > max possible -> DOGE never trades\n")

assets = ["ETH", "XRP", "DOGE", "SOL"]
wc = {}
for asset in assets:
    w, pl = bt.load_data(asset=asset, start_year=2024, mode="full", verbose=False)
    wc[asset] = (w, pl)
    print(f"{asset}: {len(w)} windows")

print("\n--- ETH S1 min_dist sweep (target 3-8%) ---")
for md in [0.0015, 0.0018, 0.0020, 0.0022, 0.0025, 0.0028]:
    cfg = {**b.S1_V2["ETH"], "min_dist": md}
    r = b.backtest_s1_asset("ETH", cfg, *wc["ETH"], random.Random(42))
    pct = r["trades"] / len(wc["ETH"][0]) * 100
    print(f"  min_dist={md:.4f}  trades={r['trades']:>6}  {pct:5.1f}%  wr={r['win_rate']:.1%}")

print("\n--- XRP S1 min_dist sweep ---")
for md in [0.0020, 0.0024, 0.0028, 0.0032]:
    cfg = {**b.S1_V2["XRP"], "min_dist": md}
    r = b.backtest_s1_asset("XRP", cfg, *wc["XRP"], random.Random(42))
    pct = r["trades"] / len(wc["XRP"][0]) * 100
    print(f"  min_dist={md:.4f}  trades={r['trades']:>6}  {pct:5.1f}%  wr={r['win_rate']:.1%}")

print("\n--- DOGE S1 min_dist sweep (must be < 0.003) ---")
for md in [0.0008, 0.0010, 0.0013, 0.0015, 0.0018, 0.0020]:
    cfg = {**b.S1_V2["DOGE"], "min_dist": md}
    r = b.backtest_s1_asset("DOGE", cfg, *wc["DOGE"], random.Random(42))
    pct = r["trades"] / len(wc["DOGE"][0]) * 100
    print(f"  min_dist={md:.4f}  trades={r['trades']:>6}  {pct:5.1f}%  wr={r['win_rate']:.1%}")

print("\n--- SOL S2 min_vel sweep ---")
for mv in [0.60, 0.40, 0.25, 0.15, 0.10]:
    cfg = {**b.S2_V2["SOL"], "min_vel_delta": mv}
    r = b.backtest_s2_asset("SOL", cfg, *wc["SOL"], random.Random(42))
    pct = r["trades"] / len(wc["SOL"][0]) * 100
    print(f"  min_vel={mv:.2f}   trades={r['trades']:>6}  {pct:5.1f}%  wr={r['win_rate']:.1%}")

print("\n--- DOGE S2 min_dist + min_vel sweep ---")
for md, mv in [(0.0012, 0.30), (0.0015, 0.30), (0.0015, 0.20), (0.0010, 0.20)]:
    cfg = {**b.S2_V2["DOGE"], "min_dist": md, "min_vel_delta": mv}
    r = b.backtest_s2_asset("DOGE", cfg, *wc["DOGE"], random.Random(42))
    pct = r["trades"] / len(wc["DOGE"][0]) * 100
    print(f"  dist={md}  vel={mv}  trades={r['trades']:>6}  {pct:5.1f}%  wr={r['win_rate']:.1%}")
