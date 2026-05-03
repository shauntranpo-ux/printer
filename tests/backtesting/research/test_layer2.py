import numpy as np
import pandas as pd
import pytest
from backtesting.research.layer2 import (
    run_null_simulation, flip_side_pnl, NullResult, layer2_verdict,
    audit_lookahead,
)


def _trade_log(n=200, win_rate=0.58):
    rng = np.random.default_rng(42)
    wins = (rng.uniform(0, 1, n) < win_rate)
    pnls = np.where(wins, 0.08, -0.07)
    return pd.DataFrame({'pnl': pnls, 'side': np.where(wins, 'yes', 'no')})


def test_flip_side_pnl_inverts():
    pnls = np.array([0.08, -0.07, 0.08])
    flipped = flip_side_pnl(pnls)
    assert flipped[0] == pytest.approx(-0.08)
    assert flipped[1] == pytest.approx(0.07)


def test_null_result_structure():
    log = _trade_log(n=150)
    result = run_null_simulation(log['pnl'].values, n_iter=100, seed=0)
    assert isinstance(result, NullResult)
    assert len(result.null_sharpes) == 100
    assert result.real_sharpe != 0.0
    assert 0.0 <= result.p_value <= 1.0


def test_null_worse_than_real_on_good_signal():
    # Good signal: 60% win rate → real Sharpe > median null
    log = _trade_log(n=300, win_rate=0.60)
    result = run_null_simulation(log['pnl'].values, n_iter=500, seed=1)
    assert result.real_sharpe > np.median(result.null_sharpes)


def test_null_indistinguishable_on_noise():
    # Noise signal: 50% win rate → real Sharpe ≈ null distribution
    rng = np.random.default_rng(7)
    pnls = np.where(rng.integers(0, 2, 200), 0.08, -0.08)
    result = run_null_simulation(pnls, n_iter=500, seed=2)
    assert result.p_value > 0.05  # fail to reject H0


def test_layer2_verdict():
    assert layer2_verdict(0.01) == 'PASS'
    assert layer2_verdict(0.07) == 'CONDITIONAL'
    assert layer2_verdict(0.15) == 'FAIL'


def test_audit_lookahead_returns_list():
    findings = audit_lookahead('BTC')
    assert isinstance(findings, list)
