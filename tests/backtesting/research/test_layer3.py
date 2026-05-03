import numpy as np
import pytest
from backtesting.research.layer3 import (
    deflated_sharpe_ratio, min_backtest_length,
    probability_of_backtest_overfitting, layer3_verdict,
)


def test_dsr_high_for_good_strategy():
    # SR=3.0 with 5 trials → sr_star ≈ 1.16 → DSR should be high
    pnls = np.random.default_rng(0).normal(0.006, 0.06, 5000)
    dsr = deflated_sharpe_ratio(sr_obs=3.0, pnls=pnls, num_trials=5)
    assert dsr > 0.80


def test_dsr_low_when_many_trials():
    # Same config as good_strategy test but 500 trials → sr_star >> sr_obs → low DSR
    pnls = np.random.default_rng(0).normal(0.006, 0.06, 5000)
    dsr = deflated_sharpe_ratio(sr_obs=3.0, pnls=pnls, num_trials=500)
    assert dsr < 0.90


def test_dsr_between_zero_and_one():
    pnls = np.random.default_rng(1).normal(0, 0.05, 200)
    dsr = deflated_sharpe_ratio(sr_obs=0.5, pnls=pnls, num_trials=50)
    assert 0.0 <= dsr <= 1.0


def test_min_backtest_length_formula():
    # SR=1.0, alpha=0.05 → ~2.7 years
    years = min_backtest_length(sr=1.0, alpha=0.05)
    assert 2.0 < years < 3.5


def test_min_backtest_length_higher_sr_needs_less():
    y1 = min_backtest_length(sr=1.0)
    y2 = min_backtest_length(sr=2.0)
    assert y2 < y1


def test_pbo_all_is_best_is_oos_best():
    # IS-best always also OOS-best → PBO = 0
    folds = [{'is_rank': 0, 'oos_rank': 0} for _ in range(10)]
    assert probability_of_backtest_overfitting(folds) == pytest.approx(0.0)


def test_pbo_is_best_never_oos_best():
    # IS-best always OOS-worst → PBO = 1
    folds = [{'is_rank': 0, 'oos_rank': 4} for _ in range(10)]
    assert probability_of_backtest_overfitting(folds) == pytest.approx(1.0)


def test_layer3_verdict():
    # All thresholds passed → PASS
    assert layer3_verdict(dsr=0.97, pbo=0.18, minbtl=1.2, data_years=3.0) == 'PASS'
    # Low DSR → FAIL (dsr < 0.80 triggers inner FAIL)
    assert layer3_verdict(dsr=0.60, pbo=0.18, minbtl=1.2, data_years=3.0) == 'FAIL'
    # minbtl slightly > data_years but < 1.5x → CONDITIONAL
    assert layer3_verdict(dsr=0.97, pbo=0.18, minbtl=4.0, data_years=3.0) == 'CONDITIONAL'
    # minbtl > data_years * 1.5 → FAIL
    assert layer3_verdict(dsr=0.97, pbo=0.18, minbtl=5.0, data_years=3.0) == 'FAIL'
