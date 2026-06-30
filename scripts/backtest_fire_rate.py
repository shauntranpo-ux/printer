#!/usr/bin/env python3
"""Backtest S1 fire rate (dist + rv + EMA + reversal gates) against real 1m data.
Sweeps ema_long to find parameter that hits target fire rate.
Usage: py scripts/backtest_fire_rate.py
"""
import pandas as pd
import numpy as np

# Current post-tune config
ASSETS = {
    "BTC":  {"min_dist": 0.00125, "max_rv": 0.0250, "ema_short": 3},
    "ETH":  {"min_dist": 0.0025,  "max_rv": 0.0360, "ema_short": 3},
    "SOL":  {"min_dist": 0.0025,  "max_rv": 0.0600, "ema_short": 3},
    "XRP":  {"min_dist": 0.0024,  "max_rv": 0.0500, "ema_short": 3},
    "DOGE": {"min_dist": 0.0018,  "max_rv": 0.0900, "ema_short": 2},
}
EMA_LONG_SWEEP = [3, 4, 5, 6, 7, 8, 10, 12, 15]
TIME_MIN, TIME_MAX = 3, 12
DAILY_HOURS = 24  # crypto runs 24/7
# Trades/day targets per asset
TARGETS = {"BTC": (20, 30), "ETH": (20, 30), "SOL": (8, 12), "XRP": (8, 12), "DOGE": (8, 12)}


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

    tlo, thi = TARGETS[asset]
    fr_lo = tlo / (DAILY_HOURS * 4)
    fr_hi = thi / (DAILY_HOURS * 4)

    print(f"{asset} (min_dist={cfg['min_dist']}, max_rv={cfg['max_rv']}, target={tlo}-{thi}/day):")
    print(f"  {'ema_long':>8}  {'fire_rate':>10}  {'est_trades/day':>15}")
    best_el, best_dist = None, float('inf')
    for el in EMA_LONG_SWEEP:
        if el <= cfg['ema_short']:
            continue
        fr = fire_rate(df, cfg["min_dist"], cfg["max_rv"], cfg["ema_short"], el)
        daily = DAILY_HOURS * 4 * fr
        mid = (tlo + thi) / 2
        marker = " OK" if tlo <= daily <= thi else ""
        print(f"  {el:>8}  {fr:>9.1%}  {daily:>14.1f}{marker}", flush=True)
        if abs(daily - mid) < best_dist:
            best_dist = abs(daily - mid)
            best_el = el
    print(f"  -> recommend ema_long={best_el}")
