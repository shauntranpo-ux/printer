from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal


@dataclass(frozen=True)
class FeatureVector:
    # ------------------------------------------------------------------
    # Price
    return_1m: float | None = None
    return_5m: float | None = None
    return_15m: float | None = None

    # ------------------------------------------------------------------
    # Volatility
    realized_vol_60m: float | None = None
    atr_14: float | None = None
    atr_percentile: float | None = None

    # ------------------------------------------------------------------
    # Momentum
    rsi_14_1m: float | None = None
    vwap_deviation_60m: float | None = None

    # ------------------------------------------------------------------
    # Microstructure
    orderbook_imbalance: float | None = None
    taker_buy_ratio_5m: float | None = None
    spread_bps: float | None = None

    # ------------------------------------------------------------------
    # Cross-market
    btc_return_3m: float | None = None
    btc_return_5m: float | None = None
    eth_btc_ratio_z: float | None = None

    # ------------------------------------------------------------------
    # Regime (always present — pure time functions)
    session_bucket: Literal["asia", "eu", "us", "off"] = "off"
    is_weekend: bool = False
    minutes_to_top_of_hour: float = 0.0

    # ------------------------------------------------------------------
    # Kalshi context
    kalshi_yes_price_cents: int | None = None
    kalshi_implied_prob: float | None = None
    kalshi_spread_cents: int | None = None
    seconds_since_window_open: float | None = None

    # ------------------------------------------------------------------
    # Meta
    computed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    degraded: bool = False
    missing: frozenset[str] = field(default_factory=frozenset)

    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, float | int | str | bool | list[str] | None]:
        result: dict[str, float | int | str | bool | list[str] | None] = {}
        for f in dataclasses.fields(self):
            val = getattr(self, f.name)
            if isinstance(val, frozenset):
                result[f.name] = sorted(val)
            elif isinstance(val, datetime):
                result[f.name] = val.isoformat()
            else:
                result[f.name] = val
        return result

    def as_feature_dict_for_strategy(self) -> dict[str, float | int | bool | None]:
        """Numeric feature dict for strategy models. Excludes meta fields."""
        return {
            "return_1m": self.return_1m,
            "return_5m": self.return_5m,
            "return_15m": self.return_15m,
            "realized_vol_60m": self.realized_vol_60m,
            "atr_14": self.atr_14,
            "atr_percentile": self.atr_percentile,
            "rsi_14_1m": self.rsi_14_1m,
            "vwap_deviation_60m": self.vwap_deviation_60m,
            "orderbook_imbalance": self.orderbook_imbalance,
            "taker_buy_ratio_5m": self.taker_buy_ratio_5m,
            "spread_bps": self.spread_bps,
            "btc_return_3m": self.btc_return_3m,
            "btc_return_5m": self.btc_return_5m,
            "eth_btc_ratio_z": self.eth_btc_ratio_z,
            "is_weekend": self.is_weekend,
            "minutes_to_top_of_hour": self.minutes_to_top_of_hour,
            "kalshi_implied_prob": self.kalshi_implied_prob,
            "seconds_since_window_open": self.seconds_since_window_open,
        }
