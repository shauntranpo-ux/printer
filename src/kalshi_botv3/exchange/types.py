from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Trade:
    ts: datetime  # UTC
    price: float
    size: float
    side: str  # "buy" | "sell"


@dataclass(frozen=True, slots=True)
class Quote:
    ts: datetime  # UTC
    bid: float
    ask: float
    bid_size: float
    ask_size: float


@dataclass(frozen=True, slots=True)
class OHLCV:
    ts: datetime  # UTC, start of bar
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class OrderbookSnapshot:
    ts: datetime  # UTC
    bids: tuple[tuple[float, float], ...]  # (price, size) descending by price
    asks: tuple[tuple[float, float], ...]  # (price, size) ascending by price
