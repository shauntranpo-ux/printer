import numpy as np
import pandas as pd
from backtesting.research.layer1 import run_layer1, layer1_verdict


def _bars(n=200, trend=50.0):
    rng = np.random.default_rng(7)
    prices = [100_000.0 + trend * i + rng.normal(0, 100) for i in range(n)]
    idx = pd.date_range('2025-01-01', periods=n, freq='1min', tz='UTC')
    return pd.DataFrame({
        'close': prices, 'open': prices,
        'high': [p + 50 for p in prices], 'low': [p - 50 for p in prices],
        'volume': np.ones(n),
    }, index=idx)


def test_run_layer1_returns_all_signals():
    from backtesting.research.signal_extractor import SIGNAL_NAMES
    bars = _bars()
    result = run_layer1(bars, strike=100_000.0, asset='BTC')
    assert 'signals' in result
    assert 'verdict' in result
    assert 'n_failing' in result
    for name in SIGNAL_NAMES:
        assert name in result['signals']


def test_run_layer1_result_structure():
    bars = _bars()
    result = run_layer1(bars, strike=100_000.0, asset='BTC')
    sig = result['signals']['v2_mtf_momentum']
    assert 'ic' in sig
    assert 'icir' in sig
    assert 't_stat' in sig
    assert 'ic_decay' in sig
    assert 'verdict' in sig
    assert len(sig['ic_decay']) == 4


def test_layer1_verdict_aggregation():
    assert layer1_verdict(n_failing=8, n_total=8) == 'FAIL'
    assert layer1_verdict(n_failing=1, n_total=8) == 'PASS'
    assert layer1_verdict(n_failing=3, n_total=8) == 'CONDITIONAL'
