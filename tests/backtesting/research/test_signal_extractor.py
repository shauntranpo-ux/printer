import numpy as np
import pandas as pd
from backtesting.research.signal_extractor import extract_all_signals, SIGNAL_NAMES


def _bars(n=120, trend=0.0):
    rng = np.random.default_rng(42)
    prices = [100_000.0 + trend * i + rng.normal(0, 50) for i in range(n)]
    return pd.DataFrame({
        'close': prices, 'open': prices,
        'high': [p + 20 for p in prices],
        'low': [p - 20 for p in prices],
        'volume': np.ones(n),
    })


def test_returns_all_signal_names():
    result = extract_all_signals(_bars(), strike=100_000.0, asset='BTC')
    for name in SIGNAL_NAMES:
        assert name in result, f"Missing signal: {name}"


def test_predictions_are_probabilities():
    result = extract_all_signals(_bars(), strike=100_000.0, asset='BTC')
    for name, preds in result.items():
        assert isinstance(preds, np.ndarray), f"{name} not ndarray"
        assert np.all((preds >= 0.0) & (preds <= 1.0)), f"{name} has out-of-range values"


def test_output_length_matches_bars():
    result = extract_all_signals(_bars(n=90), strike=100_000.0, asset='BTC')
    for name, preds in result.items():
        assert len(preds) == 90, f"{name} length mismatch"


def test_supertrend_bimodal():
    # With a clear uptrend, supertrend should return mostly 0.70 (bullish)
    result = extract_all_signals(_bars(n=200, trend=100.0), strike=100_000.0, asset='BTC')
    st = result['supertrend_direction']
    unique = np.unique(st)
    # Should have at most 3 distinct values: 0.3, 0.5 (warmup), 0.7
    assert len(unique) <= 3
