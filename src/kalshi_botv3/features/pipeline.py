from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TypeVar

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_botv3.config.runtime_config import RuntimeConfig
from kalshi_botv3.db.dtos import FeatureDTO, WindowDTO
from kalshi_botv3.db.repository import FeatureRepo, WindowRepo
from kalshi_botv3.exchange.buffers import Aggregator
from kalshi_botv3.features import cross_market, micro, price, regime
from kalshi_botv3.features.kalshi_context import kalshi_implied_prob, kalshi_spread_cents
from kalshi_botv3.features.vector import FeatureVector
from kalshi_botv3.kalshi.models import Orderbook
from kalshi_botv3.kalshi.ticker_map import build_current_window_ticker
from kalshi_botv3.utils.events import FEATURES_COMPUTED
from kalshi_botv3.utils.logging import get_logger

_T = TypeVar("_T")
_DEGRADED_THRESHOLD = 0.30
_OPTIONAL_FEATURE_COUNT = 18  # features that can legitimately be None

logger = get_logger("features.pipeline")


# ---------------------------------------------------------------------------
# Kalshi client protocol (avoids importing concrete client types)
# ---------------------------------------------------------------------------


class _KalshiClientProto:
    """Structural protocol — anything with get_orderbook/get_market works."""

    async def get_orderbook(self, ticker: str, depth: int = 10) -> Orderbook:
        raise NotImplementedError

    async def get_market(
        self, ticker: str
    ) -> object:  # Market — avoid import cycle
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class FeaturePipeline:
    def __init__(
        self,
        aggregator: Aggregator,
        kalshi_client: object,  # duck-typed: get_orderbook(ticker) -> Orderbook
        runtime_config: RuntimeConfig,
    ) -> None:
        self._agg = aggregator
        self._kalshi = kalshi_client
        self._cfg = runtime_config

    # ------------------------------------------------------------------
    # _safe_call: wraps any callable, catches exceptions, marks missing
    # ------------------------------------------------------------------

    def _safe_call(
        self,
        name: str,
        fn: Callable[[], _T],
        missing: set[str],
    ) -> _T | None:
        try:
            return fn()
        except Exception as exc:
            logger.warning("feature_failed", feature=name, error=str(exc))
            missing.add(name)
            return None

    # ------------------------------------------------------------------
    # Main compute path
    # ------------------------------------------------------------------

    async def compute(self, market: str, window_start: datetime) -> FeatureVector:
        now = datetime.now(UTC)
        missing: set[str] = set()

        # Retrieve data from in-memory buffers
        buf = self._agg.get(market, None)
        df = buf.get_ohlcv() if buf is not None else pd.DataFrame()
        trades = buf.get_trades_since(now - timedelta(minutes=10)) if buf is not None else []
        ob_snap = buf.latest_orderbook() if buf is not None else None

        # ---- async Kalshi calls (best-effort) --------------------------
        kalshi_ob: Orderbook | None = None
        yes_price: int | None = None
        try:
            ticker = build_current_window_ticker(market, window_start)
            # duck-typed: works with HttpKalshiClient and MockKalshiClient
            kalshi_ob = await self._kalshi.get_orderbook(ticker)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("kalshi_feature_failed", error=str(exc))
            missing.add("kalshi_orderbook")

        # Try to get yes price from first yes level of orderbook
        if kalshi_ob is not None and kalshi_ob.yes:
            yes_price = kalshi_ob.yes[0].price

        # ---- price features -------------------------------------------
        r1m = self._safe_call("return_1m", lambda: price.return_over(df, 1), missing)
        r5m = self._safe_call("return_5m", lambda: price.return_over(df, 5), missing)
        r15m = self._safe_call("return_15m", lambda: price.return_over(df, 15), missing)
        rvol = self._safe_call("realized_vol_60m", lambda: price.realized_vol(df, 60), missing)
        atr14 = self._safe_call("atr_14", lambda: price.atr(df), missing)
        atr_pct = self._safe_call(
            "atr_percentile",
            lambda: price.atr_percentile(df, atr14) if atr14 is not None else None,
            missing,
        )
        rsi14 = self._safe_call("rsi_14_1m", lambda: price.rsi(df), missing)
        vwap_dev = self._safe_call(
            "vwap_deviation_60m", lambda: price.vwap_deviation(df, 60), missing
        )

        # ---- microstructure -------------------------------------------
        ob_imb = self._safe_call(
            "orderbook_imbalance",
            lambda: micro.orderbook_imbalance(ob_snap) if ob_snap is not None else None,
            missing,
        )
        tbr = self._safe_call(
            "taker_buy_ratio_5m",
            lambda: micro.taker_buy_ratio(trades, lookback_seconds=300.0),
            missing,
        )
        sprd_bps = self._safe_call(
            "spread_bps",
            lambda: micro.spread_bps(ob_snap) if ob_snap is not None else None,
            missing,
        )

        # ---- cross-market ---------------------------------------------
        btc3m = self._safe_call(
            "btc_return_3m", lambda: cross_market.btc_return(self._agg, 3), missing
        )
        btc5m = self._safe_call(
            "btc_return_5m", lambda: cross_market.btc_return(self._agg, 5), missing
        )
        eth_z = self._safe_call(
            "eth_btc_ratio_z",
            lambda: cross_market.eth_btc_ratio_zscore(self._agg, window_min=60),
            missing,
        )

        # ---- regime (never fail — pure time arithmetic) ---------------
        session = regime.session_bucket(now)
        weekend = regime.is_weekend(now)
        mins_to_hour = regime.minutes_to_top_of_hour(now)

        # ---- Kalshi context ------------------------------------------
        k_prob: float | None = None
        k_spread: int | None = None
        secs_open: float | None = None

        if yes_price is not None:
            k_prob = self._safe_call(
                "kalshi_implied_prob",
                lambda: kalshi_implied_prob(yes_price),
                missing,
            )
        else:
            missing.add("kalshi_implied_prob")

        if kalshi_ob is not None:
            k_spread = self._safe_call(
                "kalshi_spread_cents",
                lambda: kalshi_spread_cents(kalshi_ob),
                missing,
            )
        else:
            missing.add("kalshi_spread_cents")

        secs_open = (now - window_start).total_seconds()

        # ---- degraded check ------------------------------------------
        degraded = len(missing) / _OPTIONAL_FEATURE_COUNT > _DEGRADED_THRESHOLD

        fv = FeatureVector(
            return_1m=r1m,
            return_5m=r5m,
            return_15m=r15m,
            realized_vol_60m=rvol,
            atr_14=atr14,
            atr_percentile=atr_pct,
            rsi_14_1m=rsi14,
            vwap_deviation_60m=vwap_dev,
            orderbook_imbalance=ob_imb,
            taker_buy_ratio_5m=tbr,
            spread_bps=sprd_bps,
            btc_return_3m=btc3m,
            btc_return_5m=btc5m,
            eth_btc_ratio_z=eth_z,
            session_bucket=session,
            is_weekend=weekend,
            minutes_to_top_of_hour=mins_to_hour,
            kalshi_yes_price_cents=yes_price,
            kalshi_implied_prob=k_prob,
            kalshi_spread_cents=k_spread,
            seconds_since_window_open=secs_open,
            computed_at=now,
            degraded=degraded,
            missing=frozenset(missing),
        )
        logger.info(
            FEATURES_COMPUTED,
            market=market,
            missing_count=len(missing),
            degraded=degraded,
        )
        return fv

    # ------------------------------------------------------------------
    # Persist to database
    # ------------------------------------------------------------------

    async def compute_and_persist(
        self,
        session: AsyncSession,
        market: str,
        window_start: datetime,
    ) -> FeatureVector:
        fv = await self.compute(market, window_start)

        window_id = f"{market}-{window_start.isoformat()}"
        window_repo = WindowRepo()
        window = await window_repo.get_by_window_id(session, window_id)
        if window is None:
            window = await window_repo.create(
                session,
                WindowDTO(
                    window_id=window_id,
                    market=market,
                    start_ts=window_start,
                    end_ts=window_start + timedelta(minutes=15),
                ),
            )

        assert window.id is not None
        await FeatureRepo().create(
            session,
            FeatureDTO(
                window_fk=window.id,
                computed_at=fv.computed_at,
                payload=fv.to_dict(),
                return_1m=fv.return_1m,
                return_5m=fv.return_5m,
                return_15m=fv.return_15m,
                realized_vol_60m=fv.realized_vol_60m,
                atr_percentile=fv.atr_percentile,
                btc_return_5m=fv.btc_return_5m,
                degraded=fv.degraded,
            ),
        )
        return fv
