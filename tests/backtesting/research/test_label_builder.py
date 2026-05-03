import pandas as pd
import numpy as np
from backtesting.research.label_builder import build_binary_labels, build_lagged_labels, STRIKE_SPACING, nearest_strike

def _bars(prices, freq='1min'):
    idx = pd.date_range('2025-01-01 00:00', periods=len(prices), freq=freq, tz='UTC')
    return pd.DataFrame({'close': prices, 'open': prices, 'high': prices, 'low': prices, 'volume': 1.0}, index=idx)

def test_yes_wins():
    prices = [100_000 + i * 100 for i in range(30)]
    bars = _bars(prices)
    labels = build_binary_labels(bars, strike=100_000.0, horizon_bars=15)
    assert labels[0] == 1   # close[15] = 101500 > 100000

def test_no_wins():
    prices = [100_000 - i * 100 for i in range(30)]
    bars = _bars(prices)
    labels = build_binary_labels(bars, strike=100_000.0, horizon_bars=15)
    assert labels[0] == 0   # close[15] = 98500 < 100000

def test_output_length():
    bars = _bars([100.0] * 40)
    labels = build_binary_labels(bars, strike=99.0, horizon_bars=15)
    assert len(labels) == 25  # 40 - 15
    assert labels.dtype == np.int8

def test_lagged_labels_keys():
    bars = _bars([100.0 + i * 0.1 for i in range(60)])
    lagged = build_lagged_labels(bars, strike=100.0, lags=[1, 2, 4, 8])
    assert set(lagged.keys()) == {1, 2, 4, 8}
    assert len(lagged[1]) == 59   # 60 - 1
    assert len(lagged[8]) == 52   # 60 - 8

def test_strike_spacing_defined():
    assert 'BTC' in STRIKE_SPACING
    assert 'ETH' in STRIKE_SPACING


def test_nearest_strike_btc():
    assert nearest_strike(100_049.99, "BTC") == 100_000.0
    assert nearest_strike(100_050.01, "BTC") == 100_100.0


def test_nearest_strike_eth():
    assert nearest_strike(3_497.3, "ETH") == 3_495.0
    assert nearest_strike(3_502.6, "ETH") == 3_505.0
