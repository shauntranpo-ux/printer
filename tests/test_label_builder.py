import pandas as pd
import numpy as np
import pytest
from backtesting.data.label_builder import build_labels


def _make_bars(n_windows=50, bars_per_window=90, start_price=50000.0, seed=42):
    rng = np.random.default_rng(seed)
    total = n_windows * bars_per_window
    ts = pd.date_range("2025-01-01 00:00:00", periods=total, freq="10s", tz="UTC")
    prices = start_price + rng.normal(0, 50, total).cumsum()
    prices = np.clip(prices, 1000, None)
    return pd.DataFrame({
        "timestamp": ts,
        "open": prices,
        "high": prices + rng.uniform(0, 10, total),
        "low":  prices - rng.uniform(0, 10, total),
        "close": prices + rng.normal(0, 20, total),
        "volume": rng.uniform(0.1, 5.0, total),
    })


def test_output_columns():
    bars = _make_bars()
    out = build_labels(bars)
    assert set(out.columns) == {"timestamp", "label", "reference_price_open",
                                "reference_price_close", "log_return"}


def test_label_binary():
    bars = _make_bars()
    out = build_labels(bars)
    assert out["label"].isin([0, 1]).all()


def test_label_matches_price_comparison():
    bars = _make_bars()
    out = build_labels(bars)
    expected = (out["reference_price_close"] > out["reference_price_open"]).astype(int)
    pd.testing.assert_series_equal(out["label"], expected, check_names=False)


def test_log_return_sign_matches_label():
    bars = _make_bars()
    out = build_labels(bars)
    assert (out.loc[out["label"] == 1, "log_return"] > 0).all()
    assert (out.loc[out["label"] == 0, "log_return"] <= 0).all()


def test_incomplete_window_dropped():
    bars = _make_bars(n_windows=5)
    bars = bars.iloc[5:].copy()
    out = build_labels(bars, drop_incomplete=True)
    assert len(out) < 5


def test_empty_bars_returns_empty():
    empty = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    empty = empty.astype({"timestamp": "datetime64[ns, UTC]"})
    out = build_labels(empty)
    assert out.empty


def test_timestamps_are_utc():
    bars = _make_bars()
    out = build_labels(bars)
    assert out["timestamp"].dt.tz is not None
    assert str(out["timestamp"].dt.tz) == "UTC"
