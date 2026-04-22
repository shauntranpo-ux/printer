import numpy as np
import pytest
from backtesting.metrics.overfitting import (
    deflated_sharpe_ratio, probabilistic_sharpe_ratio,
    probability_of_backtest_overfitting, overfitting_summary,
)


def test_dsr_returns_valid_range():
    dsr, pvalue = deflated_sharpe_ratio(sharpe_obs=0.0, n_trials=1, n_obs=100)
    assert 0.0 <= dsr <= 1.0
    assert 0.0 <= pvalue <= 1.0


def test_dsr_negative_sharpe_gives_low_dsr():
    dsr, _ = deflated_sharpe_ratio(sharpe_obs=-2.0, n_trials=10, n_obs=200)
    assert dsr < 0.5


def test_psr_high_negative_sharpe():
    psr = probabilistic_sharpe_ratio(sharpe_obs=-3.0, sharpe_benchmark=0.0, n_obs=252)
    assert psr < 0.01


def test_psr_high_positive_sharpe():
    psr = probabilistic_sharpe_ratio(sharpe_obs=3.0, sharpe_benchmark=0.0, n_obs=252)
    assert psr > 0.99


def test_pbo_range():
    oos = np.array([0.5, 0.8, -0.2, 1.2, 0.3])
    is_ = np.array([1.5, 0.9, 0.3, 0.7, 1.1])
    pbo = probability_of_backtest_overfitting(oos, is_)
    assert 0.0 <= pbo <= 1.0


def test_overfitting_summary_keys():
    oos = np.array([0.5, 0.8, -0.2, 1.2, 0.3])
    is_ = np.array([1.5, 0.9, 0.3, 0.7, 1.1])
    summary = overfitting_summary(oos, is_, n_trials=5)
    assert "dsr" in summary
    assert "pbo" in summary
    assert "psr" in summary


def test_empty_oos_returns_nans():
    summary = overfitting_summary(np.array([]), np.array([]), n_trials=1)
    assert summary["dsr"] != summary["dsr"]  # isnan check
