from __future__ import annotations

from datetime import UTC, datetime, timedelta

from kalshi_botv3.exchange.types import OHLCV, Trade

_NEG_INF = float("-inf")
_POS_INF = float("inf")


def _truncate_to_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0, tzinfo=UTC if dt.tzinfo is None else dt.tzinfo)


class MinuteBarBuilder:
    """Accumulates trades into 1-minute OHLCV bars.

    Call on_trade() for every incoming trade.
    Returns a closed OHLCV when a minute boundary is crossed, else None.
    Unit-testable with synthetic trade streams.
    """

    def __init__(self) -> None:
        self._bar_ts: datetime | None = None
        self._open = 0.0
        self._high = _NEG_INF
        self._low = _POS_INF
        self._close = 0.0
        self._volume = 0.0

    def on_trade(self, trade: Trade) -> OHLCV | None:
        bar_ts = _truncate_to_minute(trade.ts)

        if self._bar_ts is None:
            self._start_bar(bar_ts, trade)
            return None

        if bar_ts > self._bar_ts:
            closed = self._close_bar()
            self._start_bar(bar_ts, trade)
            return closed

        self._update_bar(trade)
        return None

    def _start_bar(self, bar_ts: datetime, trade: Trade) -> None:
        self._bar_ts = bar_ts
        self._open = trade.price
        self._high = trade.price
        self._low = trade.price
        self._close = trade.price
        self._volume = trade.size

    def _update_bar(self, trade: Trade) -> None:
        self._high = max(self._high, trade.price)
        self._low = min(self._low, trade.price)
        self._close = trade.price
        self._volume += trade.size

    def _close_bar(self) -> OHLCV:
        assert self._bar_ts is not None
        return OHLCV(
            ts=self._bar_ts,
            open=self._open,
            high=self._high,
            low=self._low,
            close=self._close,
            volume=self._volume,
        )

    def flush(self) -> OHLCV | None:
        """Close and return the current in-progress bar (e.g., on shutdown)."""
        if self._bar_ts is None:
            return None
        bar = self._close_bar()
        self._bar_ts = None
        return bar

    @staticmethod
    def synthetic_stream(
        start: datetime,
        prices: list[float],
        interval_s: int = 30,
    ) -> list[Trade]:
        """Generate a synthetic trade stream for testing."""
        trades = []
        ts = start
        for price in prices:
            trades.append(Trade(ts=ts, price=price, size=1.0, side="buy"))
            ts = ts + timedelta(seconds=interval_s)
        return trades
