"""Binance WebSocket combined-streams client.

Verified 2026-04-17 — https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams
  URL:     wss://stream.binance.com:9443/stream?streams=<s1>/<s2>/...
  Streams: <symbol>@bookTicker, <symbol>@aggTrade, <symbol>@depth20@100ms
  Wrapper: {"stream": "<name>", "data": {...}}

HYPE: HYPEUSDT is available on Binance spot (confirmed 2026-04-17).
All 7 coins use standard USDT pairs — no skipped markets.

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
from kalshi_botv3.exchange.symbols import binance_to_coin
from kalshi_botv3.exchange.types import OrderbookSnapshot, Quote, Trade
from kalshi_botv3.utils.events import WS_CONNECTED, WS_RECONNECTING
from kalshi_botv3.utils.logging import get_logger

_BASE = "wss://stream.binance.com:9443/stream"
_STREAM_TYPES = ("bookTicker", "aggTrade", "depth20@100ms")
_RECV_TIMEOUT = 30.0
_MAX_BACKOFF = 60.0

logger = get_logger("exchange.binance")


# ---------------------------------------------------------------------------
# Pure parse functions
# ---------------------------------------------------------------------------


def _ms_to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def parse_binance_message(
    msg: dict[str, Any],
) -> tuple[str, Trade | Quote | OrderbookSnapshot] | None:
    """Parse a Binance combined-stream wrapper. Returns (coin, event) or None."""
    stream: str = msg.get("stream", "")
    data: dict[str, Any] = msg.get("data", {})

    # Extract symbol from stream name (e.g. "btcusdt@bookTicker" → "BTCUSDT")
    parts = stream.split("@")
    if len(parts) < 2:
        return None
    symbol = parts[0].upper()
    coin = binance_to_coin(symbol)
    if coin is None:
        return None

    event_type: str = data.get("e", "")

    try:
        if event_type == "bookTicker":
            quote = Quote(
                ts=_ms_to_utc(int(data.get("E", 0))),
                bid=float(data["b"]),
                ask=float(data["a"]),
                bid_size=float(data["B"]),
                ask_size=float(data["A"]),
            )
            return coin, quote

        if event_type == "aggTrade":
            side = "sell" if bool(data["m"]) else "buy"
            trade = Trade(
                ts=_ms_to_utc(int(data["T"])),
                price=float(data["p"]),
                size=float(data["q"]),
                side=side,
            )
            return coin, trade

        if "depth" in stream:
            bids = tuple(
                (float(b[0]), float(b[1])) for b in data.get("bids", [])
            )
            asks = tuple(
                (float(a[0]), float(a[1])) for a in data.get("asks", [])
            )
            snap = OrderbookSnapshot(
                ts=datetime.now(UTC),
                bids=bids,
                asks=asks,
            )
            return coin, snap

    except (KeyError, ValueError, TypeError):
        pass

    return None


# ---------------------------------------------------------------------------
# WebSocket client
# ---------------------------------------------------------------------------


def _build_url(symbols: list[str]) -> str:
    streams = []
    for sym in symbols:
        s = sym.lower()
        for stype in _STREAM_TYPES:
            streams.append(f"{s}@{stype}")
    return f"{_BASE}?streams={'/'.join(streams)}"


class BinanceWebSocket:
    def __init__(
        self,
        aggregator: Aggregator,
        bar_builders: dict[str, MinuteBarBuilder],
        symbols: list[str],  # Binance symbols, e.g. ["BTCUSDT", ...]
    ) -> None:
        self._aggregator = aggregator
        self._bar_builders = bar_builders
        self._symbols = symbols
        self._url = _build_url(symbols)
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
                logger.warning(WS_RECONNECTING, exchange="binance", attempt=attempt, error=str(exc))
                attempt += 1
                await asyncio.sleep(delay)

    async def stop(self) -> None:
        self._stopped = True
        self._is_connected = False

    async def _run_once(self) -> None:
        async with websockets.connect(self._url, ping_interval=20, ping_timeout=10) as ws:
            self._is_connected = True
            logger.info(WS_CONNECTED, exchange="binance", symbols=len(self._symbols))
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=_RECV_TIMEOUT)
                except TimeoutError:
                    logger.warning("heartbeat_timeout", exchange="binance")
                    break
                self._last_msg_ts = datetime.now(UTC)
                await self._handle(json.loads(raw))

    async def _handle(self, msg: dict[str, Any]) -> None:
        result = parse_binance_message(msg)
        if result is None:
            return
        coin, event = result
        if coin not in self._aggregator:
            return
        if isinstance(event, Trade):
            await self._aggregator[coin].add_trade(event)
            bar = self._bar_builders[coin].on_trade(event)
            if bar is not None:
                await self._aggregator[coin].append_minute_bar(bar)
        elif isinstance(event, Quote):
            await self._aggregator[coin].add_quote(event)
        elif isinstance(event, OrderbookSnapshot):
            await self._aggregator[coin].update_orderbook(event)
