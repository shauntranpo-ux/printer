from pathlib import Path

import yaml
from pydantic import BaseModel


class TradeConfig(BaseModel):
    model_config = {"extra": "forbid"}

    # Dollar amount staked per position
    size_usd: float
    # Minimum edge in cents required to enter a trade
    edge_threshold_cents: float
    # Maximum acceptable Kalshi bid-ask spread in cents
    max_kalshi_spread_cents: float
    # Seconds after window open before the bot decides
    entry_delay_seconds: int


class SchedulerConfig(BaseModel):
    model_config = {"extra": "forbid"}

    # Length of each trading window in minutes
    window_minutes: int
    # Minutes of history loaded before trading starts
    warmup_minutes: int


class FeatureConfig(BaseModel):
    model_config = {"extra": "forbid"}

    # Total OHLCV lookback in minutes for feature computation
    ohlcv_lookback_minutes: int
    # Rolling window in minutes for realised volatility
    realized_vol_window_minutes: int
    # Number of bars for ATR computation
    atr_window_bars: int


class ExchangesConfig(BaseModel):
    model_config = {"extra": "forbid"}

    # Primary price feed exchange name
    primary: str
    # Fallback price feed exchange name
    secondary: str
    # Per-asset settlement reference exchange
    settlement_source: dict[str, str]


class RuntimeConfig(BaseModel):
    model_config = {"extra": "forbid"}

    # List of Kalshi market asset tickers to trade
    markets: list[str]
    trade: TradeConfig
    scheduler: SchedulerConfig
    features: FeatureConfig
    exchanges: ExchangesConfig


def load_runtime_config(path: Path) -> RuntimeConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return RuntimeConfig.model_validate(raw)
