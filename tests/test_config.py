from pathlib import Path

import pytest
from pydantic import ValidationError

from kalshi_botv3.config.runtime_config import RuntimeConfig, load_runtime_config
from kalshi_botv3.config.settings import Settings, get_settings

_YAML_PATH = Path(__file__).parent.parent / "src" / "kalshi_botv3" / "config" / "config.yaml"


def test_settings_loads_with_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "./nonexistent.pem")
    monkeypatch.setenv("MODE", "dry_run")
    get_settings.cache_clear()
    s = Settings()
    assert s.kalshi_api_key_id == "test-key-id"
    assert s.mode == "dry_run"
    assert s.kalshi_env == "demo"


def test_settings_live_mode_requires_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """live mode with a missing key file must raise a ValidationError mentioning 'live'."""
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "./nonexistent.pem")
    monkeypatch.setenv("MODE", "live")
    monkeypatch.setenv("KALSHI_ENV", "prod")
    with pytest.raises(ValidationError, match="live"):
        Settings()


def test_runtime_config_parses_yaml() -> None:
    cfg = load_runtime_config(_YAML_PATH)
    assert cfg.markets == ["BTC", "ETH", "XRP", "SOL", "DOGE", "HYPE", "BNB"]
    assert len(cfg.markets) == 7


def test_runtime_config_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(
            {
                "markets": ["BTC"],
                "trade": {
                    "size_usd": 5,
                    "edge_threshold_cents": 4,
                    "max_kalshi_spread_cents": 8,
                    "entry_delay_seconds": 75,
                },
                "scheduler": {"window_minutes": 15, "warmup_minutes": 60},
                "features": {
                    "ohlcv_lookback_minutes": 240,
                    "realized_vol_window_minutes": 60,
                    "atr_window_bars": 14,
                },
                "exchanges": {
                    "primary": "coinbase",
                    "secondary": "binance",
                    "settlement_source": {"BTC": "coinbase"},
                },
                "surprise_field": "should_be_rejected",
            }
        )
