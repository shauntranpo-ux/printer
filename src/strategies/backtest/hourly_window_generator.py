"""
Generate 60-min binary contract windows from historical 1-min data.

Models Kalshi "Hourly Crypto" markets — e.g. "BTC price at 3pm PDT?"
Windows run on fixed hourly boundaries (:00 of each hour).
Strike is set at window-open price rounded to the nearest increment.
Bot evaluates every 5 minutes, first at t=5min, last at t=55min.
"""

from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Iterator, Optional

import pandas as pd

from strategies.backtest.kalshi_amm import simulate_orderbook, SimulatedOrderbook


WINDOW_LENGTH_MINUTES = 60
EVAL_INTERVAL_SECONDS = 300   # evaluate every 5 min
FIRST_EVAL_SECONDS    = 300   # first decision at t=5min
LAST_EVAL_SECONDS     = 3300  # last decision at t=55min (5 min remain)

# Kalshi hourly markets use finer strike increments than 15-min.
STRIKE_INCREMENTS = {
    "BTC":  100.0,
    "ETH":  10.0,
    "SOL":  1.0,
    "XRP":  0.005,
    "DOGE": 0.001,
}


@dataclass
class HourlyBacktestEvent:
    asset: str
    window_start_ts: float
    window_close_ts: float
    strike: float
    eval_ts: float
    seconds_left: float
    elapsed_seconds: float
    current_price: float
    close_price: float
    orderbook: SimulatedOrderbook
    price_history: list           # [(ts, price)]
    realized_vol_1min: Optional[float]


def round_to_strike(price: float, increment: float) -> float:
    return round(price / increment) * increment


def realized_vol_from_history(history: list) -> Optional[float]:
    if len(history) < 4:
        return None
    prices = [p for _, p in history]
    returns = []
    for i in range(1, len(prices)):
        if prices[i - 1] > 0 and prices[i] > 0:
            returns.append(math.log(prices[i] / prices[i - 1]))
    if len(returns) < 3:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(var) if var > 0 else None


def generate_hourly_events(
    df: pd.DataFrame,
    asset: str,
    start_ts: Optional[float] = None,
    end_ts: Optional[float] = None,
    seed: Optional[int] = None,
) -> Iterator[HourlyBacktestEvent]:
    """
    Yield HourlyBacktestEvents for every (window, eval_point) pair.

    Args:
        df: DataFrame with columns timestamp (unix seconds) and close
        asset: asset name
        start_ts / end_ts: optional filtering
        seed: for reproducible orderbook simulation
    """
    if start_ts is not None:
        df = df[df["timestamp"] >= start_ts]
    if end_ts is not None:
        df = df[df["timestamp"] <= end_ts]
    df = df.sort_values("timestamp").reset_index(drop=True)

    if len(df) < 120:
        return

    strike_incr = STRIKE_INCREMENTS.get(asset, 1.0)

    prices_by_min: dict[int, float] = {}
    for _, row in df.iterrows():
        ts_min = int(row["timestamp"]) // 60 * 60
        prices_by_min[ts_min] = float(row["close"])

    sorted_min_ts = sorted(prices_by_min.keys())
    if not sorted_min_ts:
        return

    first_ts = sorted_min_ts[0]
    last_ts  = sorted_min_ts[-1]

    # Start at the first full hourly boundary after the data begins
    first_window_start = ((first_ts // 3600) + 1) * 3600
    last_window_start  = (last_ts  // 3600) * 3600 - 3600

    for window_start in range(first_window_start, last_window_start, 3600):
        window_close = window_start + WINDOW_LENGTH_MINUTES * 60

        if window_start not in prices_by_min or window_close not in prices_by_min:
            continue

        open_price  = prices_by_min[window_start]
        close_price = prices_by_min[window_close]
        strike      = round_to_strike(open_price, strike_incr)

        for elapsed in range(FIRST_EVAL_SECONDS, LAST_EVAL_SECONDS + 1, EVAL_INTERVAL_SECONDS):
            eval_ts = window_start + elapsed
            eval_ts_min = eval_ts // 60 * 60
            if eval_ts_min not in prices_by_min:
                continue
            current_price = prices_by_min[eval_ts_min]
            seconds_left  = window_close - eval_ts

            hist_start = eval_ts - 3600
            history = [
                (float(ts), prices_by_min[ts])
                for ts in range(int(hist_start // 60 * 60), eval_ts_min + 1, 60)
                if ts in prices_by_min
            ]

            rv = realized_vol_from_history(history)
            if rv is None or rv <= 0:
                continue

            ob = simulate_orderbook(
                current_price, strike, seconds_left, rv, asset,
                seed=seed + int(eval_ts) if seed is not None else None,
            )

            yield HourlyBacktestEvent(
                asset=asset,
                window_start_ts=float(window_start),
                window_close_ts=float(window_close),
                strike=strike,
                eval_ts=float(eval_ts),
                seconds_left=float(seconds_left),
                elapsed_seconds=float(elapsed),
                current_price=current_price,
                close_price=close_price,
                orderbook=ob,
                price_history=history,
                realized_vol_1min=rv,
            )
