"""Kalshi REST API v2 async client.

Verified against Kalshi API docs — 2026-04-17
Source: https://docs.kalshi.com/api-reference/

Base URLs:
  Demo:  https://demo-api.kalshi.co/trade-api/v2
  Prod:  https://trading-api.kalshi.com/trade-api/v2

Auth (RSA-PSS):
  Sign: f"{timestamp_ms}{METHOD}{path_no_query}"
  Algorithm: RSA-PSS, SHA-256, MGF1(SHA-256), salt_length=32 (DIGEST_LENGTH)
  Headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE

Rate limits (Basic tier, default at signup):
  Read:  20 req/s
  Write: 10 req/s

Endpoint paths:
  GET  /exchange/status
  GET  /markets?series_ticker=&status=&limit=
  GET  /markets/{ticker}
  GET  /markets/{ticker}/orderbook?depth=
  POST /portfolio/orders
  DEL  /portfolio/orders/{order_id}
  GET  /portfolio/positions
  GET  /portfolio/fills?ticker=&limit=
"""

import asyncio
import time
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from kalshi_botv3.kalshi.auth import KalshiSigner
from kalshi_botv3.kalshi.models import (
    ExchangeStatus,
    Fill,
    Market,
    MarketListResponse,
    MarketStatus,
    Orderbook,
    OrderbookLevel,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from kalshi_botv3.utils.events import KALSHI_API_ERROR
from kalshi_botv3.utils.logging import get_logger

_BASE_URLS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "prod": "https://trading-api.kalshi.com/trade-api/v2",
}
_BUCKET_CAPACITY = 10.0
_BUCKET_REFILL_RATE = 10.0  # tokens/sec (conservative: Basic read limit is 20)

logger = get_logger("kalshi.client")


class _TokenBucket:
    def __init__(self, capacity: float, rate: float) -> None:
        self._capacity = capacity
        self._rate = rate
        self._tokens = capacity
        self._last = time.monotonic()

    async def acquire(self) -> None:
        while True:
            now = time.monotonic()
            self._tokens = min(
                self._capacity, self._tokens + (now - self._last) * self._rate
            )
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            await asyncio.sleep(1.0 / self._rate)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(exc, httpx.TransportError)


def _parse_orderbook_levels(raw: list[Any]) -> list[OrderbookLevel]:
    levels = []
    for item in raw:
        if isinstance(item, list) and len(item) == 2:
            price = round(float(item[0]) * 100)
            qty = round(float(item[1]))
        elif isinstance(item, dict):
            price = round(float(item.get("price", 0)) * 100)
            qty = int(item.get("quantity", item.get("count", 0)) or 0)
        else:
            continue
        levels.append(OrderbookLevel(price=price, quantity=qty))
    return levels


def _parse_market(raw: dict[str, Any]) -> Market:
    return Market(
        ticker=raw.get("ticker", ""),
        series_ticker=raw.get("series_ticker", ""),
        title=raw.get("title", ""),
        status=MarketStatus(raw.get("status", "open")),
        yes_bid=int(raw.get("yes_bid", 0) or 0),
        yes_ask=int(raw.get("yes_ask", 0) or 0),
        volume=int(raw.get("volume", 0) or 0),
        open_time=raw.get("open_time"),
        close_time=raw.get("close_time"),
        expiration_time=raw.get("expiration_time"),
        result=raw.get("result"),
        extra={
            k: v
            for k, v in raw.items()
            if k
            not in {
                "ticker",
                "series_ticker",
                "title",
                "status",
                "yes_bid",
                "yes_ask",
                "volume",
                "open_time",
                "close_time",
                "expiration_time",
                "result",
            }
        },
    )


def _parse_order_response(raw: dict[str, Any]) -> OrderResponse:
    order = raw.get("order", raw)
    return OrderResponse(
        order_id=order.get("order_id", ""),
        client_order_id=order.get("client_order_id", ""),
        ticker=order.get("ticker", ""),
        side=OrderSide(order.get("side", "yes")),
        type=OrderType(order.get("type", "limit")),
        status=OrderStatus(order.get("status", "resting")),
        count=int(order.get("count", 0)),
        filled_count=int(order.get("filled_count", 0)),
        yes_price=order.get("yes_price"),
        no_price=order.get("no_price"),
        created_time=order.get("created_time"),
    )


