"""
Fill model for Kalshi binary options backtesting.

Taker: market order → crosses spread, pays 3% fee.
Maker: limit order → logistic fill probability, adverse selection penalty.
All prices in [0, 1] (NOT cents).
"""
from __future__ import annotations
import math
import numpy as np
import pandas as pd
from typing import Optional

TAKER_FEE_RATE = 0.03
MAKER_FEE_RATE = 0.00


class TakerFillModel:
    """
    Taker fill simulation for Kalshi binary contracts.

    Rules:
    - Latency: configurable ms between signal_timestamp and fill timestamp.
      Use the first available tick at or after signal_timestamp + latency_ms.
      If none, fall back to last tick before signal_timestamp.
    - Slippage: cross spread (YES side → yes_ask, NO side → no_ask).
    - Fee: fee_rate × fill_price.
    """

    def __init__(self, latency_ms: float = 500.0, fee_rate: float = TAKER_FEE_RATE):
        self.latency_ms = latency_ms
        self.fee_rate = fee_rate

    def fill(
        self,
        side: str,
        signal_timestamp: pd.Timestamp,
        kalshi_ticks: list[dict],
    ) -> Optional[dict]:
        """
        Returns:
            {"fill_price": float [0,1], "fee": float, "slippage": float,
             "timestamp_filled": pd.Timestamp}
            or None if kalshi_ticks is empty.

        Each tick dict has: timestamp, yes_bid, yes_ask, no_bid, no_ask
        (all in cents [0–100]).
        """
        if not kalshi_ticks:
            return None

        target_ts = signal_timestamp + pd.Timedelta(milliseconds=self.latency_ms)

        fill_tick = None
        for tick in kalshi_ticks:
            ts = (
                tick["timestamp"]
                if isinstance(tick["timestamp"], pd.Timestamp)
                else pd.Timestamp(tick["timestamp"])
            )
            if ts >= target_ts:
                fill_tick = tick
                break

        if fill_tick is None:
            fill_tick = kalshi_ticks[-1]

        raw_price = fill_tick["yes_ask"] if side == "yes" else fill_tick["no_ask"]
        fill_price = float(np.clip(raw_price / 100.0, 0.01, 0.99))
        fee = self.fee_rate * fill_price

        # Slippage = distance from mid to fill price
        if side == "yes":
            mid = (fill_tick["yes_bid"] + fill_tick["yes_ask"]) / 200.0
        else:
            mid = (fill_tick["no_bid"] + fill_tick["no_ask"]) / 200.0
        slippage = abs(fill_price - mid)

        ts_filled = (
            fill_tick["timestamp"]
            if isinstance(fill_tick["timestamp"], pd.Timestamp)
            else pd.Timestamp(fill_tick["timestamp"])
        )

        return {
            "fill_price": fill_price,
            "fee": fee,
            "slippage": slippage,
            "timestamp_filled": ts_filled,
        }


class MakerFillModel:
    """
    Maker (limit order) fill simulation.

    Fill probability: logistic function of price_improvement / spread.
    Adverse selection: fills are penalized by adverse_selection_fraction × spread.
    Maker fee = 0 on Kalshi.
    """

    def __init__(
        self,
        price_improvement_cents: float = 1.0,
        adverse_selection_fraction: float = 0.4,
        fee_rate: float = MAKER_FEE_RATE,
    ):
        self.price_improvement_cents = price_improvement_cents
        self.adverse_selection_fraction = adverse_selection_fraction
        self.fee_rate = fee_rate

    def fill_probability(self, side: str, tick: dict) -> float:
        """
        Logistic fill probability. Higher price improvement → lower fill prob.
        Returns value in (0, 1).
        """
        spread_cents = tick["yes_ask"] - tick["yes_bid"]
        if spread_cents <= 0:
            return 0.5
        x = self.price_improvement_cents / spread_cents
        return float(1.0 / (1.0 + math.exp(x)))

    def fill(
        self,
        side: str,
        signal_timestamp: pd.Timestamp,
        kalshi_ticks: list[dict],
        rng: Optional[object] = None,
    ) -> Optional[dict]:
        """
        Returns fill dict or None if not filled
        (fill probability check fails or no ticks).
        """
        if not kalshi_ticks:
            return None
        if rng is None:
            rng = np.random.default_rng()

        tick = kalshi_ticks[-1]
        prob = self.fill_probability(side, tick)
        if rng.random() > prob:
            return None

        # Limit price: best bid + price_improvement
        if side == "yes":
            limit_cents = tick["yes_bid"] + self.price_improvement_cents
        else:
            limit_cents = tick["no_bid"] + self.price_improvement_cents

        spread = abs(tick["yes_ask"] - tick["yes_bid"]) / 100.0
        adverse = self.adverse_selection_fraction * spread
        fill_price = float(np.clip(limit_cents / 100.0 + adverse, 0.01, 0.99))
        fee = self.fee_rate * fill_price

        ts_filled = (
            tick["timestamp"]
            if isinstance(tick["timestamp"], pd.Timestamp)
            else pd.Timestamp(tick["timestamp"])
        )

        return {
            "fill_price": fill_price,
            "fee": fee,
            "slippage": adverse,
            "timestamp_filled": ts_filled,
        }
