from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import numpy as np
import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from kalshi_botv3.db.models import Base
from kalshi_botv3.exchange.buffers import Aggregator
from kalshi_botv3.exchange.types import OHLCV, OrderbookSnapshot, Trade
from kalshi_botv3.features import price, regime
from kalshi_botv3.features.kalshi_context import kalshi_implied_prob, kalshi_spread_cents
from kalshi_botv3.features.micro import orderbook_imbalance, spread_bps, taker_buy_ratio
from kalshi_botv3.features.pipeline import FeaturePipeline
from kalshi_botv3.features.vector import FeatureVector
from kalshi_botv3.kalshi.models import Orderbook, OrderbookLevel

_UTC = UTC
_TS = datetime(2026, 4, 17, 14, 0, 0, tzinfo=_UTC)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def synthetic_ohlcv() -> pd.DataFrame:
    """240 deterministic 1-minute OHLCV bars (seed=42)."""
    rng = np.random.default_rng(42)
    n = 240
    returns = rng.normal(0, 0.001, n)
    closes = 50_000.0 * np.exp(returns.cumsum())
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    highs = np.maximum(opens, closes) * (1 + rng.uniform(0, 0.0005, n))
    lows = np.minimum(opens, closes) * (1 - rng.uniform(0, 0.0005, n))
    volumes = rng.uniform(0.5, 5.0, n)
    idx = pd.date_range(_TS - timedelta(minutes=n - 1), periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# ===========================================================================
# price.return_over — happy + boundary
# ===========================================================================


def test_return_over_happy(synthetic_ohlcv: pd.DataFrame) -> None:
    result = price.return_over(synthetic_ohlcv, 5)
    assert result is not None
    assert isinstance(result, float)
    close_now = float(synthetic_ohlcv["close"].iloc[-1])
    close_ago = float(synthetic_ohlcv["close"].iloc[-6])
    assert result == pytest.approx(close_now / close_ago - 1.0, rel=1e-9)


def test_return_over_insufficient_data() -> None:
    tiny = pd.DataFrame({"close": [100.0, 101.0]})
    assert price.return_over(tiny, 5) is None


# ===========================================================================
# price.realized_vol — happy + boundary
# ===========================================================================


def test_realized_vol_happy(synthetic_ohlcv: pd.DataFrame) -> None:
    result = price.realized_vol(synthetic_ohlcv, 60)
    assert result is not None
    assert result > 0


def test_realized_vol_insufficient_data() -> None:
    assert price.realized_vol(pd.DataFrame({"close": [100.0]}), 60) is None


# ===========================================================================
# price.rsi — happy + boundary
# ===========================================================================


def test_rsi_happy(synthetic_ohlcv: pd.DataFrame) -> None:
    result = price.rsi(synthetic_ohlcv, 14)
    assert result is not None
    assert 0 <= result <= 100


def test_rsi_insufficient_data() -> None:
    assert price.rsi(pd.DataFrame({"close": [100.0, 101.0]}), 14) is None


def test_rsi_all_gains() -> None:
    # All gains → RSI = 100
    closes = list(range(1, 20))
    df = pd.DataFrame({"close": closes})
    result = price.rsi(df, 14)
    assert result == pytest.approx(100.0)


# ===========================================================================
# price.atr — happy + boundary
# ===========================================================================


def test_atr_happy(synthetic_ohlcv: pd.DataFrame) -> None:
    result = price.atr(synthetic_ohlcv, 14)
    assert result is not None
    assert result > 0


def test_atr_insufficient_data() -> None:
    assert price.atr(pd.DataFrame({"high": [1.0], "low": [0.9], "close": [1.0]}), 14) is None


# ===========================================================================
# price.atr_percentile — happy + boundary
# ===========================================================================


def test_atr_percentile_happy(synthetic_ohlcv: pd.DataFrame) -> None:
    atr_val = price.atr(synthetic_ohlcv, 14)
    assert atr_val is not None
    # Use lookback_bars=200 so requirement (200+14+1=215) fits within 240-bar fixture
    pct = price.atr_percentile(synthetic_ohlcv, atr_val, lookback_bars=200)
    assert pct is not None
    assert 0.0 <= pct <= 1.0


def test_atr_percentile_insufficient_data(synthetic_ohlcv: pd.DataFrame) -> None:
    assert price.atr_percentile(synthetic_ohlcv.iloc[:10], 100.0) is None


# ===========================================================================
# price.vwap_deviation — happy + boundary
# ===========================================================================


def test_vwap_deviation_at_vwap() -> None:
    # Bars where typical price == close → deviation ≈ 0 if volume is equal
    df = pd.DataFrame({
        "high": [100.0, 100.0],
        "low": [100.0, 100.0],
        "close": [100.0, 100.0],
        "volume": [1.0, 1.0],
    })
    result = price.vwap_deviation(df, 2)
    assert result == pytest.approx(0.0)


def test_vwap_deviation_insufficient_data() -> None:
    assert price.vwap_deviation(pd.DataFrame(), 5) is None


# ===========================================================================
# micro.orderbook_imbalance — happy + boundary
# ===========================================================================


def _snap(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        ts=_TS,
        bids=tuple(bids),
        asks=tuple(asks),
    )


def test_orderbook_imbalance_balanced() -> None:
    snap = _snap([(100.0, 10.0), (99.0, 10.0)], [(101.0, 10.0), (102.0, 10.0)])
    result = orderbook_imbalance(snap)
    assert result == pytest.approx(0.0)


def test_orderbook_imbalance_bid_heavy() -> None:
    snap = _snap([(100.0, 90.0)], [(101.0, 10.0)])
    result = orderbook_imbalance(snap)
    assert result == pytest.approx(0.8)  # (90-10)/100


def test_orderbook_imbalance_empty() -> None:
    snap = _snap([], [])
    assert orderbook_imbalance(snap) is None


# ===========================================================================
# micro.taker_buy_ratio — happy + boundary
# ===========================================================================


def test_taker_buy_ratio_half() -> None:
    trades = [
        Trade(ts=_TS, price=100.0, size=1.0, side="buy"),
        Trade(ts=_TS, price=100.0, size=1.0, side="sell"),
    ]
    assert taker_buy_ratio(trades) == pytest.approx(0.5)


def test_taker_buy_ratio_empty() -> None:
    assert taker_buy_ratio([]) is None


def test_taker_buy_ratio_cutoff() -> None:
    old = Trade(ts=_TS - timedelta(seconds=400), price=100.0, size=10.0, side="buy")
    new = Trade(ts=_TS, price=100.0, size=1.0, side="sell")
    # old is outside 300s window → only new counts → all sells → 0.0
    assert taker_buy_ratio([old, new]) == pytest.approx(0.0)


# ===========================================================================
# micro.spread_bps — happy + boundary
# ===========================================================================


def test_spread_bps_basic() -> None:
    snap = _snap([(100.0, 1.0)], [(101.0, 1.0)])
    result = spread_bps(snap)
    # mid = 100.5, spread = 1.0, bps = 1/100.5 * 10000 ≈ 99.5
    assert result is not None
    assert result == pytest.approx(1.0 / 100.5 * 10_000, rel=1e-4)


def test_spread_bps_empty() -> None:
    assert spread_bps(_snap([], [])) is None


# ===========================================================================
# regime functions — happy + boundary
# ===========================================================================


def test_session_bucket_boundaries() -> None:
    assert regime.session_bucket(datetime(2026, 4, 17, 3, 0, tzinfo=_UTC)) == "asia"
    assert regime.session_bucket(datetime(2026, 4, 17, 9, 0, tzinfo=_UTC)) == "eu"
    assert regime.session_bucket(datetime(2026, 4, 17, 15, 0, tzinfo=_UTC)) == "us"
    assert regime.session_bucket(datetime(2026, 4, 17, 22, 0, tzinfo=_UTC)) == "off"


def test_is_weekend() -> None:
    saturday = datetime(2026, 4, 18, 12, 0, tzinfo=_UTC)  # Saturday
    monday = datetime(2026, 4, 20, 12, 0, tzinfo=_UTC)
    assert regime.is_weekend(saturday) is True
    assert regime.is_weekend(monday) is False


def test_minutes_to_top_of_hour_half() -> None:
    half_past = datetime(2026, 4, 17, 14, 30, 0, tzinfo=_UTC)
    assert regime.minutes_to_top_of_hour(half_past) == pytest.approx(30.0)


def test_minutes_to_top_of_hour_at_start() -> None:
    on_hour = datetime(2026, 4, 17, 14, 0, 0, tzinfo=_UTC)
    assert regime.minutes_to_top_of_hour(on_hour) == pytest.approx(60.0)


# ===========================================================================
# kalshi_context functions — happy + boundary
# ===========================================================================


def test_kalshi_implied_prob() -> None:
    assert kalshi_implied_prob(55) == pytest.approx(0.55)
    assert kalshi_implied_prob(1) == pytest.approx(0.01)
    assert kalshi_implied_prob(99) == pytest.approx(0.99)


def test_kalshi_spread_cents_basic() -> None:
    ob = Orderbook(
        ticker="KXBTC15M-26APR171700",
        yes=[OrderbookLevel(price=55, quantity=10)],
        no=[OrderbookLevel(price=40, quantity=10)],
    )
    # spread = (100 - 40) - 55 = 5
    assert kalshi_spread_cents(ob) == 5


def test_kalshi_spread_cents_empty() -> None:
    ob = Orderbook(ticker="KXBTC15M-26APR171700")
    assert kalshi_spread_cents(ob) is None


# ===========================================================================
# test_pipeline_compute_full_vector
# ===========================================================================


async def test_pipeline_compute_full_vector(synthetic_ohlcv: pd.DataFrame) -> None:
    # Build a warmed aggregator
    aggregator = Aggregator(["BTC", "ETH"])
    for _, row in synthetic_ohlcv.iterrows():
        bar = OHLCV(
            ts=row.name.to_pydatetime(),  # type: ignore[union-attr]
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        )
        await aggregator["BTC"].append_minute_bar(bar)
        await aggregator["ETH"].append_minute_bar(bar)

    # Mock Kalshi client
    mock_kalshi = AsyncMock()
    mock_kalshi.get_orderbook.return_value = Orderbook(
        ticker="KXBTC15M-26APR171700",
        yes=[OrderbookLevel(price=55, quantity=10)],
        no=[OrderbookLevel(price=40, quantity=10)],
    )

    from kalshi_botv3.config.runtime_config import RuntimeConfig
    cfg = RuntimeConfig.model_validate({
        "markets": ["BTC"],
        "trade": {"size_usd": 100.0, "edge_threshold_cents": 3.0, "max_kalshi_spread_cents": 5.0, "entry_delay_seconds": 30},
        "scheduler": {"window_minutes": 15, "warmup_minutes": 90},
        "features": {"ohlcv_lookback_minutes": 90, "realized_vol_window_minutes": 60, "atr_window_bars": 14},
        "exchanges": {"primary": "coinbase", "secondary": "binance", "settlement_source": {"BTC": "coinbase"}},
    })

    pipeline = FeaturePipeline(aggregator, mock_kalshi, cfg)
    fv = await pipeline.compute("BTC", _TS)

    assert isinstance(fv, FeatureVector)
    assert fv.return_5m is not None
    assert fv.realized_vol_60m is not None
    assert fv.rsi_14_1m is not None
    assert fv.session_bucket in ("asia", "eu", "us", "off")
    assert not fv.degraded


# ===========================================================================
# test_pipeline_degraded_on_sparse_data
# ===========================================================================


async def test_pipeline_degraded_on_sparse_data() -> None:
    # Empty aggregator → all optional features missing
    aggregator = Aggregator(["BTC", "ETH"])
    mock_kalshi = AsyncMock()
    mock_kalshi.get_orderbook.side_effect = RuntimeError("no market")

    from kalshi_botv3.config.runtime_config import RuntimeConfig
    cfg = RuntimeConfig.model_validate({
        "markets": ["BTC"],
        "trade": {"size_usd": 100.0, "edge_threshold_cents": 3.0, "max_kalshi_spread_cents": 5.0, "entry_delay_seconds": 30},
        "scheduler": {"window_minutes": 15, "warmup_minutes": 90},
        "features": {"ohlcv_lookback_minutes": 90, "realized_vol_window_minutes": 60, "atr_window_bars": 14},
        "exchanges": {"primary": "coinbase", "secondary": "binance", "settlement_source": {"BTC": "coinbase"}},
    })

    pipeline = FeaturePipeline(aggregator, mock_kalshi, cfg)
    fv = await pipeline.compute("BTC", _TS)

    assert len(fv.missing) > 0
    assert "kalshi_orderbook" in fv.missing


# ===========================================================================
# test_pipeline_persists_to_db
# ===========================================================================


async def test_pipeline_persists_to_db(
    synthetic_ohlcv: pd.DataFrame,
    db_session: AsyncSession,
) -> None:
    aggregator = Aggregator(["BTC", "ETH"])
    for _, row in synthetic_ohlcv.iterrows():
        bar = OHLCV(
            ts=row.name.to_pydatetime(),  # type: ignore[union-attr]
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        )
        await aggregator["BTC"].append_minute_bar(bar)

    mock_kalshi = AsyncMock()
    mock_kalshi.get_orderbook.side_effect = RuntimeError("no market")

    from kalshi_botv3.config.runtime_config import RuntimeConfig
    cfg = RuntimeConfig.model_validate({
        "markets": ["BTC"],
        "trade": {"size_usd": 100.0, "edge_threshold_cents": 3.0, "max_kalshi_spread_cents": 5.0, "entry_delay_seconds": 30},
        "scheduler": {"window_minutes": 15, "warmup_minutes": 90},
        "features": {"ohlcv_lookback_minutes": 90, "realized_vol_window_minutes": 60, "atr_window_bars": 14},
        "exchanges": {"primary": "coinbase", "secondary": "binance", "settlement_source": {"BTC": "coinbase"}},
    })

    pipeline = FeaturePipeline(aggregator, mock_kalshi, cfg)
    fv = await pipeline.compute_and_persist(db_session, "BTC", _TS)

    assert isinstance(fv, FeatureVector)

    # Verify DB rows were created
    from kalshi_botv3.db.repository import FeatureRepo, WindowRepo
    window_id = f"BTC-{_TS.isoformat()}"
    window = await WindowRepo().get_by_window_id(db_session, window_id)
    assert window is not None
    feat = await FeatureRepo().get_for_window(db_session, window.id)  # type: ignore[arg-type]
    assert feat is not None
    assert feat.return_5m is not None
