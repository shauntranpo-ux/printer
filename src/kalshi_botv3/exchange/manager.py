from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from kalshi_botv3.exchange.binance_ws import BinanceWebSocket
from kalshi_botv3.exchange.buffers import Aggregator
from kalshi_botv3.exchange.coinbase_ws import CoinbaseWebSocket
from kalshi_botv3.exchange.historical import backfill_ohlcv
from kalshi_botv3.exchange.minute_bars import MinuteBarBuilder
from kalshi_botv3.exchange.symbols import COIN_TO_BINANCE, COIN_TO_COINBASE
from kalshi_botv3.utils.logging import get_logger

_HEALTHY_MSG_MAX_AGE_S = 60
_HEALTHY_MIN_BARS = 60

logger = get_logger("exchange.manager")

_ALL_MARKETS = ["BTC", "ETH", "XRP", "SOL", "DOGE", "HYPE", "BNB"]


class ExchangeManager:
    """Orchestrates Coinbase + Binance WS feeds, backfill, and health checks."""

    def __init__(
        self,
        markets: list[str] | None = None,
        backfill_minutes: int = 90,
    ) -> None:
        self._markets = markets if markets is not None else _ALL_MARKETS
        self._backfill_minutes = backfill_minutes
        self._aggregator = Aggregator(self._markets)
        self._bar_builders: dict[str, MinuteBarBuilder] = {
            m: MinuteBarBuilder() for m in self._markets
        }
        coinbase_products = [COIN_TO_COINBASE[m] for m in self._markets]
        binance_symbols = [COIN_TO_BINANCE[m] for m in self._markets]

        self._coinbase_ws = CoinbaseWebSocket(
            self._aggregator, self._bar_builders, coinbase_products
        )
        self._binance_ws = BinanceWebSocket(
            self._aggregator, self._bar_builders, binance_symbols
        )
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def aggregator(self) -> Aggregator:
        return self._aggregator

    async def start(self) -> None:
        logger.info("exchange_manager_starting", markets=self._markets)

        # Backfill all markets in parallel
        await asyncio.gather(
            *[
                backfill_ohlcv(self._aggregator, m, self._backfill_minutes)
                for m in self._markets
            ],
            return_exceptions=True,
        )

        self._tasks.append(asyncio.create_task(self._coinbase_ws.run()))
        self._tasks.append(asyncio.create_task(self._binance_ws.run()))
        logger.info("exchange_manager_started")

    async def stop(self) -> None:
        await self._coinbase_ws.stop()
        await self._binance_ws.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("exchange_manager_stopped")

    def is_healthy(self) -> bool:
        """True when both WS are connected, last message <60 s ago, all markets ≥60 bars."""
        if not self._coinbase_ws.is_connected:
            return False
        if not self._binance_ws.is_connected:
            return False

        now = datetime.now(UTC)
        cb_last = self._coinbase_ws.last_msg_ts
        bn_last = self._binance_ws.last_msg_ts

        if cb_last is None or (now - cb_last).total_seconds() > _HEALTHY_MSG_MAX_AGE_S:
            return False
        if bn_last is None or (now - bn_last).total_seconds() > _HEALTHY_MSG_MAX_AGE_S:
            return False

        for market in self._markets:
            if self._aggregator[market].bar_count < _HEALTHY_MIN_BARS:
                return False

        return True
