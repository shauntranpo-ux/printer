import pandas as pd
import pytest
from shared.regime_filters import (
    get_current_regime,
    get_regime_threshold,
    get_fee_adjusted_threshold,
)

_FEES = {"kalshi": {"taker_fee_rate": 0.03, "maker_fee_rate": 0.00}, "safety_margin": 0.005}
_CFG = {
    "thresholds": {
        "edge_above_fee": {
            "eu_open": 0.02,
            "asia_deep_night": 0.03,
            "weekend": 0.04,
        }
    }
}


def test_regime_eu_open():
    ts = pd.Timestamp("2026-04-22 10:00:00", tz="UTC")  # Wednesday 10h
    assert get_current_regime(ts) == "eu_open"


def test_regime_us_afternoon():
    ts = pd.Timestamp("2026-04-22 17:30:00", tz="UTC")
    assert get_current_regime(ts) == "us_afternoon"


def test_regime_asia_deep_night():
    ts = pd.Timestamp("2026-04-22 02:00:00", tz="UTC")
    assert get_current_regime(ts) == "asia_deep_night"


def test_weekend_threshold_overrides_regime():
    ts = pd.Timestamp("2026-04-25 10:00:00", tz="UTC")  # Saturday eu_open
    result = get_regime_threshold("eu_open", ts, _CFG)
    assert result == pytest.approx(0.04)  # weekend override


def test_weekday_regime_threshold():
    ts = pd.Timestamp("2026-04-22 10:00:00", tz="UTC")  # Wednesday
    result = get_regime_threshold("eu_open", ts, _CFG)
    assert result == pytest.approx(0.02)


def test_missing_regime_key_defaults_002():
    ts = pd.Timestamp("2026-04-22 10:00:00", tz="UTC")
    result = get_regime_threshold("us_late", ts, _CFG)  # not in config
    assert result == pytest.approx(0.02)


def test_empty_config_defaults_002():
    ts = pd.Timestamp("2026-04-22 10:00:00", tz="UTC")
    result = get_regime_threshold("eu_open", ts, {})
    assert result == pytest.approx(0.02)


def test_fee_adjusted_threshold_composition():
    ts = pd.Timestamp("2026-04-22 10:00:00", tz="UTC")
    result = get_fee_adjusted_threshold("eu_open", ts, _CFG, _FEES)
    # 0.03 + 0.005 + 0.02 = 0.055
    assert result == pytest.approx(0.055)


def test_fee_adjusted_weekend():
    ts = pd.Timestamp("2026-04-25 10:00:00", tz="UTC")  # Saturday
    result = get_fee_adjusted_threshold("eu_open", ts, _CFG, _FEES)
    # 0.03 + 0.005 + 0.04 = 0.075
    assert result == pytest.approx(0.075)
