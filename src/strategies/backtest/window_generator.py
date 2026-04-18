"""
Generate 15-min binary contract windows from historical 1-min data.

For each asset, we simulate Kalshi 15-min windows. Convention:
  - Windows run on fixed boundaries: :00-:15, :15-:30, :30-:45, :45-:00
  - Strike is set at window open (rounded to nearest asset-specific
    strike increment)
  - Bot evaluates at fixed intervals during the window (every 60s
    after the first 30s warmup) and decides whether to trade
  - Window closes 15 min after open; outcome = price(close) vs strike
"""

from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Iterator, Optional

import pandas as pd

from strategies.backtest.kalshi_amm import simulate_orderbook, SimulatedOrderbook


WINDOW_LENGTH_MINUTES = 15
EVAL_INTERVAL_SECONDS = 60
FIRST_EVAL_SECONDS = 60
LAST_EVAL_SECONDS = 60 * 14  # last decision at t=14min (60s remain)

STRIKE_INCREMENTS = {
    "BTC":  1000.0,
    "ETH":  25.0,
    "SOL":  1.0,
    "XRP":  0.01,
    "DOGE": 0.001,
}


@dataclass
class BacktestEvent:
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
    """Std of 1-min log returns, expects history as [(ts, price)]."""
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


def generate_events(
    df: pd.DataFrame,
    asset: str,
    start_ts: Optional[float] = None,
    end_ts: Optional[float] = None,
    seed: Optional[int] = None,
) -> Iterator[BacktestEvent]:
    """
    Yield BacktestEvents for every (window, eval_point) pair in the
    given timeframe.

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

    if len(df) < 60:
        return

    strike_incr = STRIKE_INCREMENTS.get(asset, 1.0)

    prices_by_min = {}
    for _, row in df.iterrows():
        ts_min = int(row["timestamp"]) // 60 * 60
        prices_by_min[ts_min] = float(row["close"])

    sorted_min_ts = sorted(prices_by_min.keys())
    if not sorted_min_ts:
        return

    first_ts = sorted_min_ts[0]
    last_ts = sorted_min_ts[-1]

    first_window_start = ((first_ts // 900) + 1) * 900
    last_window_start = (last_ts // 900) * 900 - 900

    for window_start in range(first_window_start, last_window_start, 900):
        window_close = window_start + WINDOW_LENGTH_MINUTES * 60

        if window_start not in prices_by_min or window_close not in prices_by_min:
            continue

        open_price = prices_by_min[window_start]
        close_price = prices_by_min[window_close]
        strike = round_to_strike(open_price, strike_incr)

        for elapsed in range(FIRST_EVAL_SECONDS, LAST_EVAL_SECONDS + 1, EVAL_INTERVAL_SECONDS):
            eval_ts = window_start + elapsed
            if eval_ts not in prices_by_min:
                continue
            current_price = prices_by_min[eval_ts]
            seconds_left = window_close - eval_ts

            hist_start = eval_ts - 3600
            history = [
                (float(ts), prices_by_min[ts])
                for ts in range(int(hist_start // 60 * 60), eval_ts + 1, 60)
                if ts in prices_by_min
            ]

            rv = realized_vol_from_history(history)
            if rv is None or rv <= 0:
                continue

            ob = simulate_orderbook(
                current_price, strike, seconds_left, rv, asset,
                seed=seed + int(eval_ts) if seed is not None else None,
            )

            yield BacktestEvent(
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
