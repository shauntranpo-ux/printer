from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OrderSide(StrEnum):
    YES = "yes"
    NO = "no"


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(StrEnum):
    RESTING = "resting"
    CANCELED = "canceled"
    EXECUTED = "executed"
    PENDING = "pending"


class MarketStatus(StrEnum):
    UNOPENED = "unopened"
    OPEN = "open"
    PAUSED = "paused"
    CLOSED = "closed"
    SETTLED = "settled"


class OrderbookLevel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    price: int = Field(..., description="Price in cents (0-100)")
    quantity: int


class Orderbook(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticker: str
    yes: list[OrderbookLevel] = Field(default_factory=list)
    no: list[OrderbookLevel] = Field(default_factory=list)


class Market(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticker: str
    series_ticker: str
    title: str
    status: MarketStatus
    yes_bid: int = Field(0, description="Best YES bid in cents")
    yes_ask: int = Field(0, description="Best YES ask in cents")
    volume: int = 0
    open_time: datetime | None = None
    close_time: datetime | None = None
    expiration_time: datetime | None = None
    result: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class MarketListResponse(BaseModel):
    markets: list[Market]
    cursor: str = ""


class OrderRequest(BaseModel):
    ticker: str
    client_order_id: str
    type: OrderType = OrderType.LIMIT
    action: str = "buy"
    side: OrderSide
    count: int = Field(..., gt=0)
    yes_price: int | None = Field(None, description="Price in cents (1-99) for YES side")
    no_price: int | None = Field(None, description="Price in cents (1-99) for NO side")


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_id: str
    client_order_id: str
    ticker: str
    side: OrderSide
    type: OrderType
    status: OrderStatus
    count: int
    filled_count: int = 0
    yes_price: int | None = None
    no_price: int | None = None
    created_time: datetime | None = None


class Position(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticker: str
    position: int
    market_exposure: int = 0
    realized_pnl: int = 0
    unrealized_pnl: int = 0
    total_cost: int = 0


class Fill(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    fill_id: str
    order_id: str
    ticker: str
    side: OrderSide
    count: int
    yes_price: int
    is_taker: bool
    created_time: datetime | None = None


class ExchangeStatus(BaseModel):
    trading_active: bool
    exchange_active: bool
