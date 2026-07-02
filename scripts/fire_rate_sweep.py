#!/usr/bin/env python3
"""Compute distance distribution and find min_dist threshold for ~30% fire rate."""
import pandas as pd
import numpy as np

ASSETS = {
    "BTC":  {"min_dist_s1": 0.00125, "min_dist_s2": 0.00175},
    "ETH":  {"min_dist_s1": 0.0025,  "min_dist_s2": 0.0020},
    "SOL":  {"min_dist_s1": 0.0025,  "min_dist_s2": 0.0020},
    "XRP":  {"min_dist_s1": 0.0024,  "min_dist_s2": 0.0025},
    "DOGE": {"min_dist_s1": 0.0018,  "min_dist_s2": 0.0015},
}

TARGET = 0.30  # desired fire rate

for asset, cfg in ASSETS.items():
    df = pd.read_csv(
        f"data/{asset}_1m.csv",
        usecols=["open_time", "open", "close"],
        parse_dates=["open_time"],
    )
    df.sort_values("open_time", inplace=True)
    df.set_index("open_time", inplace=True)

    # Simulate 15-min windows; strike = first bar open; check bars 3-12 min in
    # (bot only acts when 3-12 min remain, meaning 3-12 min have elapsed)
    dists = []
    for _, grp in df.resample("15min"):
        if len(grp) < 5:
            continue
        strike = float(grp.iloc[0]["open"])
        if strike <= 0:
            continue
        check = grp.iloc[3:13]
        if len(check) == 0:
            continue
        max_dist = float((abs(check["close"] - strike) / strike).max())
        dists.append(max_dist)

    dists = np.array(dists)
    thresh_30 = float(np.percentile(dists, 100 * (1 - TARGET)))  # 70th pct

    fr_s1 = float((dists > cfg["min_dist_s1"]).mean())
    fr_s2 = float((dists > cfg["min_dist_s2"]).mean())

    print(
        f"{asset}: "
        f"S1={cfg['min_dist_s1']:.5f} ({fr_s1:.1%}) | "
        f"S2={cfg['min_dist_s2']:.5f} ({fr_s2:.1%}) || "
        f"30%-thresh={thresh_30:.5f}"
    )
