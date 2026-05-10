#!/usr/bin/env python3
"""Sweep min_dist for SOL/XRP/DOGE at ema_long=12 to hit 8-12 trades/day target.
Usage: py scripts/tune_alt_dist.py
"""
import pandas as pd
import numpy as np

ASSETS = {
    "SOL":  {"max_rv": 0.0600, "ema_short": 3, "ema_long": 12},
    "XRP":  {"max_rv": 0.0500, "ema_short": 3, "ema_long": 12},
    "DOGE": {"max_rv": 0.0900, "ema_short": 2, "ema_long": 12},
}
MIN_DIST_SWEEP = [0.003, 0.005, 0.007, 0.010, 0.015, 0.020, 0.025, 0.030]
TIME_MIN, TIME_MAX = 3, 12
DAILY_HOURS = 24
TARGET_LO, TARGET_HI = 8, 12


def _ema(vals: list) -> float:
    alpha = 2.0 / (len(vals) + 1)
    v = float(vals[0])
    for x in vals[1:]:
        v = alpha * float(x) + (1 - alpha) * v
    return v


def _rv(prices: list, window: int = 5) -> float:
    if len(prices) < 2:
        return 0.001
    log_rets = [abs(prices[i] / prices[i - 1] - 1) for i in range(1, min(window + 1, len(prices)))]
    return float(np.mean(log_rets)) * np.sqrt(1440)


def fire_rate(df: pd.DataFrame, min_dist: float, max_rv: float,
              ema_short: int, ema_long: int) -> float:
    total = fired = 0
    for _, grp in df.resample("15min"):
        if len(grp) < TIME_MAX + 2:
            continue
        strike = float(grp.iloc[0]["open"])
        if strike <= 0:
            continue
        total += 1
        for elapsed in range(TIME_MIN, TIME_MAX + 1):
            prices = [float(grp.iloc[i]["close"]) for i in range(elapsed + 1)]
            cur = prices[-1]
            if abs(cur - strike) / strike < min_dist:
                continue
            if _rv(prices) > max_rv:
                continue
            if len(prices) < ema_long:
                continue
            s_e = _ema(prices[-ema_short:])
            l_e = _ema(prices[-ema_long:])
            direction = "yes" if s_e > l_e else "no"
            if direction == "yes" and cur < strike:
                continue
            if direction == "no" and cur > strike:
                continue
            fired += 1
            break
    return fired / max(total, 1)


for asset, cfg in ASSETS.items():
    print(f"\nLoading {asset}...", flush=True)
    df = pd.read_csv(
        f"data/{asset}_1m.csv",
        usecols=["open_time", "open", "close"],
        parse_dates=["open_time"],
    )
    df.sort_values("open_time", inplace=True)
    df.set_index("open_time", inplace=True)
    df = df[df.index.year == 2024]

    fr_lo = TARGET_LO / (DAILY_HOURS * 4)
    fr_hi = TARGET_HI / (DAILY_HOURS * 4)
    mid = (TARGET_LO + TARGET_HI) / 2

    print(f"{asset} (max_rv={cfg['max_rv']}, ema_short={cfg['ema_short']}, ema_long={cfg['ema_long']}, target={TARGET_LO}-{TARGET_HI}/day):")
    print(f"  {'min_dist':>10}  {'fire_rate':>10}  {'est_trades/day':>15}")
    best_md, best_dist = None, float('inf')
    for md in MIN_DIST_SWEEP:
        fr = fire_rate(df, md, cfg["max_rv"], cfg["ema_short"], cfg["ema_long"])
        daily = DAILY_HOURS * 4 * fr
        marker = " OK" if TARGET_LO <= daily <= TARGET_HI else ""
        print(f"  {md:>10.3f}  {fr:>9.1%}  {daily:>14.1f}{marker}", flush=True)
        if abs(daily - mid) < best_dist:
            best_dist = abs(daily - mid)
            best_md = md
    print(f"  -> recommend min_dist={best_md}")
