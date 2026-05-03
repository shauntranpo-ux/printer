import numpy as np
import pandas as pd
import pytest
from backtesting.research.layer5 import (
    variance_ratio, classify_vol_tercile, classify_trend_regime,
    session_label, compute_regime_breakdown, run_layer5, layer5_verdict,
)


def _price_series(n=200, drift=0.0, vol=0.01, seed=0):
    rng = np.random.default_rng(seed)
    log_rets = rng.normal(drift, vol, n)
    return np.exp(np.cumsum(log_rets)) * 100_000


def test_variance_ratio_trending():
    # Strong uptrend → VR > 1.0
    prices = _price_series(n=200, drift=0.003, vol=0.001)
    vr = variance_ratio(prices, q=4)
    assert vr > 1.0


def test_variance_ratio_mean_reverting():
    # Alternating up/down → VR < 1.0
    n = 200
    prices = np.cumprod(1 + np.tile([-0.005, 0.005], n // 2)) * 100_000
    vr = variance_ratio(prices[:n], q=4)
    assert vr < 1.0


def test_variance_ratio_random_walk():
    # Pure random walk → VR ≈ 1.0 (within noise)
    rng = np.random.default_rng(42)
    log_rets = rng.normal(0, 0.01, 500)
    prices = np.exp(np.cumsum(log_rets)) * 100_000
    vr = variance_ratio(prices, q=4)
    assert 0.6 < vr < 1.4  # wider band; random walk VR converges slowly


def test_classify_vol_tercile():
    idx = pd.date_range('2025-01-01', periods=300, freq='1min', tz='UTC')
    prices = np.ones(300) * 100_000
    bars = pd.DataFrame({'close': prices}, index=idx)
    result = classify_vol_tercile(bars, window_bars=100)
    assert set(result.unique()).issubset({'low', 'mid', 'high'})


def test_session_label():
    assert session_label(pd.Timestamp('2025-01-01 10:00', tz='UTC')) == 'US'
    assert session_label(pd.Timestamp('2025-01-01 05:00', tz='UTC')) == 'London'
    assert session_label(pd.Timestamp('2025-01-01 01:00', tz='UTC')) == 'Asia'


def test_session_label_boundaries():
    # Boundary hours
    assert session_label(pd.Timestamp('2025-01-01 04:00', tz='UTC')) == 'London'   # start of London
    assert session_label(pd.Timestamp('2025-01-01 09:00', tz='UTC')) == 'US'       # start of US
    assert session_label(pd.Timestamp('2025-01-01 20:00', tz='UTC')) == 'Asia'     # start of Asia
    assert session_label(pd.Timestamp('2025-01-01 23:00', tz='UTC')) == 'Asia'     # late Asia


def test_layer5_verdict():
    # All positive → PASS
    assert layer5_verdict({'trending': 1.2, 'random': 0.8, 'mean_rev': 0.4}) == 'PASS'
    # Majority negative → FAIL
    assert layer5_verdict({'a': -0.5, 'b': -1.2, 'c': -0.8, 'd': 0.1}) == 'FAIL'
    # Empty → INSUFFICIENT_DATA
    assert layer5_verdict({}) == 'INSUFFICIENT_DATA'


def test_compute_regime_breakdown_structure():
    idx = pd.date_range('2025-01-01', periods=100, freq='15min', tz='UTC')
    log = pd.DataFrame({
        'pnl':       np.random.default_rng(0).normal(0.01, 0.06, 100),
        'timestamp': idx,
    })
    regime = pd.Series(['low_vol_trending'] * 100, index=idx)
    result = compute_regime_breakdown(log, regime)
    assert 'regime_sharpes' in result
    assert 'session_sharpes' in result


def test_run_layer5_structure():
    n = 500
    idx = pd.date_range('2025-01-01', periods=n, freq='1min', tz='UTC')
    bars = pd.DataFrame({
        'close':  _price_series(n=n, drift=0.001, vol=0.005),
        'open':   _price_series(n=n, drift=0.001, vol=0.005, seed=1),
        'high':   _price_series(n=n, drift=0.001, vol=0.005, seed=2) + 20,
        'low':    _price_series(n=n, drift=0.001, vol=0.005, seed=3) - 20,
        'volume': np.ones(n),
    }, index=idx)
    trade_idx = idx[::10]
    log = pd.DataFrame({
        'pnl': np.random.default_rng(0).normal(0.01, 0.05, len(trade_idx)),
    }, index=trade_idx)
    result = run_layer5(log, bars, vol_window_bars=100, vr_window=30)
    assert 'regime_sharpes' in result
    assert 'session_sharpes' in result
    assert 'verdict' in result
