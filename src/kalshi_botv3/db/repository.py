import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_botv3.db.dtos import (
    DecisionDTO,
    FeatureDTO,
    OrderDTO,
    OutcomeDTO,
    WindowDTO,
)
from kalshi_botv3.db.models import Decision, Feature, Order, Outcome, Window


class WindowRepo:
    async def create(self, session: AsyncSession, dto: WindowDTO) -> WindowDTO:
        row = Window(
            window_id=dto.window_id,
            market=dto.market,
            kalshi_ticker=dto.kalshi_ticker,
            start_ts=dto.start_ts,
            end_ts=dto.end_ts,
            open_price=dto.open_price,
            close_price=dto.close_price,
            resolved=dto.resolved,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return WindowDTO.model_validate(row)

    async def get_by_window_id(
        self, session: AsyncSession, window_id: str
    ) -> WindowDTO | None:
        result = await session.execute(
            select(Window).where(Window.window_id == window_id)
        )
        row = result.scalar_one_or_none()
        return WindowDTO.model_validate(row) if row is not None else None

    async def get_by_market_and_time(
        self, session: AsyncSession, market: str, start_ts: datetime.datetime
    ) -> WindowDTO | None:
        result = await session.execute(
            select(Window).where(Window.market == market, Window.start_ts == start_ts)
        )
        row = result.scalar_one_or_none()
        return WindowDTO.model_validate(row) if row is not None else None

    async def mark_resolved(
        self,
        session: AsyncSession,
        window_id: str,
        close_price: float | None = None,
    ) -> None:
        result = await session.execute(
            select(Window).where(Window.window_id == window_id)
        )
        row = result.scalar_one()
        row.resolved = True
        if close_price is not None:
            row.close_price = close_price
        await session.flush()

    async def list_unresolved_before(
        self, session: AsyncSession, cutoff: datetime.datetime
    ) -> list[WindowDTO]:
        result = await session.execute(
            select(Window).where(Window.resolved.is_(False), Window.end_ts <= cutoff)
        )
        return [WindowDTO.model_validate(r) for r in result.scalars()]


class FeatureRepo:
    async def create(self, session: AsyncSession, dto: FeatureDTO) -> FeatureDTO:
        row = Feature(
            window_fk=dto.window_fk,
            payload=dto.payload,
            return_1m=dto.return_1m,
            return_5m=dto.return_5m,
            return_15m=dto.return_15m,
            realized_vol_60m=dto.realized_vol_60m,
            atr_percentile=dto.atr_percentile,
            btc_return_5m=dto.btc_return_5m,
            degraded=dto.degraded,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return FeatureDTO.model_validate(row)

    async def get_for_window(
        self, session: AsyncSession, window_id: int
    ) -> FeatureDTO | None:
        result = await session.execute(
            select(Feature).where(Feature.window_fk == window_id)
        )
        row = result.scalar_one_or_none()
        return FeatureDTO.model_validate(row) if row is not None else None


class DecisionRepo:
    async def create(self, session: AsyncSession, dto: DecisionDTO) -> DecisionDTO:
        row = Decision(
            window_fk=dto.window_fk,
            strategy=dto.strategy,
            side=dto.side,
            p_model=dto.p_model,
            p_market=dto.p_market,
            edge=dto.edge,
            confidence=dto.confidence,
            reason=dto.reason,
            features_used=dto.features_used,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return DecisionDTO.model_validate(row)

    async def get_for_window(
        self, session: AsyncSession, window_id: int
    ) -> list[DecisionDTO]:
        result = await session.execute(
            select(Decision).where(Decision.window_fk == window_id)
        )
        return [DecisionDTO.model_validate(r) for r in result.scalars()]


class OrderRepo:
    async def create(self, session: AsyncSession, dto: OrderDTO) -> OrderDTO:
        row = Order(
            decision_fk=dto.decision_fk,
            kalshi_order_id=dto.kalshi_order_id,
            side=dto.side,
            quantity=dto.quantity,
            limit_price_cents=dto.limit_price_cents,
            fill_price_cents=dto.fill_price_cents,
            status=dto.status,
            error=dto.error,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return OrderDTO.model_validate(row)

    async def update_status(
        self,
        session: AsyncSession,
        order_id: int,
        status: str,
        fill_price_cents: int | None = None,
        filled_at: datetime.datetime | None = None,
        error: str | None = None,
    ) -> None:
        result = await session.execute(select(Order).where(Order.id == order_id))
        row = result.scalar_one()
        row.status = status
        if fill_price_cents is not None:
            row.fill_price_cents = fill_price_cents
        if filled_at is not None:
            row.filled_at = filled_at
        if error is not None:
            row.error = error
        await session.flush()

    async def get_pending(self, session: AsyncSession) -> list[OrderDTO]:
        result = await session.execute(
            select(Order).where(Order.status == "pending")
        )
        return [OrderDTO.model_validate(r) for r in result.scalars()]

    async def get_for_decision(
        self, session: AsyncSession, decision_id: int
    ) -> list[OrderDTO]:
        result = await session.execute(
            select(Order).where(Order.decision_fk == decision_id)
        )
        return [OrderDTO.model_validate(r) for r in result.scalars()]


class OutcomeRepo:
    async def create(self, session: AsyncSession, dto: OutcomeDTO) -> OutcomeDTO:
        kwargs: dict[str, Any] = dict(
            order_fk=dto.order_fk,
            settlement_price=dto.settlement_price,
            pnl_cents=dto.pnl_cents,
            won=dto.won,
        )
        if dto.resolved_at is not None:
            kwargs["resolved_at"] = dto.resolved_at
        row = Outcome(**kwargs)
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return OutcomeDTO.model_validate(row)

    async def get_for_order(
        self, session: AsyncSession, order_id: int
    ) -> OutcomeDTO | None:
        result = await session.execute(
            select(Outcome).where(Outcome.order_fk == order_id)
        )
        row = result.scalar_one_or_none()
        return OutcomeDTO.model_validate(row) if row is not None else None

    async def daily_pnl(
        self, session: AsyncSession, date: datetime.date
    ) -> int:
        stmt = select(func.coalesce(func.sum(Outcome.pnl_cents), 0)).where(
            func.date(Outcome.resolved_at) == str(date)
        )
        result = await session.execute(stmt)
        value: Any = result.scalar_one()
        return int(value)
