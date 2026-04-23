"""Tests for strategy_c.features.vol_term_structure."""
import sys
import os

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategies"))

from strategy_c.features.vol_term_structure import integrate_forecasted_variance


def _flat_forecast(variance_per_sub_interval):
    """Returns a forecast function that always returns the same value."""
    def fn(_ts):
        return variance_per_sub_interval
    return fn


def _constant_regime(regime_name):
    """Returns a regime lookup that always returns the same regime."""
    def fn(_ts):
        return regime_name
    return fn


_BASE_CONFIG = {
    "vol_term_structure": {
        "sub_interval_minutes": 15,
        "regime_multipliers": {
            "flat": 1.0,
        },
    }
}


class TestIntegrateForcastedVariance:
    def test_zero_time_returns_zero(self):
        ts = pd.Timestamp("2024-01-01 12:00:00", tz="UTC")
        result = integrate_forecasted_variance(
            _flat_forecast(0.001), ts, ts, _constant_regime("flat"), _BASE_CONFIG
        )
        assert result == 0.0

    def test_past_expiry_returns_zero(self):
        ts_now = pd.Timestamp("2024-01-01 13:00:00", tz="UTC")
        ts_exp = pd.Timestamp("2024-01-01 12:00:00", tz="UTC")
        result = integrate_forecasted_variance(
            _flat_forecast(0.001), ts_now, ts_exp, _constant_regime("flat"), _BASE_CONFIG
        )
        assert result == 0.0

    def test_one_full_interval_flat_multiplier(self):
        ts_now = pd.Timestamp("2024-01-01 12:00:00", tz="UTC")
        ts_exp = pd.Timestamp("2024-01-01 12:15:00", tz="UTC")
        var_per_sub = 0.0025
        result = integrate_forecasted_variance(
            _flat_forecast(var_per_sub), ts_now, ts_exp, _constant_regime("flat"), _BASE_CONFIG
        )
        assert result == pytest.approx(var_per_sub, rel=1e-6)

    def test_doubling_time_doubles_variance(self):
        ts_base = pd.Timestamp("2024-01-01 12:00:00", tz="UTC")
        ts_1h = pd.Timestamp("2024-01-01 13:00:00", tz="UTC")
        ts_2h = pd.Timestamp("2024-01-01 14:00:00", tz="UTC")
        var_per_sub = 0.001
        r1 = integrate_forecasted_variance(
            _flat_forecast(var_per_sub), ts_base, ts_1h, _constant_regime("flat"), _BASE_CONFIG
        )
        r2 = integrate_forecasted_variance(
            _flat_forecast(var_per_sub), ts_base, ts_2h, _constant_regime("flat"), _BASE_CONFIG
        )
        assert r2 == pytest.approx(2.0 * r1, rel=1e-6)

    def test_regime_multiplier_applied(self):
        config = {
            "vol_term_structure": {
                "sub_interval_minutes": 15,
                "regime_multipliers": {
                    "high_vol": 2.0,
                    "low_vol": 0.5,
                },
            }
        }
        ts_now = pd.Timestamp("2024-01-01 12:00:00", tz="UTC")
        ts_exp = pd.Timestamp("2024-01-01 12:15:00", tz="UTC")
        var_per_sub = 0.001
        r_high = integrate_forecasted_variance(
            _flat_forecast(var_per_sub), ts_now, ts_exp, _constant_regime("high_vol"), config
        )
        r_low = integrate_forecasted_variance(
            _flat_forecast(var_per_sub), ts_now, ts_exp, _constant_regime("low_vol"), config
        )
        assert r_high == pytest.approx(4.0 * r_low, rel=1e-6)

    def test_null_regime_multiplier_defaults_to_one(self):
        config = {
            "vol_term_structure": {
                "sub_interval_minutes": 15,
                "regime_multipliers": {"unknown": None},
            }
        }
        ts_now = pd.Timestamp("2024-01-01 12:00:00", tz="UTC")
        ts_exp = pd.Timestamp("2024-01-01 12:15:00", tz="UTC")
        var_per_sub = 0.002
        r = integrate_forecasted_variance(
            _flat_forecast(var_per_sub), ts_now, ts_exp, _constant_regime("unknown"), config
        )
        assert r == pytest.approx(var_per_sub, rel=1e-6)

    def test_partial_last_interval(self):
        # 22.5 minutes window with 15-minute sub-intervals → 1.0 + 0.5 = 1.5 sub-intervals
        ts_now = pd.Timestamp("2024-01-01 12:00:00", tz="UTC")
        ts_exp = pd.Timestamp("2024-01-01 12:22:30", tz="UTC")
        var_per_sub = 0.001
        r = integrate_forecasted_variance(
            _flat_forecast(var_per_sub), ts_now, ts_exp, _constant_regime("flat"), _BASE_CONFIG
        )
        assert r == pytest.approx(1.5 * var_per_sub, rel=1e-4)

    def test_returns_float(self):
        ts_now = pd.Timestamp("2024-01-01 12:00:00", tz="UTC")
        ts_exp = pd.Timestamp("2024-01-01 13:00:00", tz="UTC")
        r = integrate_forecasted_variance(
            _flat_forecast(0.001), ts_now, ts_exp, _constant_regime("flat"), _BASE_CONFIG
        )
        assert isinstance(r, float)
        assert r >= 0.0
