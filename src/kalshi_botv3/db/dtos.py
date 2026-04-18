import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class WindowDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    window_id: str
    market: str
    kalshi_ticker: str | None = None
    start_ts: datetime.datetime
    end_ts: datetime.datetime
    open_price: float | None = None
    close_price: float | None = None
    resolved: bool = False
    created_at: datetime.datetime | None = None


class FeatureDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    window_fk: int
    computed_at: datetime.datetime | None = None
    payload: dict[str, Any] | None = None
    return_1m: float | None = None
    return_5m: float | None = None
    return_15m: float | None = None
    realized_vol_60m: float | None = None
    atr_percentile: float | None = None
    btc_return_5m: float | None = None
    degraded: bool = False


class DecisionDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    window_fk: int
    strategy: str
    side: str
    p_model: float | None = None
    p_market: float | None = None
    edge: float | None = None
    confidence: float | None = None
    reason: str
    features_used: dict[str, Any] | None = None
    decided_at: datetime.datetime | None = None


class OrderDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    decision_fk: int
    kalshi_order_id: str | None = None
    side: str
    quantity: int
    limit_price_cents: int
    fill_price_cents: int | None = None
    status: str
    placed_at: datetime.datetime | None = None
    filled_at: datetime.datetime | None = None
    error: str | None = None


class OutcomeDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    order_fk: int
    settlement_price: float | None = None
    pnl_cents: int
    won: bool
    resolved_at: datetime.datetime | None = None