class HttpKalshiClient:
    """Async HTTP client for the Kalshi REST API v2."""

    def __init__(self, signer: KalshiSigner, kalshi_env: str = "demo") -> None:
        self._signer = signer
        self._base = _BASE_URLS[kalshi_env]
        self._http: httpx.AsyncClient | None = None
        self._bucket = _TokenBucket(_BUCKET_CAPACITY, _BUCKET_REFILL_RATE)

    async def __aenter__(self) -> "HttpKalshiClient":
        self._http = httpx.AsyncClient(base_url=self._base, timeout=15.0)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._http:
            await self._http.aclose()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("Use HttpKalshiClient as async context manager")
        return self._http

    def _path(self, url: str) -> str:
        """Extract /trade-api/v2/... path for signing (no query string)."""
        parsed = urlparse(self._base + url)
        return parsed.path

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        reraise=True,
    )
    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        await self._bucket.acquire()
        sign_path = self._path(path)
        headers = self._signer.auth_headers(method, sign_path)
        resp = self._client().request(method, path, headers=headers, **kwargs)
        r = await resp
        if not r.is_success:
            logger.error(
                KALSHI_API_ERROR,
                method=method,
                path=path,
                status=r.status_code,
                body=r.text[:500],
            )
            r.raise_for_status()
        data: dict[str, Any] = r.json()
        return data

    async def get_exchange_status(self) -> ExchangeStatus:
        data = await self._request("GET", "/exchange/status")
        ex = data.get("exchange_status", data)
        return ExchangeStatus(
            trading_active=bool(ex.get("trading_active", False)),
            exchange_active=bool(ex.get("exchange_active", False)),
        )

    async def get_markets(
        self,
        series_ticker: str,
        status: str = "open",
        limit: int = 100,
    ) -> MarketListResponse:
        params: dict[str, Any] = {
            "series_ticker": series_ticker,
            "status": status,
            "limit": limit,
        }
        data = await self._request("GET", "/markets", params=params)
        markets = [_parse_market(m) for m in data.get("markets", [])]
        return MarketListResponse(markets=markets, cursor=data.get("cursor", ""))

    async def get_market(self, ticker: str) -> Market:
        data = await self._request("GET", f"/markets/{ticker}")
        return _parse_market(data.get("market", data))

    async def get_orderbook(self, ticker: str, depth: int = 10) -> Orderbook:
        data = await self._request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": depth}
        )
        ob = data.get("orderbook", data)
        return Orderbook(
            ticker=ticker,
            yes=_parse_orderbook_levels(ob.get("yes", [])),
            no=_parse_orderbook_levels(ob.get("no", [])),
        )

    async def place_order(
        self,
        ticker: str,
        side: OrderSide,
        count: int,
        price_cents: int,
        client_order_id: str,
    ) -> OrderResponse:
        req = OrderRequest(
            ticker=ticker,
            client_order_id=client_order_id,
            side=side,
            count=count,
            yes_price=price_cents if side == OrderSide.YES else None,
            no_price=price_cents if side == OrderSide.NO else None,
        )
        data = await self._request(
            "POST",
            "/portfolio/orders",
            json=req.model_dump(exclude_none=True),
        )
        return _parse_order_response(data)

    async def cancel_order(self, order_id: str) -> OrderResponse:
        data = await self._request("DELETE", f"/portfolio/orders/{order_id}")
        return _parse_order_response(data)

    async def get_positions(self) -> list[Position]:
        data = await self._request("GET", "/portfolio/positions")
        positions = []
        for p in data.get("market_positions", data.get("positions", [])):
            positions.append(
                Position(
                    ticker=p.get("ticker", ""),
                    position=int(p.get("position", 0)),
                    market_exposure=int(p.get("market_exposure", 0)),
                    realized_pnl=int(p.get("realized_pnl", 0)),
                    unrealized_pnl=int(p.get("unrealized_pnl", 0)),
                    total_cost=int(p.get("total_cost", 0)),
                )
            )
        return positions

    async def get_fills(
        self, ticker: str | None = None, limit: int = 100
    ) -> list[Fill]:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        data = await self._request("GET", "/portfolio/fills", params=params)
        fills = []
        for f in data.get("fills", []):
            fills.append(
                Fill(
                    fill_id=f.get("fill_id", f.get("id", "")),
                    order_id=f.get("order_id", ""),
                    ticker=f.get("ticker", ""),
                    side=OrderSide(f.get("side", "yes")),
                    count=int(f.get("count", 0)),
                    yes_price=int(f.get("yes_price", 0)),
                    is_taker=bool(f.get("is_taker", False)),
                    created_time=f.get("created_time"),
                )
            )
        return fills


