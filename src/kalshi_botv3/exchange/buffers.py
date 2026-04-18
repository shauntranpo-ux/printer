from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime

import pandas as pd

from kalshi_botv3.exchange.types import OHLCV, OrderbookSnapshot, Quote, Trade

_MAXLEN_TRADES = 10_000
_MAXLEN_QUOTES = 3_600
_MAXLEN_OHLCV = 1_440  # 24 h of 1-minute bars


class MarketBuffer:
    """Thread-safe (asyncio) rolling buffer for one market."""

    def __init__(
        self,
        maxlen_trades: int = _MAXLEN_TRADES,
        maxlen_quotes: int = _MAXLEN_QUOTES,
        maxlen_ohlcv: int = _MAXLEN_OHLCV,
    ) -> None:
        self._trades: deque[Trade] = deque(maxlen=maxlen_trades)
        self._quotes: deque[Quote] = deque(maxlen=maxlen_quotes)
        self._ohlcv: deque[OHLCV] = deque(maxlen=maxlen_ohlcv)
        self._orderbook: OrderbookSnapshot | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ writes

    async def add_trade(self, trade: Trade) -> None:
        async with self._lock:
            self._trades.append(trade)

    async def add_quote(self, quote: Quote) -> None:
        async with self._lock:
            self._quotes.append(quote)

    async def update_orderbook(self, snap: OrderbookSnapshot) -> None:
        async with self._lock:
            self._orderbook = snap

    async def append_minute_bar(self, bar: OHLCV) -> None:
        async with self._lock:
            self._ohlcv.append(bar)

    # ------------------------------------------------------------------- reads
    # Reads are sync — safe in asyncio (single-threaded); deque reads are atomic.

    @property
    def bar_count(self) -> int:
        return len(self._ohlcv)

    def get_ohlcv(self, n: int | None = None) -> pd.DataFrame:
        bars = list(self._ohlcv)
        if n is not None:
            bars = bars[-n:]
        if not bars:
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume"],
                index=pd.DatetimeIndex([], name="ts"),
            )
        return pd.DataFrame(
            {
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
            },
            index=pd.DatetimeIndex([b.ts for b in bars], name="ts"),
        )

    def get_trades_since(self, since: datetime) -> list[Trade]:
        return [t for t in self._trades if t.ts >= since]

    def latest_quote(self) -> Quote | None:
        return self._quotes[-1] if self._quotes else None

    def latest_orderbook(self) -> OrderbookSnapshot | None:
        return self._orderbook


class Aggregator:
    """Collection of MarketBuffers keyed by coin name."""

    def __init__(self, markets: list[str]) -> None:
        self.buffers: dict[str, MarketBuffer] = {m: MarketBuffer() for m in markets}

    def __getitem__(self, market: str) -> MarketBuffer:
        return self.buffers[market]

    def __contains__(self, market: object) -> bool:
        return market in self.buffers

    @property
    def markets(self) -> list[str]:
        return list(self.buffers.keys())
