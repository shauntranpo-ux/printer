from __future__ import annotations

import math
from typing import Any, Dict, List

import numpy as np
import pandas as pd
from scipy import stats
from scipy.stats import norm


def _expected_max_sharpe(num_trials: int) -> float:
    """
    Expected maximum Sharpe across num_trials IID zero-mean unit-variance strategies.
    Approximation: E[max SR] ≈ Φ^{-1}(1 - 1/N).
    """
    if num_trials < 2:
        return 0.0
    return float(norm.ppf(1 - 1.0 / num_trials))


def deflated_sharpe_ratio(
    sr_obs: float,
    pnls: np.ndarray,
    num_trials: int,
) -> float:
    """
    Deflated Sharpe Ratio — adjusts observed SR for non-normality of returns
    and for the number of independent configurations tested.

    sr_obs:     annualized Sharpe ratio of the strategy
    pnls:       per-observation P&L array (used for skew + kurtosis)
    num_trials: number of independent configurations tested (e.g. EV sweeps)

    Returns DSR ∈ [0, 1]: probability that the true SR > 0 after adjustment.
    """
    n = len(pnls)
    if n < 10:
        return 0.0
    skew = float(stats.skew(pnls))
    kurt = float(stats.kurtosis(pnls, fisher=False))  # full kurtosis (normal = 3)

    # Standard error of the Sharpe ratio estimator
    sr_std = math.sqrt(
        max(1e-10, (1.0 - skew * sr_obs + (kurt - 1.0) / 4.0 * sr_obs ** 2) / (n - 1))
    )
    sr_star = _expected_max_sharpe(num_trials)

    dsr = norm.cdf((sr_obs - sr_star) / sr_std)
    return float(dsr)


def min_backtest_length(sr: float, alpha: float = 0.05) -> float:
    """
    Minimum years of data needed to reject H0 (SR=0) at significance level alpha.
    Approximation: MinBTL ≈ (Z_alpha / SR)^2 years.
    """
    if sr <= 0:
        return float('inf')
    z_alpha = norm.ppf(1 - alpha)
    return (z_alpha / sr) ** 2


def probability_of_backtest_overfitting(
    fold_results: List[Dict[str, int]],
) -> float:
    """
    PBO from CPCV fold results.

    fold_results: list of dicts with keys 'is_rank' and 'oos_rank'.
        is_rank:  0-indexed rank of IS-best strategy among all configs (0 = best IS)
        oos_rank: 0-indexed rank of that same strategy's OOS performance

    PBO = fraction of folds where the IS-best strategy ranked below the median OOS.
    Threshold: n_configs // 2.
    """
    if not fold_results:
        return float('nan')
    overfit_count = sum(
        1 for f in fold_results if f['oos_rank'] > 0
    )
    return overfit_count / len(fold_results)


def layer3_verdict(dsr: float, pbo: float, minbtl: float, data_years: float) -> str:
    if dsr < 0.95 or pbo > 0.25 or minbtl > data_years:
        if dsr < 0.80 or pbo > 0.45 or minbtl > data_years:
            return 'FAIL'
        return 'CONDITIONAL'
    return 'PASS'


def run_layer3(
    trade_log: pd.DataFrame,
    wfa_sharpes: List[float],
    data_years: float,
    num_trials: int = 50,
) -> Dict[str, Any]:
    """
    Layer 3: WFA significance.

    trade_log:   DataFrame with 'pnl' column.
    wfa_sharpes: per-fold Sharpe ratios from walk-forward analysis.
    data_years:  total years of history used.
    num_trials:  number of independent configs tested (e.g. EV sweep count).
    """
    from backtesting.metrics.trading import sharpe_ratio
    pnls = trade_log['pnl'].values
    sr_obs = sharpe_ratio(pnls)

    dsr    = deflated_sharpe_ratio(sr_obs, pnls, num_trials)
    minbtl = min_backtest_length(sr_obs)

    # PBO: compare each WFA fold Sharpe to median; above median = not overfit
    median_wfa = float(np.median(wfa_sharpes)) if wfa_sharpes else 0.0
    fold_results = [
        {'is_rank': 0, 'oos_rank': 0 if s >= median_wfa else 1}
        for s in wfa_sharpes
    ]
    pbo = probability_of_backtest_overfitting(fold_results)

    return {
        'sr_obs':      round(sr_obs, 4),
        'dsr':         round(dsr, 4),
        'pbo':         round(pbo, 4),
        'minbtl':      round(minbtl, 2),
        'data_years':  round(data_years, 2),
        'num_trials':  num_trials,
        'verdict':     layer3_verdict(dsr, pbo, minbtl, data_years),
    }