class MockKalshiClient:
    """Deterministic in-memory Kalshi client for dry-run / paper trading."""

    def __init__(self) -> None:
        self._orders: dict[str, OrderResponse] = {}
        self._fills: list[Fill] = []

    async def __aenter__(self) -> "MockKalshiClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    async def get_exchange_status(self) -> ExchangeStatus:
        return ExchangeStatus(trading_active=True, exchange_active=True)

    async def get_markets(
        self,
        series_ticker: str,
        status: str = "open",
        limit: int = 100,
    ) -> MarketListResponse:
        ticker = f"{series_ticker}-MOCK-26APR171500"
        return MarketListResponse(
            markets=[
                Market(
                    ticker=ticker,
                    series_ticker=series_ticker,
                    title=f"Mock market for {series_ticker}",
                    status=MarketStatus.OPEN,
                    yes_bid=48,
                    yes_ask=52,
                )
            ]
        )

    async def get_market(self, ticker: str) -> Market:
        return Market(
            ticker=ticker,
            series_ticker=ticker.split("-")[0],
            title=f"Mock: {ticker}",
            status=MarketStatus.OPEN,
            yes_bid=48,
            yes_ask=52,
        )

    async def get_orderbook(self, ticker: str, depth: int = 10) -> Orderbook:
        return Orderbook(
            ticker=ticker,
            yes=[OrderbookLevel(price=48, quantity=100)],
            no=[OrderbookLevel(price=48, quantity=100)],
        )

    async def place_order(
        self,
        ticker: str,
        side: OrderSide,
        count: int,
        price_cents: int,
        client_order_id: str,
    ) -> OrderResponse:
        order_id = f"mock-{uuid.uuid4().hex[:12]}"
        resp = OrderResponse(
            order_id=order_id,
            client_order_id=client_order_id,
            ticker=ticker,
            side=side,
            type=OrderType.LIMIT,
            status=OrderStatus.RESTING,
            count=count,
            filled_count=count,
            yes_price=price_cents if side == OrderSide.YES else None,
            no_price=price_cents if side == OrderSide.NO else None,
        )
        self._orders[order_id] = resp
        self._fills.append(
            Fill(
                fill_id=f"fill-{uuid.uuid4().hex[:12]}",
                order_id=order_id,
                ticker=ticker,
                side=side,
                count=count,
                yes_price=price_cents,
                is_taker=True,
            )
        )
        return resp

    async def cancel_order(self, order_id: str) -> OrderResponse:
        if order_id not in self._orders:
            raise KeyError(f"Unknown mock order: {order_id}")
        self._orders[order_id] = self._orders[order_id].model_copy(
            update={"status": OrderStatus.CANCELED}
        )
        return self._orders[order_id]

    async def get_positions(self) -> list[Position]:
        return []

    async def get_fills(
        self, ticker: str | None = None, limit: int = 100
    ) -> list[Fill]:
        fills = self._fills
        if ticker:
            fills = [f for f in fills if f.ticker == ticker]
        return fills[:limit]
