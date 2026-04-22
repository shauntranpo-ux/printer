"""Smoke tests for data loaders using synthetic in-memory data."""
import os
import pandas as pd
import numpy as np
import pytest
from backtesting.data.loaders import (
    _enforce_utc, _check_history_length, _filter_date_range, load_bars
)


def _make_bars_df(n_days=200):
    ts = pd.date_range("2025-01-01", periods=n_days * 8640, freq="10s", tz="UTC")
    rng = np.random.default_rng(0)
    prices = 50000 + rng.normal(0, 100, len(ts)).cumsum()
    return pd.DataFrame({
        "timestamp": ts,
        "open": prices,
        "high": prices + rng.uniform(0, 10, len(ts)),
        "low": prices - rng.uniform(0, 10, len(ts)),
        "close": prices + rng.normal(0, 5, len(ts)),
        "volume": rng.uniform(0.1, 5.0, len(ts)),
    })


def test_enforce_utc_naive():
    df = pd.DataFrame({"timestamp": ["2025-01-01 00:00:00", "2025-01-02 00:00:00"]})
    out = _enforce_utc(df)
    assert out["timestamp"].dt.tz is not None
    assert str(out["timestamp"].dt.tz) == "UTC"


def test_enforce_utc_already_utc():
    ts = pd.date_range("2025-01-01", periods=3, freq="h", tz="UTC")
    df = pd.DataFrame({"timestamp": ts})
    out = _enforce_utc(df)
    assert str(out["timestamp"].dt.tz) == "UTC"


def test_check_history_length_passes():
    df = _make_bars_df(200)
    _check_history_length(df, "BTC")  # should not raise


def test_check_history_length_fails():
    df = _make_bars_df(30)
    with pytest.raises(ValueError, match="Insufficient history"):
        _check_history_length(df, "BTC")


def test_filter_date_range():
    df = _make_bars_df(200)
    df = _enforce_utc(df)
    out = _filter_date_range(df, "2025-03-01", "2025-04-01")
    assert out["timestamp"].min() >= pd.Timestamp("2025-03-01", tz="UTC")
    assert out["timestamp"].max() <= pd.Timestamp("2025-04-01", tz="UTC")


def test_load_bars_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_bars("FAKE_ASSET", base_path="/nonexistent/path", check_min_history=False)
