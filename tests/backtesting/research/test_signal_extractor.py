import numpy as np
import pandas as pd
from backtesting.research.signal_extractor import extract_all_signals, SIGNAL_NAMES


def _bars(n=120, trend=0.0, seed=42):
    rng = np.random.default_rng(seed)
    prices = [100_000.0 + trend * i + rng.normal(0, 50) for i in range(n)]
    return pd.DataFrame({
        'close': prices, 'open': prices,
        'high': [p + 20 for p in prices],
        'low': [p - 20 for p in prices],
        'volume': np.ones(n),
    })


def test_returns_all_signal_names():
    result = extract_all_signals(_bars(), strike=100_000.0, asset='BTC')
    assert set(result.keys()) == set(SIGNAL_NAMES)
    for name in SIGNAL_NAMES:
        assert name in result, f"Missing signal: {name}"


def test_predictions_are_probabilities():
    result = extract_all_signals(_bars(), strike=100_000.0, asset='BTC')
    for name, preds in result.items():
        assert isinstance(preds, np.ndarray), f"{name} not ndarray"
        assert np.all((preds >= 0.0) & (preds <= 1.0)), f"{name} out-of-range values"


def test_output_length_matches_bars():
    result = extract_all_signals(_bars(n=90), strike=100_000.0, asset='BTC')
    for name, preds in result.items():
        assert len(preds) == 90, f"{name} length mismatch"


def test_v2_inverted_downtrend_produces_high_prob():
    # Strong downtrend → MTF momentum negative → V2 inverted → 0.65 (YES)
    result = extract_all_signals(_bars(n=200, trend=-500.0), strike=200_000.0, asset='BTC')
    v2 = result['v2_mtf_momentum']
    late = v2[50:]
    assert np.any(late == 0.65), "Expected v2_mtf_momentum=0.65 in downtrend (inverted)"


def test_v3_inverted_downtrend_produces_high_prob():
    # Strong downtrend → RSI oversold → V3 inverted → 0.65 (YES)
    result = extract_all_signals(_bars(n=200, trend=-500.0), strike=200_000.0, asset='BTC')
    v3 = result['v3_rsi']
    late = v3[30:]
    assert np.any(late == 0.65), "Expected v3_rsi=0.65 (oversold) in downtrend"


def test_v5_fires_at_least_as_often_as_v2():
    # V5 threshold = MTF_threshold/2 so it fires more often than V2
    result = extract_all_signals(_bars(n=200, trend=-500.0), strike=200_000.0, asset='BTC')
    v2 = result['v2_mtf_momentum']
    v5 = result['v5_mtf_magnitude']
    # Where V2 fires YES, V5 must also fire YES (V5 has a lower threshold)
    v2_yes_mask = v2 == 0.65
    assert np.all(v5[v2_yes_mask] == 0.65), "V5 must fire YES everywhere V2 fires YES"


def test_asset_thresholds_differ():
    # BTC RSI threshold=5.0, SOL=10.0 — both should run cleanly and return correct shapes
    result_btc = extract_all_signals(_bars(n=200, trend=-100.0), strike=100_000.0, asset='BTC')
    result_sol = extract_all_signals(_bars(n=200, trend=-100.0), strike=100_000.0, asset='SOL')
    for name in SIGNAL_NAMES:
        assert len(result_btc[name]) == 200, f"BTC {name} length wrong"
        assert len(result_sol[name]) == 200, f"SOL {name} length wrong"
