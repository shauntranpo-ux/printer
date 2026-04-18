import datetime
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from kalshi_botv3.db.dtos import (
    DecisionDTO,
    FeatureDTO,
    OrderDTO,
    OutcomeDTO,
    WindowDTO,
)
from kalshi_botv3.db.models import Base
from kalshi_botv3.db.repository import (
    DecisionRepo,
    FeatureRepo,
    OrderRepo,
    OutcomeRepo,
    WindowRepo,
)

_UTC = datetime.UTC
_TS = datetime.datetime(2026, 4, 17, 14, 0, 0, tzinfo=_UTC)
_TS_END = datetime.datetime(2026, 4, 17, 14, 15, 0, tzinfo=_UTC)


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _window_dto(market: str = "BTC", ts: datetime.datetime = _TS) -> WindowDTO:
    return WindowDTO(
        window_id=f"{market}-{ts.isoformat()}",
        market=market,
        start_ts=ts,
        end_ts=_TS_END,
    )


async def _make_chain(
    session: AsyncSession,
    market: str = "BTC",
    side: str = "YES",
    status: str = "pending",
    pnl_cents: int = 40,
    ts: datetime.datetime = _TS,
    resolved_at: datetime.datetime | None = None,
) -> tuple[WindowDTO, FeatureDTO, DecisionDTO, OrderDTO, OutcomeDTO]:
    w = await WindowRepo().create(session, _window_dto(market=market, ts=ts))
    assert w.id is not None

    f = await FeatureRepo().create(
        session,
        FeatureDTO(window_fk=w.id, return_5m=0.005, realized_vol_60m=0.02),
    )

    d = await DecisionRepo().create(
        session,
        DecisionDTO(
            window_fk=w.id,
            strategy="baseline",
            side=side,
            edge=0.06,
            confidence=0.80,
            reason="edge above threshold",
        ),
    )
    assert d.id is not None

    o = await OrderRepo().create(
        session,
        OrderDTO(
            decision_fk=d.id,
            side=side,
            quantity=1,
            limit_price_cents=60,
            status=status,
        ),
    )
    assert o.id is not None

    out = await OutcomeRepo().create(
        session,
        OutcomeDTO(
            order_fk=o.id,
            pnl_cents=pnl_cents,
            won=pnl_cents > 0,
            resolved_at=resolved_at,
        ),
    )

    return w, f, d, o, out


async def test_create_and_fetch_window(db_session: AsyncSession) -> None:
    created = await WindowRepo().create(db_session, _window_dto())
    assert created.id is not None

    fetched = await WindowRepo().get_by_window_id(db_session, created.window_id)
    assert fetched is not None
    assert fetched.market == "BTC"
    assert fetched.id == created.id


async def test_create_feature_linked_to_window(db_session: AsyncSession) -> None:
    w = await WindowRepo().create(db_session, _window_dto())
    assert w.id is not None

    feat = await FeatureRepo().create(
        db_session,
        FeatureDTO(window_fk=w.id, return_5m=0.003, realized_vol_60m=0.015),
    )
    assert feat.id is not None
    assert feat.window_fk == w.id

    fetched = await FeatureRepo().get_for_window(db_session, w.id)
    assert fetched is not None
    assert fetched.return_5m == pytest.approx(0.003)


async def test_create_decision_and_order_chain(db_session: AsyncSession) -> None:
    _w, _f, d, o, _out = await _make_chain(db_session)

    orders = await OrderRepo().get_for_decision(db_session, d.id)  # type: ignore[arg-type]
    assert len(orders) == 1
    assert orders[0].status == "pending"

    outcome = await OutcomeRepo().get_for_order(db_session, o.id)  # type: ignore[arg-type]
    assert outcome is not None
    assert outcome.pnl_cents == 40
    assert outcome.won is True


async def test_unique_market_start_ts_constraint(db_session: AsyncSession) -> None:
    await WindowRepo().create(db_session, _window_dto())
    await db_session.flush()

    with pytest.raises(IntegrityError):
        await WindowRepo().create(db_session, _window_dto())
        await db_session.flush()


async def test_outcome_pnl_calculation(db_session: AsyncSession) -> None:
    _, _, _, _o, out = await _make_chain(db_session, pnl_cents=-60)
    assert out.won is False
    assert out.pnl_cents == -60


async def test_daily_pnl_aggregation(db_session: AsyncSession) -> None:
    ts_today = datetime.datetime(2026, 4, 17, 14, 0, 0, tzinfo=_UTC)
    ts_yesterday = datetime.datetime(2026, 4, 16, 14, 0, 0, tzinfo=_UTC)

    await _make_chain(db_session, market="BTC", pnl_cents=50, ts=ts_today, resolved_at=ts_today)
    await _make_chain(db_session, market="ETH", pnl_cents=-30, ts=ts_today, resolved_at=ts_today)
    await _make_chain(
        db_session, market="SOL", pnl_cents=100, ts=ts_yesterday, resolved_at=ts_yesterday
    )

    today = datetime.date(2026, 4, 17)
    yesterday = datetime.date(2026, 4, 16)

    assert await OutcomeRepo().daily_pnl(db_session, today) == 20
    assert await OutcomeRepo().daily_pnl(db_session, yesterday) == 100
