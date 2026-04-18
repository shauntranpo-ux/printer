"""Coinbase Advanced Trade WebSocket client.

Verified 2026-04-17 — https://docs.cdp.coinbase.com/advanced-trade/docs/ws-overview
  URL:      wss://advanced-trade-ws.coinbase.com
  Auth:     not required for public market-data channels
  Channels: heartbeats, ticker, market_trades
  Msg fmt:  {"channel": "...", "events": [...], "timestamp": "...", "sequence_num": N}

Reconnect: exponential backoff 1 s → 60 s.
Watchdog:  no message for 30 s triggers reconnect.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import websockets
import websockets.exceptions

from kalshi_botv3.exchange.buffers import Aggregator
from kalshi_botv3.exchange.minute_bars import MinuteBarBuilder
from kalshi_botv3.exchange.symbols import coinbase_to_coin
from kalshi_botv3.exchange.types import Quote, Trade
from kalshi_botv3.utils.events import WS_CONNECTED, WS_RECONNECTING
from kalshi_botv3.utils.logging import get_logger

_URL = "wss://advanced-trade-ws.coinbase.com"
_RECV_TIMEOUT = 30.0
_MAX_BACKOFF = 60.0

logger = get_logger("exchange.coinbase")


# ---------------------------------------------------------------------------
# Pure parse functions (no I/O — importable for unit tests)
# ---------------------------------------------------------------------------


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse_ticker_events(msg: dict[str, Any]) -> list[tuple[str, Quote]]:
    """Return (coin, Quote) pairs from a Coinbase ticker channel message."""
    results: list[tuple[str, Quote]] = []
    for event in msg.get("events", []):
        for ticker in event.get("tickers", []):
            product_id = ticker.get("product_id", "")
            coin = coinbase_to_coin(product_id)
            if coin is None:
                continue
            try:
                ts_raw = ticker.get("time") or msg.get("timestamp", "")
                ts = _parse_ts(ts_raw) if ts_raw else datetime.now(UTC)
                quote = Quote(
                    ts=ts,
                    bid=float(ticker.get("best_bid", 0) or 0),
                    ask=float(ticker.get("best_ask", 0) or 0),
                    bid_size=float(ticker.get("best_bid_size", 0) or 0),
                    ask_size=float(ticker.get("best_ask_size", 0) or 0),
                )
                results.append((coin, quote))
            except (KeyError, ValueError):
                pass
    return results


def parse_trades_events(msg: dict[str, Any]) -> list[tuple[str, Trade]]:
    """Return (coin, Trade) pairs from a Coinbase market_trades channel message."""
    results: list[tuple[str, Trade]] = []
    for event in msg.get("events", []):
        for trade in event.get("trades", []):
            product_id = trade.get("product_id", "")
            coin = coinbase_to_coin(product_id)
            if coin is None:
                continue
            try:
                ts = _parse_ts(trade.get("time", ""))
                side = trade.get("side", "buy").lower()
                t = Trade(
                    ts=ts,
                    price=float(trade["price"]),
                    size=float(trade["size"]),
                    side=side,
                )
                results.append((coin, t))
            except (KeyError, ValueError):
                pass
    return results


# ---------------------------------------------------------------------------
# WebSocket client
# ---------------------------------------------------------------------------


class CoinbaseWebSocket:
    def __init__(
        self,
        aggregator: Aggregator,
        bar_builders: dict[str, MinuteBarBuilder],
        products: list[str],
    ) -> None:
        self._aggregator = aggregator
        self._bar_builders = bar_builders
        self._products = products
        self._is_connected = False
        self._last_msg_ts: datetime | None = None
        self._stopped = False

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def last_msg_ts(self) -> datetime | None:
        return self._last_msg_ts

    async def run(self) -> None:
        attempt = 0
        while not self._stopped:
            try:
                await self._run_once()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._is_connected = False
                delay = min(1.0 * (2**attempt), _MAX_BACKOFF)
                logger.warning(
                    WS_RECONNECTING, exchange="coinbase", attempt=attempt, error=str(exc)
                )
                attempt += 1
                await asyncio.sleep(delay)

    async def stop(self) -> None:
        self._stopped = True
        self._is_connected = False

    async def _run_once(self) -> None:
        async with websockets.connect(_URL, ping_interval=20, ping_timeout=10) as ws:
            await self._subscribe(ws)
            self._is_connected = True
            logger.info(WS_CONNECTED, exchange="coinbase", products=len(self._products))
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=_RECV_TIMEOUT)
                except TimeoutError:
                    logger.warning("heartbeat_timeout", exchange="coinbase")
                    break
                self._last_msg_ts = datetime.now(UTC)
                await self._handle(json.loads(raw))

    async def _subscribe(self, ws: Any) -> None:
        for channel in ("heartbeats", "ticker", "market_trades"):
            payload: dict[str, Any] = {"type": "subscribe", "channel": channel}
            if channel != "heartbeats":
                payload["product_ids"] = self._products
            await ws.send(json.dumps(payload))

    async def _handle(self, msg: dict[str, Any]) -> None:
        channel = msg.get("channel", "")
        if channel == "ticker":
            for coin, quote in parse_ticker_events(msg):
                if coin in self._aggregator:
                    await self._aggregator[coin].add_quote(quote)
        elif channel == "market_trades":
            for coin, trade in parse_trades_events(msg):
                if coin in self._aggregator:
                    await self._aggregator[coin].add_trade(trade)
                    bar = self._bar_builders[coin].on_trade(trade)
                    if bar is not None:
                        await self._aggregator[coin].append_minute_bar(bar)
        # heartbeats and subscriptions confirmations are silently accepted
