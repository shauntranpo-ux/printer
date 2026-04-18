from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Window(Base):
    __tablename__ = "windows"
    __table_args__ = (
        UniqueConstraint("market", "start_ts", name="uq_window_market_start_ts"),
        Index("ix_windows_start_ts", "start_ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    window_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    market: Mapped[str] = mapped_column(String, index=True)
    kalshi_ticker: Mapped[str | None] = mapped_column(String, nullable=True)
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )


class Feature(Base):
    __tablename__ = "features"

    id: Mapped[int] = mapped_column(primary_key=True)
    window_fk: Mapped[int] = mapped_column(ForeignKey("windows.id"), index=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    return_1m: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_5m: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_15m: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_vol_60m: Mapped[float | None] = mapped_column(Float, nullable=True)
    atr_percentile: Mapped[float | None] = mapped_column(Float, nullable=True)
    btc_return_5m: Mapped[float | None] = mapped_column(Float, nullable=True)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    window_fk: Mapped[int] = mapped_column(ForeignKey("windows.id"), index=True)
    strategy: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)  # YES / NO / SKIP
    p_model: Mapped[float | None] = mapped_column(Float, nullable=True)
    p_market: Mapped[float | None] = mapped_column(Float, nullable=True)
    edge: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(String(500))
    features_used: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_status", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    decision_fk: Mapped[int] = mapped_column(ForeignKey("decisions.id"), index=True)
    kalshi_order_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    side: Mapped[str] = mapped_column(String)
    quantity: Mapped[int] = mapped_column(Integer)
    limit_price_cents: Mapped[int] = mapped_column(Integer)
    fill_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String)  # pending/filled/rejected/canceled/simulated
    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)


class Outcome(Base):
    __tablename__ = "outcomes"
    __table_args__ = (Index("ix_outcomes_resolved_at", "resolved_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    order_fk: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True, index=True)
    settlement_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_cents: Mapped[int] = mapped_column(Integer)
    won: Mapped[bool] = mapped_column(Boolean)
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
