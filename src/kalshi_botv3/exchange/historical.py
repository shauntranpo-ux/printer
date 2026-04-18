"""Historical OHLCV backfill via Coinbase Exchange REST API.

Endpoint: GET https://api.exchange.coinbase.com/products/{product_id}/candles
Params:   granularity=60 (1-minute bars), start (ISO), end (ISO)
Response: [[time_unix, low, high, open, close, volume], ...] — newest first
Max:      300 candles per request (use pagination for more)

Public endpoint — no auth required.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from kalshi_botv3.exchange.buffers import Aggregator
from kalshi_botv3.exchange.symbols import to_coinbase
from kalshi_botv3.exchange.types import OHLCV
from kalshi_botv3.utils.logging import get_logger

_BASE = "https://api.exchange.coinbase.com"
_GRANULARITY = 60
_MAX_PER_REQUEST = 300

logger = get_logger("exchange.historical")


async def backfill_ohlcv(
    aggregator: Aggregator,
    market: str,
    minutes: int = 90,
) -> None:
    """Fetch the last N 1-minute bars for a market and populate its buffer."""
    product_id = to_coinbase(market)
    end = datetime.now(UTC)

    bars: list[OHLCV] = []
    chunk_end = end

    async with httpx.AsyncClient(base_url=_BASE, timeout=15.0) as client:
        remaining = minutes
        while remaining > 0:
            chunk_minutes = min(remaining, _MAX_PER_REQUEST)
            chunk_start = chunk_end - timedelta(minutes=chunk_minutes)
            try:
                resp = await client.get(
                    f"/products/{product_id}/candles",
                    params={
                        "granularity": _GRANULARITY,
                        "start": chunk_start.isoformat(),
                        "end": chunk_end.isoformat(),
                    },
                )
                resp.raise_for_status()
                raw = resp.json()
            except Exception as exc:
                logger.warning(
                    "backfill_failed",
                    market=market,
                    product_id=product_id,
                    error=str(exc),
                )
                break

            for row in raw:
                # [time_unix, low, high, open, close, volume]
                if len(row) < 6:
                    continue
                ts = datetime.fromtimestamp(float(row[0]), tz=UTC)
                bar = OHLCV(
                    ts=ts,
                    open=float(row[3]),
                    high=float(row[2]),
                    low=float(row[1]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
                bars.append(bar)

            chunk_end = chunk_start
            remaining -= chunk_minutes

    # Sort ascending by time before inserting
    bars.sort(key=lambda b: b.ts)

    for bar in bars:
        await aggregator[market].append_minute_bar(bar)

    logger.info("backfill_complete", market=market, bars=len(bars))
