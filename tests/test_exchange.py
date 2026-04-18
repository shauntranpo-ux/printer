from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
import respx
from httpx import Response

from kalshi_botv3.exchange.binance_ws import parse_binance_message
from kalshi_botv3.exchange.buffers import Aggregator, MarketBuffer
from kalshi_botv3.exchange.coinbase_ws import parse_ticker_events, parse_trades_events
from kalshi_botv3.exchange.historical import backfill_ohlcv
from kalshi_botv3.exchange.manager import ExchangeManager
from kalshi_botv3.exchange.minute_bars import MinuteBarBuilder
from kalshi_botv3.exchange.types import OHLCV, Quote, Trade

_UTC = UTC
_TS = datetime(2026, 4, 17, 14, 0, 0, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# test_minute_bar_builder_closes_on_boundary
# ---------------------------------------------------------------------------


def test_minute_bar_builder_closes_on_boundary() -> None:
    builder = MinuteBarBuilder()
    t1 = Trade(ts=datetime(2026, 4, 17, 14, 0, 10, tzinfo=_UTC), price=100.0, size=1.0, side="buy")
    t2 = Trade(ts=datetime(2026, 4, 17, 14, 0, 50, tzinfo=_UTC), price=102.0, size=0.5, side="sell")
    t3 = Trade(ts=datetime(2026, 4, 17, 14, 1, 5, tzinfo=_UTC), price=101.0, size=2.0, side="buy")

    assert builder.on_trade(t1) is None
    assert builder.on_trade(t2) is None
    bar = builder.on_trade(t3)

    assert bar is not None
    assert bar.ts == datetime(2026, 4, 17, 14, 0, 0, tzinfo=_UTC)
    assert bar.open == pytest.approx(100.0)
    assert bar.high == pytest.approx(102.0)
    assert bar.low == pytest.approx(100.0)
    assert bar.close == pytest.approx(102.0)
    assert bar.volume == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# test_buffer_get_ohlcv_returns_dataframe
# ---------------------------------------------------------------------------


async def test_buffer_get_ohlcv_returns_dataframe() -> None:
    buf = MarketBuffer()
    bar = OHLCV(ts=_TS, open=100.0, high=102.0, low=99.0, close=101.0, volume=10.0)
    await buf.append_minute_bar(bar)

    df = buf.get_ohlcv()
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 1
    assert df.iloc[0]["close"] == pytest.approx(101.0)
    assert df.index.name == "ts"


# ---------------------------------------------------------------------------
# test_buffer_trades_since
# ---------------------------------------------------------------------------


async def test_buffer_trades_since() -> None:
    buf = MarketBuffer()
    old = Trade(ts=datetime(2026, 4, 17, 13, 59, 0, tzinfo=_UTC), price=100.0, size=1.0, side="buy")
    new = Trade(ts=_TS, price=101.0, size=0.5, side="sell")
    await buf.add_trade(old)
    await buf.add_trade(new)

    result = buf.get_trades_since(_TS)
    assert len(result) == 1
    assert result[0].price == pytest.approx(101.0)


# ---------------------------------------------------------------------------
# test_coinbase_parser_handles_ticker_message
# ---------------------------------------------------------------------------


def test_coinbase_parser_handles_ticker_message() -> None:
    msg = {
        "channel": "ticker",
        "timestamp": "2026-04-17T14:00:00.000Z",
        "events": [
            {
                "type": "update",
                "tickers": [
                    {
                        "product_id": "BTC-USD",
                        "price": "45200.50",
                        "best_bid": "45200.00",
                        "best_bid_size": "0.5",
                        "best_ask": "45201.00",
                        "best_ask_size": "1.2",
                        "time": "2026-04-17T14:00:00.000Z",
                    }
                ],
            }
        ],
    }
    quotes = parse_ticker_events(msg)
    assert len(quotes) == 1
    coin, quote = quotes[0]
    assert coin == "BTC"
    assert isinstance(quote, Quote)
    assert quote.bid == pytest.approx(45200.00)
    assert quote.ask == pytest.approx(45201.00)
    assert quote.bid_size == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# test_coinbase_parser_handles_match_message  (market_trades)
# ---------------------------------------------------------------------------


def test_coinbase_parser_handles_match_message() -> None:
    msg = {
        "channel": "market_trades",
        "events": [
            {
                "type": "update",
                "trades": [
                    {
                        "trade_id": "123",
                        "product_id": "BTC-USD",
                        "price": "45200.50",
                        "size": "0.5",
                        "side": "BUY",
                        "time": "2026-04-17T14:00:00.000Z",
                    }
                ],
            }
        ],
    }
    trades = parse_trades_events(msg)
    assert len(trades) == 1
    coin, trade = trades[0]
    assert coin == "BTC"
    assert isinstance(trade, Trade)
    assert trade.price == pytest.approx(45200.50)
    assert trade.side == "buy"
    assert trade.size == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# test_binance_parser_book_ticker
# ---------------------------------------------------------------------------


def test_binance_parser_book_ticker() -> None:
    msg = {
        "stream": "btcusdt@bookTicker",
        "data": {
            "e": "bookTicker",
            "s": "BTCUSDT",
            "b": "45200.50",
            "B": "0.5",
            "a": "45201.00",
            "A": "1.2",
            "E": 1713358800000,  # 2026-04-17 14:00:00 UTC
        },
    }
    result = parse_binance_message(msg)
    assert result is not None
    coin, event = result
    assert coin == "BTC"
    assert isinstance(event, Quote)
    assert event.bid == pytest.approx(45200.50)
    assert event.ask == pytest.approx(45201.00)


# ---------------------------------------------------------------------------
# test_binance_parser_agg_trade
# ---------------------------------------------------------------------------


def test_binance_parser_agg_trade() -> None:
    msg = {
        "stream": "btcusdt@aggTrade",
        "data": {
            "e": "aggTrade",
            "s": "BTCUSDT",
            "p": "45200.50",
            "q": "0.5",
            "T": 1713358800000,
            "m": False,  # buyer is taker → "buy"
        },
    }
    result = parse_binance_message(msg)
    assert result is not None
    coin, event = result
    assert coin == "BTC"
    assert isinstance(event, Trade)
    assert event.price == pytest.approx(45200.50)
    assert event.side == "buy"


# ---------------------------------------------------------------------------
# test_backfill_populates_buffer
# ---------------------------------------------------------------------------


async def test_backfill_populates_buffer() -> None:
    aggregator = Aggregator(["BTC"])
    # [time_unix, low, high, open, close, volume] — Coinbase Exchange format
    candle_rows = [
        [1713355200, 44900.0, 45100.0, 45000.0, 45050.0, 10.0],
        [1713355260, 45050.0, 45200.0, 45050.0, 45150.0, 8.5],
    ]
    with respx.mock() as mock:
        mock.get("https://api.exchange.coinbase.com/products/BTC-USD/candles").mock(
            return_value=Response(200, json=candle_rows)
        )
        await backfill_ohlcv(aggregator, "BTC", minutes=2)

    df = aggregator["BTC"].get_ohlcv()
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 2
    assert df.iloc[0]["open"] == pytest.approx(45000.0)
    assert df.iloc[1]["close"] == pytest.approx(45150.0)


# ---------------------------------------------------------------------------
# test_manager_is_healthy_requires_recent_messages
# ---------------------------------------------------------------------------


async def test_manager_is_healthy_requires_recent_messages() -> None:
    manager = ExchangeManager(markets=["BTC"])

    # Not healthy before start
    assert not manager.is_healthy()

    # Simulate connected WS with recent messages
    now = datetime.now(_UTC)
    manager._coinbase_ws._is_connected = True
    manager._coinbase_ws._last_msg_ts = now
    manager._binance_ws._is_connected = True
    manager._binance_ws._last_msg_ts = now

    # Still not healthy — fewer than 60 bars
    assert not manager.is_healthy()

    # Add 60 bars
    bar = OHLCV(ts=_TS, open=100.0, high=102.0, low=99.0, close=101.0, volume=1.0)
    for _ in range(60):
        await manager.aggregator["BTC"].append_minute_bar(bar)

    assert manager.is_healthy()

    # Stale Coinbase message → unhealthy
    manager._coinbase_ws._last_msg_ts = now - timedelta(seconds=61)
    assert not manager.is_healthy()
