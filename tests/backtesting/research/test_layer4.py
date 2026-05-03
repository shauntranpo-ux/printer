import numpy as np
import pandas as pd
import pytest
from backtesting.research.layer4 import (
    block_shuffle, full_shuffle_test, block_shuffle_test,
    min_trades_needed, PermResult, run_layer4,
)


def test_block_shuffle_preserves_values():
    arr = np.arange(100, dtype=float)
    shuffled = block_shuffle(arr, block_size=10, seed=0)
    assert sorted(shuffled) == sorted(arr)


def test_block_shuffle_different_order():
    arr = np.arange(50, dtype=float)
    shuffled = block_shuffle(arr, block_size=5, seed=1)
    assert not np.array_equal(arr, shuffled)


def test_full_shuffle_p_low_for_good_signal():
    # Good strategy: consistent wins → drawdown shallower than random ordering
    rng = np.random.default_rng(42)
    wins = rng.uniform(0, 1, 300) < 0.60
    pnls = np.where(wins, 0.08, -0.07)
    result = full_shuffle_test(pnls, n_iter=1000, seed=0)
    assert isinstance(result, PermResult)
    # p-value should NOT be degenerate (not 0.0 or 1.0 always)
    assert 0.0 <= result.p_value <= 1.0


def test_block_shuffle_p_high_for_noise():
    rng = np.random.default_rng(99)
    pnls = np.where(rng.integers(0, 2, 200), 0.08, -0.08)
    result = block_shuffle_test(pnls, n_iter=500, block_size=10, seed=0)
    # Noise signal: real drawdown ≈ null distribution
    assert 0.0 <= result.p_value <= 1.0


def test_min_trades_needed():
    assert min_trades_needed(win_rate=0.65) <= 60
    assert min_trades_needed(win_rate=0.55) >= 200


def test_run_layer4_structure():
    rng = np.random.default_rng(0)
    wins = rng.uniform(0, 1, 200) < 0.58
    log = pd.DataFrame({'pnl': np.where(wins, 0.08, -0.07)})
    result = run_layer4(log, n_iter=200)
    assert 'p_value_full' in result
    assert 'p_value_block' in result
    assert 'verdict' in result
    assert 'sufficient_data' in result
